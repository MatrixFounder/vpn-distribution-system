"""``POST /agent/v1/enroll`` — обмен одноразового bootstrap-токена на identity ноды (interfaces.md
§5.2; security.md §7.1; UC-01 шаг 5, A1; Н-24). Задача 001.24: схемы и заглушка ``NodeService``.

Единственный маршрут ``/agent/v1`` без клиентского сертификата: nginx обслуживает его отдельным
``server`` (порт enrollment, 001.02), поэтому здесь нет ``X-Client-Fingerprint``. Логика проверки
токена (хеш, срок, одноразовость, привязка к ноде), подпись CSR ключом CA, запись identity и
переход ноды в ``pending`` — 001.25; ограничение частоты по адресу источника и хешу токена
(§5.12, fail-closed) — 001.72.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.agent_api.body import BoundedBodyRoute
from app.db.pool import db_pool
from app.domain.nodes import NodeService, Version
from app.errors import ApiError
from app.security.ca import pem_der

router = APIRouter(route_class=BoundedBodyRoute, strict_content_type=True)

# Длина в символах (``max_length`` pydantic): PEM — ASCII, поэтому символ равен байту, а
# enrollment-server nginx и без того обрывает тело на 64 КБ (``client_max_body_size``).
CSR_MAX_CHARS = 16 * 1024  # PEM CSR ноды — единицы килобайт; больше — не CSR


class EnrollIn(BaseModel):
    """Тело enrollment: bootstrap-токен (256 бит base64url), CSR в PEM, версии агента и Xray
    (UC-01 шаг 6: администратор сверяет их при подтверждении). Неизвестные поля — 422."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Алфавит — base64url выданного токена (``generate_bootstrap_token``, 256 бит §7.2):
    # вход сужается до отказа, а не после него — значение уедет в поиск по хешу (001.25).
    bootstrap_token: Annotated[
        str, StringConstraints(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    ]
    # Алфавит PEM (base64, переводы строк, строки BEGIN/END): проверка формы по байтам
    # (``app.body_shape``) доказывает по шаблону, что символов формы JSON в поле нет.
    csr_pem: Annotated[
        str,
        StringConstraints(min_length=1, max_length=CSR_MAX_CHARS, pattern=r"^[A-Za-z0-9+/=\s-]+$"),
    ]
    agent_version: Version
    xray_version: Version

    @field_validator("csr_pem")
    @classmethod
    def _csr(cls, value: str) -> str:
        pem_der(value, "CERTIFICATE REQUEST")  # ValueError → 422 с текстом причины
        return value


class EnrollOut(BaseModel):
    """Identity ноды: клиентский сертификат внутреннего CA, сертификат CA для проверки сервера,
    токен identity для заголовка агента, идентификатор ноды."""

    client_cert_pem: str
    ca_pem: str
    identity_token: str = Field(description="предъявляется в каждом запросе /agent/v1 (§7.1)")
    node_id: uuid.UUID


async def get_node_service(pool: Annotated[object, Depends(db_pool)]) -> NodeService:
    """Пул — зависимостью, а не вызовом внутри фабрики: так его подмена в тесте видна всему графу
    зависимостей раздела (контрактные тесты 001.28 подставляют объект-часовой вместо базы)."""
    return NodeService(pool)


Nodes = Annotated[NodeService, Depends(get_node_service)]


@router.post(
    "/enroll",
    response_model=EnrollOut,
    summary="Обмен bootstrap-токена на identity ноды (UC-01 шаг 5)",
    responses={
        400: {
            "description": "тело не разобрано (негодный UTF-8, число длиннее 4 300 цифр) — "
            "«тело запроса не разобрано»; тот же ответ у оборванной заливки"
        },
        401: {"description": "токен неизвестен, использован или просрочен (UC-01 A1)"},
        413: {
            "description": "тело больше предела enrollment-server (64 КиБ); страница nginx без "
            "тела единого формата"
        },
        411: {
            "description": "тело без известной длины (chunked, поток HTTP/2 без content-length) "
            "прокси не принимает — `length_required` единого формата: не повторять"
        },
        422: {"description": "тело не прошло проверку или CSR отвергнут CA (`invalid_csr`)"},
        429: {
            "description": "предел прокси enrollment-server: не чаще одного запроса в секунду и "
            "не больше четырёх соединений с адреса, не чаще двух в секунду от всех клиентов "
            "(`Retry-After`); хеш токена и распределённый источник — 001.72"
        },
        503: {
            "description": "апстрим недоступен — `upstream_unavailable` единого формата от "
            "прокси с `Retry-After`: повторить"
        },
    },
)
async def enroll(body: EnrollIn, nodes: Nodes) -> EnrollOut:
    try:
        identity = await nodes.enroll(
            body.bootstrap_token, body.csr_pem, body.agent_version, body.xray_version
        )
    except ValueError as exc:
        # Тело уже прошло проверку рамки PEM, поэтому сюда доходит только CSR, отвергнутый
        # самим CA (разбор и подпись — 001.25): это ошибка запроса, а не сбой сервера, и
        # текст исключения наружу не выносится. Страж ветки — тест с подменой службы.
        raise ApiError("invalid_csr", "CSR отвергнут удостоверяющим центром", status=422) from exc
    return EnrollOut(
        client_cert_pem=identity.client_cert_pem,
        ca_pem=identity.ca_pem,
        identity_token=identity.identity_token,
        node_id=identity.node_id,
    )
