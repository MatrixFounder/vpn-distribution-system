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

from app.db.pool import db_pool
from app.domain.nodes import NodeService, Version
from app.errors import ApiError
from app.security.ca import pem_der

router = APIRouter()

# Длина в символах (``max_length`` pydantic): PEM — ASCII, поэтому символ равен байту, а
# enrollment-server nginx и без того обрывает тело на 64 КБ (``client_max_body_size``).
CSR_MAX_CHARS = 16 * 1024  # PEM CSR ноды — единицы килобайт; больше — не CSR


class EnrollIn(BaseModel):
    """Тело enrollment: bootstrap-токен (256 бит base64url), CSR в PEM, версии агента и Xray
    (UC-01 шаг 6: администратор сверяет их при подтверждении). Неизвестные поля — 422."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    bootstrap_token: Annotated[str, StringConstraints(min_length=32, max_length=128)]
    csr_pem: Annotated[str, StringConstraints(min_length=1, max_length=CSR_MAX_CHARS)]
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


async def get_node_service() -> NodeService:
    return NodeService(await db_pool())


Nodes = Annotated[NodeService, Depends(get_node_service)]


@router.post(
    "/enroll",
    response_model=EnrollOut,
    summary="Обмен bootstrap-токена на identity ноды (UC-01 шаг 5)",
    responses={
        401: {"description": "токен неизвестен, использован или просрочен (UC-01 A1)"},
        422: {"description": "тело не прошло проверку или CSR отвергнут CA (`invalid_csr`)"},
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
