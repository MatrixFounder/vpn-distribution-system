"""``POST /agent/v1/reports`` и ``POST /agent/v1/quota/request`` — отчёт о трафике и запрос
гранта квоты (interfaces.md §5.2; постановка §5.9, §4.11; UC-04; R-19, R-21, R-26).

Задача 001.33: маршруты на заглушках ``AccountingService`` и ``QuotaService``; схемы живут в
``app.accounting`` (домен возвращает их, обратный порядок сделал бы домен зависимым от слоя
API). Приём с ключом идемпотентности, допуском времени и порогом аномалии — 001.34, факты и
списание — 001.77; правило гранта — 001.36; ``conn_stats[]`` для признаков перепродажи —
001.39.

Тело отчёта — единственное в разделе, что весит больше сотен байт: до ``REPORT_MAX_LINES`` строк и
``REPORT_MAX_ONLINE_IPS`` адресов, 50 мс времени апстрима на стенде (p95 при двух потоках
отчётов) в цикле событий процесса api на потолке и не больше двух частей одновременно на парк
(2 × 50 мс плюс операции остальных видов по 4 мс и enrollment по 2 мс за окно разбора в бюджете Н-4
по ``urt`` с резервом 50 мс на транзакцию). FastAPI читает тело и разбирает JSON до разрешения
зависимостей, то есть до отказа по версии агента и identity, а проверку модели — после них, но
тоже синхронно в том же цикле, — поэтому и предел размера, и число одновременных отчётов
ограничивает прокси: nginx пропускает отчёты отдельным ``location = /agent/v1/reports`` со своим
пределом, ``limit_conn`` и ``limit_req`` по отпечатку клиентского сертификата, а остальные
операции остаются под пределом агентского ``server`` (64 КБ) с общей частотой по сертификату.
Отозванную identity nginx не знает до 001.25 (``ssl_crl``) — до тех пор держатель любого
сертификата CA оплачивается пределами прокси, не приложением.

Путь отказа не дороже пути приёма: ``BoundedBodyRoute`` (``agent_api/body.py``) перед разбором
JSON сверяет тело по байтам с пределами формы модели маршрута (у отчёта —
``service.BODY_MAX_OPENERS``/``BODY_MAX_COMMAS``, у гранта — два поля) — тело шире их
недопустимо заведомо, и на него уходит ``bytes.count``, а не тысячи ошибок pydantic; глубина
вложенности тем самым тоже ограничена (в пределах формы разобранный не-объект получает 422
модели: 3 000 уровней у отчёта — без ``RecursionError``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.accounting.quota import QuotaGrant, QuotaRequestIn
from app.accounting.service import Accept, ReportIn
from app.agent_api.body import BoundedBodyRoute
from app.agent_api.deps import Accounting, Quotas, Served
from app.agent_api.state import AGENT_ERRORS

# Проверка формы по байтам стоит на каждой операции раздела по пределам её модели
# (``agent_api/body.py``): у отчёта — пределы частей, у запроса гранта — два поля и одна запятая.
router = APIRouter(route_class=BoundedBodyRoute, strict_content_type=True)

# 429 и 413 для отчётов отдаёт прокси (nginx, `deploy/nginx/nginx.conf`), а не приложение:
# 429 — в едином формате ошибки с `Retry-After` (`error_page` прокси), 413 — стандартной
# страницей nginx без тела единого формата. Оба объявлены здесь, потому что контракт читает
# вторая сторона обмена (001.53), а не потому, что их производит этот код.
PROXY_429 = {
    "description": "предел прокси: не больше одного отчёта от ноды и двух от парка "
    "одновременно, не чаще двух в секунду (сверх — очередь, 429 — сверх очереди), не больше "
    "восьми соединений ноды на весь агентский server (`Retry-After`); лимит приложения — 001.72"
}
REPORT_ERRORS: dict[int | str, dict[str, Any]] = {
    **AGENT_ERRORS,
    403: {
        "description": "нода не подтверждена (`pending`, §4.6) — `node_not_approved`; отчёт "
        "содержит идентификатор другой ноды — `node_mismatch` (`Retry-After`)"
    },
    413: {
        "description": "тело больше предела прокси (1 МиБ): отчёт делится и не повторяется "
        "как есть; ответ — страница nginx без тела единого формата"
    },
    429: PROXY_429,
}
QUOTA_ERRORS: dict[int | str, dict[str, Any]] = {
    **AGENT_ERRORS,
    403: {
        "description": "нода не подтверждена (`pending`, §4.6) — `node_not_approved`; ноде в "
        "этом статусе грант не выдаётся (`provisioning`, `disabled`, `suspended`) — "
        "`grant_unavailable` (`Retry-After`)"
    },
}


@router.post(
    "/reports",
    response_model=Accept,
    summary="Отчёт о трафике за интервал: ключ, период, строки, адреса, счётчики (§5.9)",
    responses=REPORT_ERRORS,
)
async def report(body: ReportIn, agent: Served, accounting: Accounting) -> Accept:
    """Нода — из identity запроса; ``node_id`` тела, если есть, служба сверяет с ней."""
    return await accounting.accept_report(agent.node, body)


@router.post(
    "/quota/request",
    response_model=QuotaGrant,
    summary="Запрос гранта квоты пары «пользователь × нода» (R-26, §4.11)",
    responses=QUOTA_ERRORS,
)
async def quota_request(body: QuotaRequestIn, agent: Served, quotas: Quotas) -> QuotaGrant:
    """Нода — из identity запроса, целиком: вентиль §4.6 для гранта применяет служба."""
    return await quotas.grant(agent.node, body.user_id, body.consumed_bytes)
