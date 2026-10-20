"""Сквозные проверки отчётов о трафике и запроса гранта квоты на заглушках (задача 001.33;
R-19, R-21…R-24, R-26, R-27, R-29, R-48; UC-04): контракт обеих операций в OpenAPI, приём отчёта
(TC-E2E-01), грант, признаки identity и вентиль §4.6 по инвентарю раздела, форма отчёта (границы
колонок §4.2.5, порядок границ периода, уникальность ключей строк и адресов, пределы размера
§5.7), правило «нода отчитывается только за себя», разобранное тело доходит до домена.

Байтовая форма ответов закреплена отдельно — `tests/contract/test_agent_v1.py` по фикстурам
`contracts/agent_v1/report*.json` и `quota-request.json`.

Задача 001.34 — приём на живой базе (раздел внизу файла, стенд — `tests/e2e/_reporting.py`):
TC-E2E-01…03 (повтор, порог, время), A3, правила частей и интервалов, якорь, закрытие, потолки
открытых интервалов и смен эпохи, состав ноды, перепроверка identity под блокировкой, точка
продолжения, статусы §4.6, метрика длительности, фикстуры отчёта как ответы живого приёма.
Проверки формы и маршрута выше остаются герметичными: приём в них — `ShapeOnlyAccounting`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from app import metrics
from app.accounting import quota, service
from app.accounting.device_limit import DeviceLimitService
from app.accounting.quota import QuotaGrant, QuotaService
from app.accounting.service import (
    _EPOCH_CHANGES,
    BODY_MAX_COMMAS,
    BODY_MAX_OPENERS,
    REPORT_MAX_EPOCHS_PER_DAY,
    REPORT_MAX_LINES,
    REPORT_MAX_ONLINE_IPS,
    REPORT_MAX_OPEN_INTERVALS,
    Accept,
    AccountingService,
    ReportIn,
    _Acceptance,
    anomaly_threshold,
    own_node,
)
from app.agent_api.body import BoundedBodyRoute
from app.agent_api.deps import (
    current_node,
    get_accounting_service,
    get_device_limit_service,
    get_quota_service,
)
from app.agent_api.router import router as agent_router
from app.db.pool import db_pool
from app.domain import composition
from app.domain.nodes import _LOCK_NODE as REVOCATION_LOCK
from app.domain.nodes import STUB_NODE_ID, Node, NodeStatus, stub_node
from app.domain.statuses import INT8_MAX, STREAM_GATE
from app.errors import VALIDATION_ERRORS_MAX, VALIDATION_LOC_MAX_CHARS
from app.main import create_app
from fastapi import APIRouter
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from tests._agent import FINGERPRINT, HEADERS, node_as
from tests._reports import USER_A, USER_B, VALID_REPORT, report_with, widest_report

from ._reporting import (
    Line,
    ReportingStand,
    db_now,
    insert_identity,
    insert_node,
    insert_user,
    reporting,
    wait_blocked_by,
)

REPORTS = "/agent/v1/reports"
QUOTA = "/agent/v1/quota/request"
OTHER_NODE_ID = uuid.UUID("00000000-0000-7000-8000-0000000000bb")
VALID_QUOTA: dict[str, Any] = {"user_id": USER_A, "consumed_bytes": 536870912}
# Величины требований — литералами, а не из констант приёма: вход, выведенный из проверяемого
# предела, растёт вместе с ним, и страж не краснеет (посадка P04). Допуск часов §5.9 — 5 с: точки
# у границы — 3 с внутри, 7 и 10 с снаружи. Константы закреплены своими утверждениями в
# `tests/unit/accounting/test_acceptance.py`.
BUFFER_DEPTH = dt.timedelta(hours=24)  # Н-8: якорь первого интервала
PARTS_DEADLINE = dt.timedelta(hours=25)  # срок закрытия после последней части (README)
INTERVAL_AGE = dt.timedelta(hours=48)  # потолок возраста открытого интервала (README)


class NoDatabase:
    """Заглушки раздела в базу не ходят — обращение к пулу валит тест."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"обращение к базе: {name}")


class ShapeOnlyAccounting(AccountingService):
    """Приём без базы для проверок формы тела, вентиля и маршрута: правило «нода отчитывается
    только за себя» — настоящее (``own_node``, его же вызывает служба до транзакции), ответ —
    точка продолжения, равная номеру части. Настоящий приём — тесты 001.34 ниже, на живой
    базе."""

    def __init__(self) -> None:
        super().__init__(NoDatabase())

    async def accept_report(self, node: Node, report: ReportIn, fingerprint: str) -> Accept:
        own_node(node, report)
        return Accept(last_accepted_seq=report.report_seq, duplicate=False)


@asynccontextmanager
async def agent(
    status: NodeStatus = "active", overrides: dict[Any, Any] | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    """Клиент раздела без базы: нода запроса — фиксированная карточка в статусе `status`, признаки
    и версия разбираются настоящим кодом (`tests/_agent.py::node_as`), приём — без базы
    (``ShapeOnlyAccounting``); `overrides` — прочие зависимости (в том числе другая нода)."""
    app = create_app()
    app.dependency_overrides[db_pool] = NoDatabase
    app.dependency_overrides[current_node] = node_as(status)
    app.dependency_overrides[get_accounting_service] = ShapeOnlyAccounting
    for dependency, replacement in (overrides or {}).items():
        app.dependency_overrides[dependency] = replacement
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://control-plane") as client:
        yield client


def fields(schema: dict[str, Any], name: str) -> set[str]:
    return set(schema["components"]["schemas"][name]["properties"])


def required(schema: dict[str, Any], name: str) -> set[str]:
    return set(schema["components"]["schemas"][name].get("required", ()))


async def test_reports_and_quota_contract_in_openapi(app_client: httpx.AsyncClient) -> None:
    """Критерий приёмки «схемы зафиксированы»: обе операции в схеме под тегом раздела, коды
    ответов и наборы полей — равенством, обязательность — равенством (поле, ставшее
    необязательным, молча подменяется умолчанием), пределы списков — литералами."""
    schema = (await app_client.get("/openapi.json")).json()
    paths = schema["paths"]
    common = {"200", "400", "401", "403", "411", "413", "422", "426", "429", "503"}
    # 409 — отказ части отчёта (001.34): у запроса гранта его нет.
    for path, model, codes in (
        (REPORTS, "Accept", common | {"409"}),
        (QUOTA, "QuotaGrant", common),
    ):
        operation = paths[path]["post"]
        assert operation["tags"] == ["agent"], path
        assert set(operation["responses"]) == codes, path
        answer = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert answer["$ref"].endswith(f"/{model}"), (path, answer)
        assert "requestBody" in operation, path
    assert "node_mismatch" in paths[REPORTS]["post"]["responses"]["403"]["description"], (
        "второй смысл 403 объявлен второй стороне обмена"
    )
    assert "grant_unavailable" in paths[QUOTA]["post"]["responses"]["403"]["description"]
    for path in (REPORTS, QUOTA):
        assert "прокси" in paths[path]["post"]["responses"]["429"]["description"], (
            "429 для этих операций отдаёт прокси — сказано второй стороне обмена"
        )
    refusals = paths[REPORTS]["post"]["responses"]
    for code in (
        "rejected_time",
        "future",
        "too_old",
        "overlap",
        "before_anchor",
        "interval_closed",
        "`closed`",
        "out_of_range",
        "repeated",
    ):
        assert code in refusals["409"]["description"], code
    for code in (
        "report_inconsistent",
        "parts_total_decreased",
        "counters_in_continuation",
        "user_in_another_part",
        "seq_reused",
    ):
        assert code in refusals["422"]["description"], code
    assert "1 МиБ" in paths[REPORTS]["post"]["responses"]["413"]["description"]
    assert "64 КиБ" in paths[QUOTA]["post"]["responses"]["413"]["description"]

    assert fields(schema, "ReportIn") == {
        "node_id",
        "counter_epoch",
        "report_seq",
        "parts_total",
        "period_start",
        "period_end",
        "lines",
        "online_ips",
        "node_rx_bytes",
        "node_tx_bytes",
    }
    assert required(schema, "ReportIn") == fields(schema, "ReportIn") - {"node_id"}, (
        "ноду даёт identity запроса; идентификатор в теле — необязательная сверка"
    )
    assert fields(schema, "ReportLine") == {"user_id", "uplink_bytes", "downlink_bytes"}
    assert required(schema, "ReportLine") == fields(schema, "ReportLine")
    assert fields(schema, "OnlineIp") == {"user_id", "ip", "last_seen"}
    assert required(schema, "OnlineIp") == fields(schema, "OnlineIp")
    assert fields(schema, "Accept") == {"last_accepted_seq", "duplicate"}
    assert required(schema, "Accept") == fields(schema, "Accept")
    assert fields(schema, "QuotaRequestIn") == {"user_id", "consumed_bytes"}
    assert required(schema, "QuotaRequestIn") == fields(schema, "QuotaRequestIn")
    assert fields(schema, "QuotaGrant") == {"quota_grant_bytes", "issued_seq"}
    assert required(schema, "QuotaGrant") == fields(schema, "QuotaGrant")
    for name in ("ReportIn", "ReportLine", "OnlineIp", "QuotaRequestIn"):
        assert schema["components"]["schemas"][name]["additionalProperties"] is False, name
    report = schema["components"]["schemas"]["ReportIn"]["properties"]
    assert report["lines"]["maxItems"] == 500, "потолок одной части — из бюджета Н-4 при двух сразу"
    assert report["online_ips"]["maxItems"] == 2500
    assert report["report_seq"]["minimum"] == 1, "нумерация с единицы: 0 — «ничего не принято»"
    assert (report["parts_total"]["minimum"], report["parts_total"]["maximum"]) == (1, 1000)
    assert (
        schema["components"]["schemas"]["Accept"]["properties"]["last_accepted_seq"]["minimum"] == 0
    )
    assert "Settings" not in schema["components"]["schemas"], "настройки не публикуются (WI-6)"


async def test_a_quota_request_is_answered_with_the_stub_grant() -> None:
    """Грант заглушки — тот же 1 ГиБ, что поток состава кладёт в `quota_grant_bytes`: две
    заглушки одного правила R-26 берут одну константу домена (учёт зависит от домена, не
    наоборот)."""
    assert composition.STUB_QUOTA_GRANT_BYTES == 1073741824
    granted = [
        row.quota_grant_bytes
        for row in composition.stub_rows(STUB_NODE_ID)
        if row.state == "active"
    ]
    assert granted and set(granted) == {composition.STUB_QUOTA_GRANT_BYTES}, (
        "поток состава берёт ту же константу"
    )
    async with agent() as client:
        response = await client.post(QUOTA, json=VALID_QUOTA, headers=HEADERS)
        assert response.status_code == 200, response.text
        assert response.json() == {"quota_grant_bytes": 1073741824, "issued_seq": 1}


async def test_every_operation_of_the_section_needs_identity_and_an_approved_node(
    app_client: httpx.AsyncClient,
) -> None:
    """Свойство раздела утверждается по инвентарю, а не по списку в тесте: каждая операция
    `/agent/v1` из OpenAPI, кроме enrollment, без признаков identity отвечает 401, а
    неподтверждённой ноде — 403. Операция, добавленная позже, попадает под страж сама."""
    schema = (await app_client.get("/openapi.json")).json()
    inventory = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        if path.startswith("/agent/v1/") and path != "/agent/v1/enroll"
        for method in operations
    }
    assert inventory == {
        ("GET", "/agent/v1/state"),
        ("POST", "/agent/v1/ack"),
        ("POST", "/agent/v1/heartbeat"),
        ("POST", "/agent/v1/metrics"),
        ("POST", "/agent/v1/commands/{command_id}/result"),
        ("POST", REPORTS),
        ("POST", QUOTA),
    }, "новая операция раздела добавляется сюда осознанно"
    async with agent() as anonymous, agent("pending") as pending:
        for method, path in sorted(inventory):
            concrete = path.replace("{command_id}", str(uuid.uuid4()))
            body: dict[str, Any] | None = {} if method == "POST" else None
            refused = await anonymous.request(
                method, concrete, json=body, headers={"X-Agent-Version": "0.1.0"}
            )
            assert refused.status_code == 401, (path, refused.text)
            assert refused.json()["error"]["code"] == "unauthenticated", path
            gated = await pending.request(method, concrete, json=body, headers=HEADERS)
            assert gated.status_code == 403, (path, gated.text)
            assert gated.json()["error"]["code"] == "node_not_approved", path


@pytest.mark.parametrize("status", sorted(STREAM_GATE))
async def test_the_gate_decides_reports_and_grants_by_status(status: NodeStatus) -> None:
    """Вентиль §4.6 на обеих операциях, по всем статусам. Отчёт: `pending` — 403, любой другой
    статус — приём (столбец «Учитывается трафик» — `provisioning` нет, `offline`/`disabled`/
    `suspended` из буфера — правило 001.34: отчёт принимается, чтобы нода освободила буфер, и
    тарифицируется по статусу). Грант — разрешение обслуживать пользователя автономно: выдаётся
    только там, где поток состава открыт целиком; ноде с одними отзывами (`disabled`,
    `suspended`) и без состава (`provisioning`) — 403 `grant_unavailable` с `Retry-After`."""
    gate = STREAM_GATE[status]
    async with agent(status) as client:
        report = await client.post(REPORTS, json=VALID_REPORT, headers=HEADERS)
        assert report.status_code == (403 if gate.refused else 200), (status, report.text)
        grant = await client.post(QUOTA, json=VALID_QUOTA, headers=HEADERS)
        if gate.refused:
            assert grant.status_code == 403 and grant.json()["error"]["code"] == "node_not_approved"
        elif gate.composition != "all":
            assert grant.status_code == 403, (status, grant.text)
            assert grant.json()["error"]["code"] == "grant_unavailable"
            assert grant.json()["error"]["details"] == {"node_status": status}
            assert grant.headers["retry-after"] == "300"
        else:
            assert grant.status_code == 200, (status, grant.text)
    assert quota.RETRY_AFTER_GRANT == 300


def without(body: dict[str, Any], key: str) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k != key}


BAD_REPORTS: list[tuple[str, dict[str, Any]]] = [
    (f"без {key}", without(VALID_REPORT, key)) for key in VALID_REPORT
] + [
    ("лишнее поле", {**VALID_REPORT, "x": 1}),
    ("лишнее поле строки", {**VALID_REPORT, "lines": [{**VALID_REPORT["lines"][0], "x": 1}]}),
    (
        "лишнее поле адреса",
        {**VALID_REPORT, "online_ips": [{**VALID_REPORT["online_ips"][0], "x": 1}]},
    ),
    ("node_id не uuid", {**VALID_REPORT, "node_id": "not-a-uuid"}),
    ("counter_epoch не uuid", {**VALID_REPORT, "counter_epoch": "epoch-1"}),
    ("report_seq 0", {**VALID_REPORT, "report_seq": 0}),
    ("report_seq отрицательный", {**VALID_REPORT, "report_seq": -1}),
    ("report_seq шире bigint", {**VALID_REPORT, "report_seq": 2**63}),
    ("period_end == period_start", {**VALID_REPORT, "period_end": "2026-09-10T12:00:00Z"}),
    ("period_end < period_start", {**VALID_REPORT, "period_end": "2026-09-10T11:59:00Z"}),
    ("period_start без пояса", {**VALID_REPORT, "period_start": "2026-09-10T12:00:00"}),
    ("period_end без пояса", {**VALID_REPORT, "period_end": "2026-09-10T12:01:00"}),
    (
        "uplink отрицательный",
        {**VALID_REPORT, "lines": [{**VALID_REPORT["lines"][0], "uplink_bytes": -1}]},
    ),
    (
        "downlink шире bigint",
        {**VALID_REPORT, "lines": [{**VALID_REPORT["lines"][0], "downlink_bytes": 2**63}]},
    ),
    ("node_rx отрицательный", {**VALID_REPORT, "node_rx_bytes": -1}),
    ("node_tx шире bigint", {**VALID_REPORT, "node_tx_bytes": 2**63}),
    (
        "user_id строки не uuid",
        {**VALID_REPORT, "lines": [{**VALID_REPORT["lines"][0], "user_id": "u1"}]},
    ),
    (
        "адрес не адрес",
        {**VALID_REPORT, "online_ips": [{**VALID_REPORT["online_ips"][0], "ip": "999.1.1.1"}]},
    ),
    (
        "last_seen без пояса",
        {
            **VALID_REPORT,
            "online_ips": [{**VALID_REPORT["online_ips"][0], "last_seen": "2026-09-10T12:00:57"}],
        },
    ),
    (
        "пользователь дважды в строках",
        {**VALID_REPORT, "lines": [VALID_REPORT["lines"][0], VALID_REPORT["lines"][0]]},
    ),
    (
        "пара пользователь × адрес дважды",
        {**VALID_REPORT, "online_ips": [VALID_REPORT["online_ips"][0]] * 2},
    ),
    ("lines не список", {**VALID_REPORT, "lines": {}}),
    ("период дольше часа", {**VALID_REPORT, "period_end": "2026-09-10T13:00:00.000001Z"}),
    ("parts_total 0", {**VALID_REPORT, "parts_total": 0}),
    ("parts_total сверх предела", {**VALID_REPORT, "parts_total": 1001}),
    ("parts_total булево", {**VALID_REPORT, "parts_total": True}),
    # Число — та же секунда, что и в тексте (2026-09-10T12:00:00Z): отказать обязано правило
    # формы, а не порядок или длина периода — иначе посадка на правило формы осталась бы зелёной.
    ("period_start числом", {**VALID_REPORT, "period_start": 1789041600}),
    ("period_start строкой-числом", {**VALID_REPORT, "period_start": "1789041600"}),
    ("period_start с пробелом вместо T", {**VALID_REPORT, "period_start": "2026-09-10 12:00:00Z"}),
    ("period_start без смещения", {**VALID_REPORT, "period_start": "2026-09-10T12:00:00"}),
    (
        "period_end с семью знаками дробной части",
        {**VALID_REPORT, "period_end": "2026-09-10T12:01:00.1234567Z"},
    ),
    (
        "period_end со смещением без двоеточия",
        {**VALID_REPORT, "period_end": "2026-09-10T12:01:00+0000"},
    ),
    ("report_seq булево", {**VALID_REPORT, "report_seq": True}),
    (
        "uplink булево",
        {**VALID_REPORT, "lines": [{**VALID_REPORT["lines"][0], "uplink_bytes": True}]},
    ),
    (
        "period_start длиннее 32 символов",
        {**VALID_REPORT, "period_start": "2026-09-10T12:00:00.0000001+00:00"},
    ),
    (
        "period_end длиннее 32 символов",
        {**VALID_REPORT, "period_end": "2026-09-10T12:01:00.1234567+00:00"},
    ),
    (
        "uuid в форме urn",
        {**VALID_REPORT, "counter_epoch": "urn:uuid:00000000-0000-7000-8000-0000000000e1"},
    ),
    (
        "uuid без дефисов (32 hex — pydantic сам по себе принял бы)",
        {
            **VALID_REPORT,
            "lines": [{**VALID_REPORT["lines"][0], "user_id": USER_A.replace("-", "")}],
        },
    ),
    (
        "uuid с дефисами не на месте (36 символов)",
        {**VALID_REPORT, "counter_epoch": "----" + USER_A.replace("-", "")},
    ),
    (
        "адрес с zone id",
        {**VALID_REPORT, "online_ips": [{**VALID_REPORT["online_ips"][0], "ip": "fe80::1%eth0"}]},
    ),
    (
        "адрес числом",
        {**VALID_REPORT, "online_ips": [{**VALID_REPORT["online_ips"][0], "ip": 3232235777}]},
    ),
    (
        "last_seen числом",
        {
            **VALID_REPORT,
            "online_ips": [{**VALID_REPORT["online_ips"][0], "last_seen": 1789041657}],
        },
    ),
]


@pytest.mark.parametrize(("label", "body"), BAD_REPORTS, ids=[label for label, _ in BAD_REPORTS])
async def test_the_report_shape_is_validated(label: str, body: dict[str, Any]) -> None:
    """Форма отчёта — ключи и границы таблиц §4.2.5: каждое обязательное поле, закрытость для
    лишних полей на всех трёх уровнях, ширина `bigint`, нумерация отчётов с единицы, порядок
    границ периода и период не длиннее часа, метки времени только текстом со смещением и не
    шире `TIMESTAMP_MAX_CHARS`, UUID только в канонической записи, адреса только текстом и без
    zone id (`inet` его не хранит), уникальность пользователя в строках и пары «пользователь ×
    адрес» в адресах (первичные ключи `traffic_lines` и `user_online_ips`: о них иначе
    споткнулась бы вставка 001.77 — ошибкой базы вместо 422)."""
    async with agent() as client:
        response = await client.post(REPORTS, json=body, headers=HEADERS)
    assert response.status_code == 422, (label, response.text)
    assert response.json()["error"]["code"] == "validation_error", label


# Каждое числовое поле обеих операций с его границами: значение на единицу ниже нижней и на
# единицу выше верхней. Ширина — колонки `bigint` §4.2.5; `report_seq` и `parts_total` — с
# единицы, `parts_total` — не больше `REPORT_MAX_PARTS`.
BOUNDED: list[tuple[str, str, int, int]] = [
    (REPORTS, "report_seq", 0, 2**63),
    (REPORTS, "parts_total", 0, 1_001),
    (REPORTS, "node_rx_bytes", -1, 2**63),
    (REPORTS, "node_tx_bytes", -1, 2**63),
    (REPORTS, "lines.uplink_bytes", -1, 2**63),
    (REPORTS, "lines.downlink_bytes", -1, 2**63),
    (QUOTA, "consumed_bytes", -1, 2**63),
]


@pytest.mark.parametrize(
    ("path", "field", "value"),
    [(path, field, value) for path, field, low, high in BOUNDED for value in (low, high)],
)
async def test_every_bounded_field_is_bounded_on_both_sides(
    path: str, field: str, value: int
) -> None:
    """Каждое поле с объявленной границей проверяется с обеих сторон — по перечню полей с их
    собственными границами, а не по одному примеру на класс (`developer-guidelines` §6.3 п. 7):
    у соседа границу можно снять молча."""
    if path == QUOTA:
        body: dict[str, Any] = {**VALID_QUOTA, field: value}
    elif field.startswith("lines."):
        body = {**VALID_REPORT, "lines": [{**VALID_REPORT["lines"][0], field[6:]: value}]}
    else:
        body = {**VALID_REPORT, field: value}
    async with agent() as client:
        response = await client.post(path, json=body, headers=HEADERS)
    assert response.status_code == 422, (field, value, response.text)
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("без user_id", {"consumed_bytes": 1}),
        ("без consumed_bytes", {"user_id": USER_A}),
        ("лишнее поле", {**VALID_QUOTA, "x": 1}),
        ("user_id не uuid", {**VALID_QUOTA, "user_id": "u1"}),
        ("user_id в форме urn", {**VALID_QUOTA, "user_id": "urn:uuid:" + USER_A}),
        ("user_id hex без дефисов", {**VALID_QUOTA, "user_id": USER_A.replace("-", "")}),
        ("consumed_bytes булево", {**VALID_QUOTA, "consumed_bytes": True}),
        ("пусто", {}),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
async def test_the_quota_request_shape_is_validated(label: str, body: dict[str, Any]) -> None:
    async with agent() as client:
        response = await client.post(QUOTA, json=body, headers=HEADERS)
    assert response.status_code == 422, (label, response.text)
    assert response.json()["error"]["code"] == "validation_error", label


async def test_the_report_boundaries_are_accepted() -> None:
    """Сами границы принимаются: страж меряет предел, а не «что-то большое». Пустые списки —
    нода без пользователей всё равно отчитывается счётчиками интерфейса; один адрес у двух
    пользователей (NAT) и два адреса у одного (смена сети) — штатные случаи §4.13."""
    top = 2**63 - 1
    widest_line = {"user_id": USER_B, "uplink_bytes": top, "downlink_bytes": top}
    shared = [
        {"user_id": USER_A, "ip": "198.51.100.23", "last_seen": "2026-09-10T12:00:57+03:00"},
        {"user_id": USER_B, "ip": "198.51.100.23", "last_seen": "2026-09-10T12:00:58Z"},
    ]
    async with agent() as client:
        for body in (
            {**VALID_REPORT, "lines": [], "online_ips": []},
            {**VALID_REPORT, "report_seq": 1},
            {**VALID_REPORT, "report_seq": top, "node_rx_bytes": top, "node_tx_bytes": top},
            {**VALID_REPORT, "lines": [VALID_REPORT["lines"][0], widest_line]},
            {**VALID_REPORT, "online_ips": shared},
            {**VALID_REPORT, "period_end": "2026-09-10T12:00:00.000001Z"},
            {**VALID_REPORT, "period_end": "2026-09-10T13:00:00Z"},  # ровно час — принят
            {**VALID_REPORT, "parts_total": 1000},  # предел числа частей — принят
            {  # дельта после перерыва снятия счётчиков — интервал длиннее штатных 60 с
                **VALID_REPORT,
                "period_start": "2026-09-10T12:00:00Z",
                "period_end": "2026-09-10T12:37:00Z",
            },
            {  # интервал через границу часа принимается: он целиком относится к часу начала
                **VALID_REPORT,
                "period_start": "2026-09-10T12:59:30Z",
                "period_end": "2026-09-10T13:00:30Z",
            },
            {**VALID_REPORT, "period_end": "2026-09-10T12:01:00.123456+00:00"},  # 32 символа
            {
                **VALID_REPORT,
                "online_ips": [{**VALID_REPORT["online_ips"][0], "ip": "::ffff:192.0.2.1"}],
            },
            {**VALID_REPORT, "node_id": None},  # Go шлёт null для незаполненного поля
            {
                **VALID_REPORT,
                # один пользователь, одно число адреса в двух семействах — inet их различает
                "online_ips": [
                    {**VALID_REPORT["online_ips"][0], "ip": "192.0.2.1"},
                    {**VALID_REPORT["online_ips"][0], "ip": "::192.0.2.1"},
                ],
            },
        ):
            response = await client.post(REPORTS, json=body, headers=HEADERS)
            assert response.status_code == 200, (str(body)[:80], response.text)


async def test_the_size_limits_are_literal_and_enforced() -> None:
    """Пределы размера — литералами, константы закреплены отдельно (иначе страж рос бы вместе с
    тем, что проверяет): на один сверх предела — 422, ровно предел строк — принят. Предел адресов
    целиком проверяется моделью в `tests/unit/accounting/test_report_schema.py`."""
    assert (REPORT_MAX_LINES, REPORT_MAX_ONLINE_IPS) == (500, 2_500)
    async with agent() as client:
        too_many_lines = await client.post(REPORTS, json=report_with(501, 0), headers=HEADERS)
        assert too_many_lines.status_code == 422, too_many_lines.text[:300]
        assert too_many_lines.json()["error"]["code"] == "validation_error"
        too_many_ips = await client.post(REPORTS, json=report_with(2, 2_501), headers=HEADERS)
        assert too_many_ips.status_code == 422, too_many_ips.text[:300]
        widest_body = {**widest_report(), "node_id": str(STUB_NODE_ID)}  # та же ширина поля
        widest = await client.post(REPORTS, json=widest_body, headers=HEADERS)
        assert widest.status_code == 200, "самое большое допустимое тело принимается через HTTP"


async def test_a_validation_answer_is_bounded_however_bad_the_body_is() -> None:
    """Ответ 422 — не эхо тела: pydantic даёт по ошибке на каждый негодный элемент и каждое
    лишнее поле, и без потолка отчёт в тысячи негодных строк получал бы ответ того же порядка,
    собранный в том же цикле событий. Потолок — литералом; остаток сообщается числом."""
    assert (VALIDATION_ERRORS_MAX, VALIDATION_LOC_MAX_CHARS) == (50, 64)
    bad_lines = report_with(500, 0, bad_lines=True)
    # 8 000 лишних ключей проходят проверку формы по байтам (запятых меньше, чем у самого
    # большого тела) и упираются в потолок числа ошибок; шире — отсекает проверка формы.
    extra_keys = {**VALID_REPORT, **{f"x{n}": 0 for n in range(8_000)}}
    # Имя лишнего поля — текст клиента: пятьдесят ключей по десять тысяч символов (полмегабайта —
    # под пределом прокси 1 МиБ) остаются под пределом числа ошибок и без обрезки ``loc``
    # вернулись бы эхом полумегабайтным ответом.
    long_keys = {**VALID_REPORT, **{"k" * 10_000 + str(n): 0 for n in range(50)}}
    assert len(json.dumps(long_keys)) < 1024**2, "тело достижимо через прокси"
    async with agent() as client:
        for body, expected_total in ((bad_lines, 500), (extra_keys, 8_000), (long_keys, 50)):
            response = await client.post(REPORTS, json=body, headers=HEADERS)
            assert response.status_code == 422, response.text[:200]
            details = response.json()["error"]["details"]
            assert len(details["errors"]) == min(expected_total, 50)
            assert details.get("truncated", 0) == max(expected_total - 50, 0)
            assert len(response.content) < 10_000, (
                "ответ на негодное тело — килобайты, не мегабайты"
            )
            for error in details["errors"]:
                assert all(len(part) <= 64 for part in error["loc"] if isinstance(part, str))
        one = await client.post(REPORTS, json={**VALID_REPORT, "report_seq": 0}, headers=HEADERS)
        assert "truncated" not in one.json()["error"]["details"], "одна ошибка — без остатка"


async def test_a_body_wider_than_the_widest_report_is_refused_before_parsing() -> None:
    """Путь отказа не дороже пути приёма: тело с числом объектов, массивов или членов больше,
    чем у самого большого допустимого, недопустимо заведомо, и маршрут отвергает его до
    разбора JSON (иначе 1 МиБ лишних ключей — 131 000 ошибок pydantic до усечения). Пределы —
    ровно счётчики самого большого тела (равенством) и литералами."""
    widest = json.dumps(widest_report()).encode()
    assert (BODY_MAX_OPENERS, BODY_MAX_COMMAS) == (3_003, 9_007)
    assert widest.count(b"[") + widest.count(b"{") == BODY_MAX_OPENERS
    assert widest.count(b",") == BODY_MAX_COMMAS
    too_many_objects = b'{"lines": [' + b"{}," * BODY_MAX_OPENERS + b"{}]}"
    too_many_members = b'{"lines": [' + b"1," * (BODY_MAX_COMMAS + 1) + b"1]}"
    too_deep = b"[" * 100_000 + b"]" * 100_000
    async with agent() as client:
        for body in (too_many_objects, too_many_members, too_deep):
            response = await client.post(REPORTS, content=body, headers=HEADERS)
            assert response.status_code == 422, response.text[:200]
            details = response.json()["error"]["details"]
            assert [e["type"] for e in details["errors"]] == ["too_wide"], details
            assert "truncated" not in details
        # Ровно на пределе форма проходит, и отказывает уже модель — одной ошибкой `too_long`
        # (список длиннее предела отвергается целиком, не поэлементно), без остатка.
        at_limit = json.dumps({**VALID_REPORT, "lines": [{}] * 3_000, "online_ips": []}).encode()
        assert at_limit.count(b"[") + at_limit.count(b"{") == BODY_MAX_OPENERS
        json_headers = {**HEADERS, "Content-Type": "application/json"}  # content= без типа — байты
        response = await client.post(REPORTS, content=at_limit, headers=json_headers)
        assert response.status_code == 422
        details = response.json()["error"]["details"]
        assert [e["type"] for e in details["errors"]] == ["too_long"], details
        assert "truncated" not in details


async def test_the_worst_body_that_passes_the_width_check_is_bounded_too() -> None:
    """Худшее тело, которое проверка формы по байтам пропускает: столько пустых объектов, сколько
    разрешают пределы списков — по три ошибки `missing` на каждый, 9 000 ошибок pydantic, — и
    ответ всё равно 50 ошибок с остатком числом (на стенде — 11 мс сквозного времени). Путь
    отказа после проверки формы ограничен пределами модели, а не только счётчиками байтов."""
    worst = {
        **VALID_REPORT,
        "lines": [{}] * REPORT_MAX_LINES,
        "online_ips": [{}] * REPORT_MAX_ONLINE_IPS,
    }
    async with agent() as client:
        response = await client.post(REPORTS, json=worst, headers=HEADERS)
    assert response.status_code == 422, response.text[:200]
    details = response.json()["error"]["details"]
    assert len(details["errors"]) == VALIDATION_ERRORS_MAX
    assert details["truncated"] == 3 * (REPORT_MAX_LINES + REPORT_MAX_ONLINE_IPS) - 50
    assert {e["type"] for e in details["errors"]} == {"missing"}
    assert len(response.content) < 10_000


async def test_a_body_that_cannot_be_parsed_is_a_unified_400() -> None:
    """Негодный UTF-8 и число длиннее 4 300 цифр (`ValueError` разбора, не `JSONDecodeError`)
    FastAPI отвергает до проверки модели кодом 400: ответ — единый формат с русским текстом, а
    не английская строка фреймворка и не 500; код объявлен у всех операций раздела с телом. До
    разбора такое число доходит только у модели со строковыми полями (heartbeat): у отчёта и
    гранта серия цифр длиннее `bigint` — `too_wide` до разбора (закрытие 001.33). Синтаксически
    негодный JSON — другая ветка FastAPI: 422
    `json_invalid` единого формата (агент чинит сериализатор, не делит отчёт — README). Глубина
    вложенности предела рекурсии не достигает: 3 000 уровней на отчёте проходят проверку формы
    (не больше `BODY_MAX_OPENERS` скобок), разбираются без `RecursionError` и получают 422
    модели как не-объект; на гранте та же глубина шире формы (одна скобка) — `too_wide` до
    разбора."""
    unified = {
        "error": {"code": "bad_request", "message": "тело запроса не разобрано", "details": {}}
    }
    long_number = b'{"a": ' + b"9" * 4_400 + b"}"
    deep = b"[" * BODY_MAX_OPENERS + b"]" * BODY_MAX_OPENERS
    async with agent() as client:
        for path in (REPORTS, QUOTA):
            response = await client.post(
                path,
                content=b'{"a": "\xff"}',
                headers={**HEADERS, "Content-Type": "application/json"},
            )
            assert response.status_code == 400, (path, response.text[:200])
            assert response.json() == unified, path
            response = await client.post(
                path, content=long_number, headers={**HEADERS, "Content-Type": "application/json"}
            )
            assert response.status_code == 422, (path, response.text[:200])
            errors = response.json()["error"]["details"]["errors"]
            assert [e["type"] for e in errors] == ["too_wide"], (path, errors)
            broken = await client.post(
                path, content=b'{"a":', headers={**HEADERS, "Content-Type": "application/json"}
            )
            assert broken.status_code == 422, (path, broken.text[:200])
            errors = broken.json()["error"]["details"]["errors"]
            assert [e["type"] for e in errors] == ["json_invalid"], (path, errors)
        for path, expected in ((REPORTS, "model_attributes_type"), (QUOTA, "too_wide")):
            response = await client.post(
                path, content=deep, headers={**HEADERS, "Content-Type": "application/json"}
            )
            assert response.status_code == 422, (path, response.text[:200])
            errors = response.json()["error"]["details"]["errors"]
            assert [e["type"] for e in errors] == [expected], (path, errors)
        response = await client.post(
            "/agent/v1/heartbeat",
            content=long_number,
            headers={**HEADERS, "Content-Type": "application/json"},
        )
        assert response.status_code == 400, response.text[:200]
        assert response.json() == unified


async def test_a_body_without_the_json_content_type_is_refused_by_the_model() -> None:
    """Без `Content-Type: application/json` FastAPI не разбирает тело как JSON — модель
    получает не словарь и отвечает 422 единого формата (README: тип содержимого обязателен);
    проверка формы по байтам такому телу не мешает — оно в пределах формы. Оба случая: чужой
    тип и отсутствие заголовка вовсе — второй держится на `strict_content_type`, объявленном
    у роутеров явно (умолчание FastAPI, снятое одним словом, разбирало бы тело без типа)."""
    async with agent() as client:
        for path, body in ((REPORTS, VALID_REPORT), (QUOTA, VALID_QUOTA)):
            for headers in (
                {**HEADERS, "Content-Type": "text/plain"},
                {k: v for k, v in HEADERS.items() if k.lower() != "content-type"},
            ):
                response = await client.post(
                    path, content=json.dumps(body).encode(), headers=headers
                )
                assert response.status_code == 422, (path, headers, response.text[:200])
                errors = response.json()["error"]["details"]["errors"]
                assert [e["type"] for e in errors] == ["model_attributes_type"], (path, errors)


async def test_free_text_of_a_command_result_is_not_mistaken_for_shape() -> None:
    """Текст ошибки исполнения команды содержит что угодно, в том числе запятые и скобки
    («xray: config error at [inbounds][0], tag not unique»): предел формы результата команды
    включает бюджет свободного текста (`error` до 2 000 символов), и такое тело доходит до
    модели и принимается — 204. Тело шире бюджета (2 100 ключей) по-прежнему `too_wide`."""
    async with agent() as client:
        url = f"/agent/v1/commands/{uuid.uuid4()}/result"
        body = {"status": "failed", "error": "xray: config error at [inbounds][0], {tag}, dup"}
        response = await client.post(url, json=body, headers=HEADERS)
        assert response.status_code == 204, response.text[:200]


async def test_a_number_longer_than_bigint_is_refused_before_parsing() -> None:
    """Модель отчёта без строковых полей несёт предел серии цифр — 19 знаков `bigint`: число из
    двадцати цифр — `too_wide` до разбора (каждое такое число иначе превращалось бы в `int` до
    проверки `le`, и тело из сотен чисел по 4 300 цифр стоило вдвое дороже законной части);
    ровно `INT8_MAX` (19 знаков) проходит форму и принимается."""
    async with agent() as client:
        response = await client.post(
            REPORTS, json={**VALID_REPORT, "node_rx_bytes": INT8_MAX}, headers=HEADERS
        )
        assert response.status_code == 200, response.text[:200]
        too_long = json.dumps({**VALID_REPORT, "node_rx_bytes": 0}).replace(
            '"node_rx_bytes": 0', '"node_rx_bytes": 92233720368547758070'
        )
        assert "92233720368547758070" in too_long
        response = await client.post(
            REPORTS,
            content=too_long.encode(),
            headers={**HEADERS, "Content-Type": "application/json"},
        )
        assert response.status_code == 422, response.text[:200]
        errors = response.json()["error"]["details"]["errors"]
        assert [e["type"] for e in errors] == ["too_wide"], errors
        assert "длинное число" in errors[0]["msg"], errors


async def test_every_operation_is_bounded_by_the_shape_of_its_own_model(
    app_client: httpx.AsyncClient,
) -> None:
    """Проверка формы по байтам стоит на каждой операции раздела с телом, и пределы у каждой —
    из её модели (раунд 8): heartbeat из пяти полей отвергает тело с пятью запятыми до разбора
    (`too_wide`, до identity — так путь отказа стоит `bytes.count`, а не тысячи ошибок
    pydantic). У плоской модели предел запятых равен числу полей минус один, поэтому любой
    лишний ключ сверх полного набора — `too_wide` до разбора; до модели доходит тело в
    пределах формы — лишний ключ вместо обязательного (`extra_forbidden` и `missing`).
    Исключение — результат команды: его `error` — свободный текст до 2 000 символов, и предел
    формы включает этот бюджет (2 001 запятая), так что «широким» его делает лишь тело шире
    бюджета. По инвентарю OpenAPI: новая операция с телом без проверки формы красила бы этот
    тест. Запрос гранта, «широкий» по объектам, — тоже `too_wide`."""
    schema = (await app_client.get("/openapi.json")).json()
    posts = sorted(
        path
        for path, ops in schema["paths"].items()
        if path.startswith("/agent/v1/") and "post" in ops
    )
    assert len(posts) == 7, "все операции раздела с телом"
    json_headers = {**HEADERS, "Content-Type": "application/json"}
    async with agent() as client:
        for path in posts:
            url = path.replace("{command_id}", str(uuid.uuid4()))
            wide = b'{"k": [' + b"{}," * BODY_MAX_OPENERS + b"{}]}"
            response = await client.post(url, content=wide, headers=json_headers)
            assert response.status_code == 422, (path, response.text[:200])
            errors = response.json()["error"]["details"]["errors"]
            assert [e["type"] for e in errors] == ["too_wide"], (path, errors)
            # Больше запятых, чем полей у любой модели раздела, кроме отчёта (у него свои сотни):
            # девять пар ключей в плоском объекте — форма нарушена, разбора не было.
            if path != REPORTS:
                width = 2_100 if path.endswith("/result") else 10
                keys = b"{" + b",".join(b'"k%d":0' % n for n in range(width)) + b"}"
                response = await client.post(url, content=keys, headers=json_headers)
                assert response.status_code == 422, (path, response.text[:200])
                errors = response.json()["error"]["details"]["errors"]
                assert [e["type"] for e in errors] == ["too_wide"], (path, errors)
        one_extra = {**VALID_QUOTA, "x": 0}
        response = await client.post(QUOTA, json=one_extra, headers=HEADERS)
        assert response.status_code == 422, response.text[:200]
        errors = response.json()["error"]["details"]["errors"]
        assert [e["type"] for e in errors] == ["too_wide"], errors
        swapped = {"user_id": USER_A, "x": 0}
        response = await client.post(QUOTA, json=swapped, headers=HEADERS)
        assert response.status_code == 422, response.text[:200]
        errors = response.json()["error"]["details"]["errors"]
        assert sorted(e["type"] for e in errors) == ["extra_forbidden", "missing"], errors


async def _post_with_broken_upload(
    app: Any, path: str, *, failure: str = "disconnect"
) -> tuple[int, dict[str, Any]]:
    """Запрос, чей клиент объявил тело и отключился, не дослав его: ASGI-сообщение
    `http.disconnect` вместо тела — так его видит приложение за uvicorn; `failure="raise"` —
    сбой самого чтения (исключение из `receive`), как при ошибке протокола у сервера."""
    headers = {**HEADERS, "content-type": "application/json", "content-length": "5000"}
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 1),
        "server": ("control-plane", 80),
    }
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if failure == "raise":
            raise OSError("чтение тела оборвано сервером")
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(body)


async def test_a_broken_upload_is_a_unified_400_on_every_operation(
    app_client: httpx.AsyncClient,
) -> None:
    """Заливка, оборванная до конца тела, — отказ клиента, не сбой сервера: по инвентарю
    OpenAPI каждая операция раздела с телом (включая enrollment) отвечает 400 единого формата с
    тем же текстом, что у неразобранного тела (FastAPI переводит обрыв в свой 400, маршрут
    отчёта читает тело раньше FastAPI и переводит сам — любой сбой чтения, не только
    `ClientDisconnect`: правило то же, что у FastAPI). Клиент ответа уже не получает, но сервер
    не пишет трассировку и не считает 500."""
    schema = (await app_client.get("/openapi.json")).json()
    posts = sorted(
        path
        for path, ops in schema["paths"].items()
        if path.startswith("/agent/v1/") and "post" in ops
    )
    assert len(posts) == 7, "все операции раздела с телом"
    app = create_app()
    app.dependency_overrides[db_pool] = NoDatabase
    app.dependency_overrides[current_node] = node_as()
    unified = {
        "error": {"code": "bad_request", "message": "тело запроса не разобрано", "details": {}}
    }
    for path in posts:
        status, body = await _post_with_broken_upload(
            app, path.replace("{command_id}", str(uuid.uuid4()))
        )
        assert (status, body) == (400, unified), path
    for path in (REPORTS, QUOTA):
        status, body = await _post_with_broken_upload(app, path, failure="raise")
        assert (status, body) == (400, unified), (path, "сбой чтения — тот же 400")


class _SubscriptionProbe(BaseModel):
    """Тело пробного маршрута под /s/: одно поле с верхней границей — пределы формы выводятся из
    модели (``BoundedBodyRoute``), а класс на уровне модуля, чтобы FastAPI разрешил аннотацию."""

    value: int = Field(ge=0, le=9)


async def test_a_broken_upload_logs_the_path_without_a_subscription_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Н-25: сбой чтения тела пишет в журнал путь запроса — через ``loggable_path``, как
    обработчики 500 и 503. Маршрутов с телом под /s/ сегодня нет, но строка журнала не должна
    зависеть от того, где маршрут (роаст 001.25, раунд 7): пробный маршрут раздела с телом — под
    ``/s/{value}/``, чтение тела обрывается исключением."""
    app = create_app()
    router = APIRouter(route_class=BoundedBodyRoute)

    @router.post("/s/{value}/body")
    async def probe(value: str, body: _SubscriptionProbe) -> None:
        return None

    app.include_router(router)
    token = f"probe-{uuid.uuid4().hex}"
    with caplog.at_level(logging.WARNING, logger="app.agent_api.body"):
        status, _ = await _post_with_broken_upload(app, f"/s/{token}/body", failure="raise")
    assert status == 400
    logged = "\n".join(r.getMessage() for r in caplog.records if r.name == "app.agent_api.body")
    assert "/s/…/body" in logged and token not in logged, logged


async def test_the_width_check_precedes_identity_and_its_limits_are_public() -> None:
    """Проверка формы по байтам стоит до разрешения зависимостей: тело шире самого большого
    допустимого получает 422 `too_wide` и без identity — так дешевле всего для сервера, а
    пределы формы записаны в README контракта и секретом не являются. Тело в пределах формы без
    identity получает 401, как и прежде."""
    wide = b'{"lines": [' + b"{}," * BODY_MAX_OPENERS + b"{}]}"
    anonymous = {"X-Agent-Version": "0.1.0", "Content-Type": "application/json"}
    async with agent() as client:
        refused = await client.post(REPORTS, content=wide, headers=anonymous)
        assert refused.status_code == 422, refused.text[:200]
        assert [e["type"] for e in refused.json()["error"]["details"]["errors"]] == ["too_wide"]
        narrow = await client.post(REPORTS, json=VALID_REPORT, headers=anonymous)
        assert narrow.status_code == 401, narrow.text[:200]


async def test_a_trailing_slash_is_not_redirected_to_the_report_route() -> None:
    """`/agent/v1/reports/` — 404 единого формата, а не редирект на маршрут отчёта: редирект
    (`redirect_slashes`) провёл бы тело мимо `location = /agent/v1/reports` прокси с его
    пределами и зонами в общий `location` с пределом 64 КиБ — и обратно в обработчик отчёта."""
    async with agent() as client:
        response = await client.post(REPORTS + "/", json=VALID_REPORT, headers=HEADERS)
    assert response.status_code == 404, response.text[:200]
    assert response.json()["error"]["code"] == "not_found"


async def test_every_operation_of_the_section_declares_the_proxy_limits(
    app_client: httpx.AsyncClient,
) -> None:
    """Пределы прокси действуют на всех операциях раздела: 429 (пределы соединений и частоты
    — по сертификату и на парк на агентском server, по адресу и на всех клиентов на
    enrollment-server) и 413 (прокси сверяет `Content-Length` независимо от метода — 64 КиБ, у
    отчёта 1 МиБ) объявлены у каждой; 400 (тело не разобрано) и 411 (тело без известной
    длины) — у каждой операции с телом и ни у одной без него (GET состояния их не производит:
    контракт не обещает кодов, которых не бывает). По инвентарю OpenAPI, а не по списку: код, о
    котором вторая сторона узнаёт в бою, — её неизвестный фатальный ответ."""
    schema = (await app_client.get("/openapi.json")).json()
    operations = [
        (path, method, operation)
        for path, methods in schema["paths"].items()
        if path.startswith("/agent/v1/")
        for method, operation in methods.items()
    ]
    assert len(operations) == 8, "восемь операций раздела"
    assert sorted(method for _, method, _ in operations) == ["get"] + ["post"] * 7
    body_codes = {"400", "411"}
    for path, method, operation in operations:
        responses = operation["responses"]
        assert "429" in responses and "прокси" in responses["429"]["description"], (path, method)
        assert "413" in responses, (path, method)
        assert "503" in responses and "прокси" in responses["503"]["description"], (path, method)
        declared = body_codes & set(responses)
        assert declared == (body_codes if method == "post" else set()), (path, method, declared)
        if method == "post":
            assert "не повторять" in responses["411"]["description"], (path, method)


async def test_a_report_for_another_node_is_refused() -> None:
    """Нода недоверенная (§11.3) и отчитывается только за себя: идентификатор другой ноды в
    теле — 403 `node_mismatch` с длинным `Retry-After` (это ошибка конфигурации агента, повтор
    без вмешательства оператора бессмысленен). Нода запроса намеренно не фиксированная: страж
    сверяет запрошенную ноду, а не константу."""
    assert service.RETRY_AFTER_NODE_MISMATCH == 3600
    async with agent(overrides={current_node: node_as(node_id=OTHER_NODE_ID)}) as client:
        foreign = await client.post(
            REPORTS, json={**VALID_REPORT, "node_id": str(STUB_NODE_ID)}, headers=HEADERS
        )
        assert foreign.status_code == 403, foreign.text
        error = foreign.json()["error"]
        assert error["code"] == "node_mismatch"
        assert error["details"] == {"node_id": str(OTHER_NODE_ID)}
        assert foreign.headers["retry-after"] == "3600"
        own = await client.post(
            REPORTS, json={**VALID_REPORT, "node_id": str(OTHER_NODE_ID)}, headers=HEADERS
        )
        assert own.status_code == 200, own.text


class RecordingAccounting(AccountingService):
    """Служба учёта, которая запоминает вызовы вместо записи в базу и отвечает не константой."""

    def __init__(self) -> None:
        super().__init__(None)
        self.reports: list[tuple[uuid.UUID, ReportIn, str]] = []

    async def accept_report(self, node: Node, report: ReportIn, fingerprint: str) -> Accept:
        self.reports.append((node.id, report, fingerprint))
        return Accept(last_accepted_seq=7, duplicate=True)


class RecordingQuota(QuotaService):
    def __init__(self) -> None:
        super().__init__(None)
        self.grants: list[tuple[uuid.UUID, uuid.UUID, int]] = []

    async def grant(self, node: Node, user_id: uuid.UUID, consumed_bytes: int) -> QuotaGrant:
        self.grants.append((user_id, node.id, consumed_bytes))
        return QuotaGrant(quota_grant_bytes=5, issued_seq=3)


async def test_routes_hand_the_parsed_bodies_to_the_domain_and_return_its_answer() -> None:
    """Маршрут, который проверил тело и выбросил его, ответил бы теми же 200: службы
    подменяются записывающими — отчёт доходит до учёта разобранным и вместе с нодой запроса,
    запрос гранта — с пользователем, нодой и заявленным расходом, а ответ маршрута — то, что
    вернула служба, а не константа заглушки."""
    accounting, quotas = RecordingAccounting(), RecordingQuota()
    overrides = {
        current_node: node_as(node_id=OTHER_NODE_ID),
        get_accounting_service: lambda: accounting,
        get_quota_service: lambda: quotas,
    }
    async with agent(overrides=overrides) as client:
        report = await client.post(REPORTS, json=VALID_REPORT, headers=HEADERS)
        assert report.status_code == 200, report.text
        assert report.json() == {"last_accepted_seq": 7, "duplicate": True}
        grant = await client.post(QUOTA, json=VALID_QUOTA, headers=HEADERS)
        assert grant.status_code == 200, grant.text
        assert grant.json() == {"quota_grant_bytes": 5, "issued_seq": 3}

    assert OTHER_NODE_ID != stub_node().id, "страж сверяет запрошенную ноду, а не фиксированную"
    assert [node_id for node_id, _, _ in accounting.reports] == [OTHER_NODE_ID]
    assert accounting.reports[0][2] == FINGERPRINT, "приём перепроверит предъявленную identity"
    parsed = accounting.reports[0][1]
    assert parsed.report_seq == 3 and parsed.node_id is None
    assert [line.user_id for line in parsed.lines] == [uuid.UUID(USER_A), uuid.UUID(USER_B)]
    assert parsed.lines[0].downlink_bytes == 10485760
    assert [str(row.ip) for row in parsed.online_ips] == ["198.51.100.23", "2001:db8::17"]
    assert (parsed.node_rx_bytes, parsed.node_tx_bytes) == (12582912, 2097152)
    assert quotas.grants == [(uuid.UUID(USER_A), OTHER_NODE_ID, 536870912)]


def _api_routes(router: APIRouter) -> list[APIRoute]:
    """Операции роутера вместе с вложенными: FastAPI хранит включённые роутеры узлами со
    ссылкой `original_router`, а не плоским списком."""
    found: list[APIRoute] = []
    for route in router.routes:
        if isinstance(route, APIRoute):
            found.append(route)
        elif (inner := getattr(route, "original_router", None)) is not None:
            found.extend(_api_routes(inner))
    return found


def _dependency_callables(dependant: Dependant) -> set[object]:
    found: set[object] = set()
    for sub in dependant.dependencies:
        found.add(sub.call)
        found |= _dependency_callables(sub)
    return found


async def test_the_pool_and_the_collaborators_reach_the_services_through_the_graph() -> None:
    """Урок WI-6 и контрактные тесты держатся на одном: фабрики служб берут пул зависимостью
    `Depends(db_pool)`, а не вызовом `get_pool()` внутри, — иначе подмена пула в тесте до
    службы не доходила бы, а заглушка, которая пула не касается, скрыла бы это навсегда.
    Свойство утверждается по инвентарю роутера раздела, а не по списку: каждая его операция
    ведёт к `db_pool`; приём отчёта, кроме того, получает службы грантов и лимита адресов из
    графа, а не создаёт их внутри (001.77 вызовет их в своей транзакции)."""
    routes = _api_routes(agent_router)
    assert len(routes) == 8, "восемь операций раздела"
    for route in routes:
        assert db_pool in _dependency_callables(route.dependant), route.path
    reports_route = next(route for route in routes if route.path.endswith("/reports"))
    reached = _dependency_callables(reports_route.dependant)
    assert {get_accounting_service, get_quota_service, get_device_limit_service} <= reached
    # Каждая фабрика объявляет пул сама (не только через коллег): фабрика, взявшая пул вызовом
    # внутри, осталась бы в графе через соседей и обошла бы подмену незаметно.
    for factory in (get_accounting_service, get_quota_service, get_device_limit_service):
        direct = {sub.call for sub in get_dependant(path="/", call=factory).dependencies}
        assert db_pool in direct, factory.__name__
    quotas, limits = QuotaService(NoDatabase()), DeviceLimitService(NoDatabase())
    built = await get_accounting_service(NoDatabase(), quotas, limits)
    assert built._quotas is quotas and built._device_limits is limits, (  # noqa: SLF001
        "фабрика отдаёт приёму коллег из графа, а не создаёт свои"
    )


# --- 001.34: приём на живой базе (R-19, R-21, R-22; UC-04 шаги 5, 6, 9, A1, A3, A4, A5) ---
#
# Нода, её состав и identity — строки живой базы (``tests/e2e/_reporting.py``), раздел опознаёт
# ноду настоящим ``current_node``, приём пишет ``traffic_reports`` и ``report_intervals``. Границы
# периодов — от часов базы (допуск §5.9 меньше шага часов VM стенда).

ALERTS = "app.accounting.service"  # журнал событий для алертов приёма (правила — 001.68)


def refusal(response: httpx.Response) -> tuple[int, str, str | None]:
    """Код ответа, машинный код ошибки и причина из ``details.reason``."""
    error = response.json()["error"]
    return response.status_code, error["code"], error["details"].get("reason")


def alerts(caplog: pytest.LogCaptureFixture, node_id: uuid.UUID, event: str) -> list[str]:
    """Записи ERROR журнала приёма о событии ``event`` этой ноды."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == ALERTS
        and record.levelno == logging.ERROR
        and event in record.getMessage()
        and str(node_id) in record.getMessage()
    ]


async def seconds_from_base(stand: ReportingStand, delta: dt.timedelta) -> float:
    """Смещение от ``base`` стенда до «сейчас базы + ``delta``» в секундах."""
    return (await db_now(stand.conn) + delta - stand.base).total_seconds()


async def test_tc_e2e_01_a_repeated_report_changes_nothing_and_says_duplicate(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """TC-E2E-01, AC-06 (Н-20, UC-04 A1): тот же ключ дважды — 200 оба раза, второй раз с
    `duplicate: true` и той же точкой продолжения; строка отчёта одна, состояние интервала не
    сдвинулось, записей баланса по ключу нет. Свой `node_id` в теле — штатно."""
    async with reporting(pg_dsn, migrate_env) as stand:
        user = stand.users[0]
        body = {
            **stand.part(1, 0, lines=[Line(user, 1000, 2000)], ips=[(user, "198.51.100.7")]),
            "node_id": str(stand.node_id),
            "node_rx_bytes": 5000,
            "node_tx_bytes": 4000,
        }
        first = await stand.post(body)
        assert first.status_code == 200, first.text
        assert first.json() == {"last_accepted_seq": 1, "duplicate": False}
        before = await stand.interval(0)
        assert before is not None
        again = await stand.post(body)
        assert again.status_code == 200, again.text
        assert again.json() == {"last_accepted_seq": 1, "duplicate": True}
        assert await stand.statuses() == ["accepted"]
        assert dict(await stand.interval(0) or {}) == dict(before), "повтор интервал не трогает"
        row = await stand.report_row(1)
        assert row is not None
        assert (row["parts_total"], row["node_rx_bytes"], row["node_tx_bytes"]) == (1, 5000, 4000)
        assert (before["accepted_uplink_bytes"], before["accepted_downlink_bytes"]) == (1000, 2000)
        assert (before["parts_accepted"], before["close_reason"]) == (1, "completed")
        balance = await stand.conn.fetchval(
            "select count(*) from balance_entries where ref_key like $1", f"%{stand.node_id}%"
        )
        assert balance == 0, "повтор баланса не меняет"


async def test_tc_e2e_02_growth_beyond_the_channel_is_held_and_charged_up_to_it(
    pg_dsn: str, migrate_env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """TC-E2E-02 (UC-04 A5, §5.9 «Защита от аномального отчёта»): порог — полоса ноды ×
    длительность интервала на сумму uplink + downlink (байт пользователя проходит порт дважды —
    решение заказчика 2026-10-20), к нарастающей сумме принятых частей интервала. Ровно порог —
    приём; каждое направление в пределе, а сумма сверх — `held_anomaly`: 200 с продвижением
    точки продолжения, отчёт сохранён, к суммам интервала прибавлена только доля в пределах
    порога, разложенная по направлениям пропорционально части (её спишет 001.77), алерт.
    Продолжение, чья сумма с прежней частью переходит порог, — удержание того же интервала.
    Числа — литералами (1 Мбит/с × 60 с = 7 500 000 байт)."""
    async with reporting(pg_dsn, migrate_env, bandwidth_mbps=1) as stand:
        ceiling = anomaly_threshold(1, dt.timedelta(seconds=60))
        assert ceiling == 7_500_000
        a, b = stand.users[:2]
        at_ceiling = await stand.post(stand.part(1, 0, lines=[Line(a, 3_000_000, 4_500_000)]))
        assert at_ceiling.json() == {"last_accepted_seq": 1, "duplicate": False}
        assert (await stand.report_row(1) or {})["status"] == "accepted"
        with caplog.at_level(logging.ERROR, logger=ALERTS):
            over = stand.part(2, 60, lines=[Line(a, 5_000_000, 0), Line(b, 0, 5_000_000)])
            held = await stand.post(over)
        assert held.status_code == 200, held.text
        assert held.json() == {"last_accepted_seq": 2, "duplicate": False}
        assert (await stand.report_row(2) or {})["status"] == "held_anomaly", (
            "по 5 МБ в каждую сторону — в пределе направления, но 10 МБ суммы — сверх"
        )
        interval = await stand.interval(60)
        assert interval is not None and interval["held"] is True
        assert (interval["accepted_uplink_bytes"], interval["accepted_downlink_bytes"]) == (
            3_750_000,
            3_750_000,
        ), "к сумме — доля в пределах порога, а не вся часть"
        assert (interval["close_reason"], interval["closed_at"] is not None) == (
            "completed",
            True,
        ), "удержанный однокусочный закрывается сразу — места среди открытых не занимает"
        assert alerts(caplog, stand.node_id, "held_anomaly"), caplog.text
        replayed = await stand.post(over)
        assert replayed.json() == {"last_accepted_seq": 2, "duplicate": True}, (
            "повтор удержанного — дубликат, как принятого"
        )

        first = stand.part(3, 120, total=2, lines=[Line(a, 4_000_000, 0)])
        assert (await stand.post(first)).status_code == 200
        second = stand.part(4, 120, total=2, lines=[Line(b, 3_000_000, 1_000_000)])
        assert (await stand.post(second)).json() == {"last_accepted_seq": 4, "duplicate": False}
        assert (await stand.report_row(4) or {})["status"] == "held_anomaly"
        split = await stand.interval(120)
        assert split is not None and split["held"] is True
        # Остаток 3 500 000 по части 3 000 000 / 1 000 000: 2 625 000 и 875 000.
        assert (split["accepted_uplink_bytes"], split["accepted_downlink_bytes"]) == (
            6_625_000,
            875_000,
        )
        assert (split["parts_accepted"], split["close_reason"]) == (2, "completed")


async def test_the_threshold_follows_the_length_of_the_interval(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Порог §5.9 — полоса × длительность **этого** интервала, а не штатных 60 с (роаст 001.34,
    раунд 3, C1): при 1 Мбит/с интервал 30 с держит 3 750 000 байт суммы, интервал часа (предел
    периода) — 450 000 000; байт сверх — удержание с долей ровно до порога. Числа — литералами."""
    async with reporting(pg_dsn, migrate_env, bandwidth_mbps=1) as stand:
        user = stand.users[0]
        for seq, start, end, volume, status, charged in (
            (1, -7260, -7230, 3_750_000, "accepted", 3_750_000),
            (2, -7230, -7200, 3_750_001, "held_anomaly", 3_750_000),
            (3, -7200, -3600, 450_000_000, "accepted", 450_000_000),
            (4, -3600, 0, 450_000_001, "held_anomaly", 450_000_000),
        ):
            response = await stand.post(stand.part(seq, start, end, lines=[Line(user, volume)]))
            assert response.json() == {"last_accepted_seq": seq, "duplicate": False}, response.text
            assert (await stand.report_row(seq) or {})["status"] == status, seq
            interval = await stand.interval(start, end)
            assert interval is not None
            assert (interval["accepted_uplink_bytes"], interval["accepted_downlink_bytes"]) == (
                charged,
                0,
            ), seq


async def test_tc_e2e_03_a_period_ahead_of_the_server_clock_is_refused(
    pg_dsn: str, migrate_env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """TC-E2E-03 (UC-04 A4, §5.9 «Синхронизация времени»): `period_end` на 10 с впереди часов
    сервера — 409 `rejected_time` без продвижения точки продолжения и без `Retry-After` (часть не
    повторяется, следующая идёт сразу), отчёт записан со статусом, интервала нет, алерт. Допуск
    5 с откалиброван с обеих сторон литералами: +7 с — отказ, +3 с — приём (по 2 с запаса на шаг
    часов стенда; посадки допуска ×1,5 и ×0,5 краснеют); отказанный интервал
    планку монотонности не поднимает — следующий интервал может его перекрыть."""
    async with reporting(pg_dsn, migrate_env) as stand:
        start = await seconds_from_base(stand, dt.timedelta(seconds=10 - 60))
        with caplog.at_level(logging.ERROR, logger=ALERTS):
            ahead = await stand.post(stand.part(1, start))
        assert refusal(ahead) == (409, "rejected_time", "future"), ahead.text
        assert ahead.json()["error"]["details"]["last_accepted_seq"] == 0
        assert "retry-after" not in ahead.headers, "часть не повторяется — паузы нет"
        assert (await stand.report_row(1) or {})["status"] == "rejected_time"
        assert await stand.interval(start) is None
        assert alerts(caplog, stand.node_id, "rejected_time"), caplog.text
        beyond = await seconds_from_base(stand, dt.timedelta(seconds=7 - 60))  # конец +7 с
        assert refusal(await stand.post(stand.part(2, beyond))) == (409, "rejected_time", "future")
        within = await seconds_from_base(stand, dt.timedelta(seconds=3 - 60))  # конец +3 с
        accepted = await stand.post(stand.part(3, within))
        assert accepted.json() == {"last_accepted_seq": 3, "duplicate": False}, accepted.text


async def test_a_decreasing_counter_is_refused_by_the_model_and_leaves_no_trace(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """UC-04 A3: убывание счётчика обрабатывает агент (001.56); Control Plane отрицательную
    дельту не принимает — 422 модели, ни отчёта, ни интервала."""
    async with reporting(pg_dsn, migrate_env) as stand:
        body = stand.part(1, 0, lines=[Line(stand.users[0], 10, 10)])
        body["lines"][0]["uplink_bytes"] = -1
        refused = await stand.post(body)
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "validation_error"
        assert await stand.statuses() == [] and await stand.interval(0) is None


async def test_parts_complete_an_interval_and_a_part_past_it_is_refused(
    pg_dsn: str, migrate_env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """Правила частей (README): части интервала — последовательные номера с одинаковыми
    границами; счётчик принятых, суммы по направлениям; интервал завершён на последней части и
    закрыт; часть сверх объявленного числа — 409 `interval_closed` без `Retry-After`, записана
    со статусом, точка продолжения не сдвигается, событие — WARNING журнала (разрыв видит
    сверка, 001.37)."""
    async with reporting(pg_dsn, migrate_env) as stand:
        a, b, c = stand.users
        one = await stand.post(stand.part(1, 0, total=2, lines=[Line(a, 10, 20)], rx=100, tx=90))
        assert one.json() == {"last_accepted_seq": 1, "duplicate": False}
        opened = await stand.interval(0)
        assert opened is not None
        assert (opened["first_seq"], opened["parts_total"], opened["parts_accepted"]) == (1, 2, 1)
        assert opened["closed_at"] is None
        two = await stand.post(stand.part(2, 0, total=2, lines=[Line(b, 5, 6)]))
        assert two.json() == {"last_accepted_seq": 2, "duplicate": False}
        done = await stand.interval(0)
        assert done is not None
        assert (done["parts_accepted"], done["close_reason"]) == (2, "completed")
        assert (done["accepted_uplink_bytes"], done["accepted_downlink_bytes"]) == (15, 26)
        with caplog.at_level(logging.WARNING, logger=ALERTS):
            late = await stand.post(stand.part(3, 0, total=2, lines=[Line(c, 1, 1)]))
        assert refusal(late) == (409, "interval_closed", "closed"), late.text
        assert late.json()["error"]["details"]["last_accepted_seq"] == 2
        assert "retry-after" not in late.headers
        assert any(
            r.levelno == logging.WARNING and "interval_closed" in r.getMessage()
            for r in caplog.records
            if r.name == ALERTS
        ), caplog.text
        assert (await stand.report_row(3) or {})["status"] == "interval_closed"
        assert await stand.statuses() == ["accepted", "accepted", "interval_closed"]
        again = await stand.post(stand.part(3, 0, total=2, lines=[Line(c, 1, 1)]))
        assert refusal(again) == (409, "interval_closed", "repeated"), again.text


async def test_the_declared_number_of_parts_only_grows_and_is_applied_before_the_range(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Деление отказанной по размеру части добавляет части: `parts_total` растёт, действует
    последнее принятое; меньше прежнего — 422 `report_inconsistent` без записи; номер вне
    диапазона (ниже первой принятой, за последней) — 409 `interval_closed` с причиной
    `out_of_range`; новое число применяется до проверки диапазона — часть за прежним
    объявленным, но в новом, принимается."""
    async with reporting(pg_dsn, migrate_env) as stand:
        assert (await stand.post(stand.part(5, 0, total=2))).status_code == 200
        grown = await stand.post(stand.part(6, 0, total=3))
        assert grown.json() == {"last_accepted_seq": 6, "duplicate": False}
        assert (await stand.interval(0) or {})["parts_total"] == 3
        shrunk = await stand.post(stand.part(7, 0, total=2))
        assert refusal(shrunk) == (422, "report_inconsistent", "parts_total_decreased")
        assert await stand.report_row(7) is None, "отказ по контракту ключа не записывает"
        below = await stand.post(stand.part(4, 0, total=3))
        assert refusal(below) == (409, "interval_closed", "out_of_range"), below.text
        beyond = await stand.post(stand.part(8, 0, total=3))
        assert refusal(beyond) == (409, "interval_closed", "out_of_range"), beyond.text
        assert (await stand.report_row(8) or {})["status"] == "interval_closed"
        widened = await stand.post(stand.part(9, 0, total=5))
        assert widened.json() == {"last_accepted_seq": 9, "duplicate": False}, widened.text
        interval = await stand.interval(0)
        assert interval is not None
        assert (interval["parts_total"], interval["parts_accepted"]) == (5, 3)
        assert interval["closed_at"] is None, "часть 7 не принята — интервал не завершён"
        assert (await stand.post(stand.part(7, 0, total=5))).status_code == 200
        gapped = await stand.interval(0)
        assert gapped is not None and gapped["parts_accepted"] == 4
        assert gapped["closed_at"] is None, (
            "номер 8 отказан и пропущен: без него интервал не завершится — его закроет следующий "
            "интервал или срок, недостающая часть — разрыв (001.37)"
        )


async def test_a_continuation_carries_no_interface_counters_and_no_user_of_another_part(
    pg_dsn: str, migrate_env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """Счётчики интерфейса несёт только первая часть; пользователь встречается в одной части
    интервала — нарушения отвергаются по контракту (422 `report_inconsistent`), а не ошибкой
    вставки 001.77 и не удвоением. Строку прежней части кладёт тест — её вставка — 001.77;
    уникальность в схеме (`traffic_lines_interval_user_key`) — страховка того же правила."""
    async with reporting(pg_dsn, migrate_env) as stand:
        a, b, _ = stand.users
        first = stand.part(1, 0, total=3, lines=[Line(a, 10, 10)], rx=50, tx=40)
        assert (await stand.post(first)).status_code == 200
        stranger = uuid.uuid4()
        with caplog.at_level(logging.ERROR, logger=ALERTS):
            counted = await stand.post(
                stand.part(2, 0, total=3, lines=[Line(b, 1, 1), Line(stranger, 1, 1)], rx=1)
            )
        assert refusal(counted) == (422, "report_inconsistent", "counters_in_continuation")
        assert alerts(caplog, stand.node_id, "report_inconsistent"), caplog.text
        assert alerts(caplog, stand.node_id, "foreign_users"), "чужой пользователь и при 422"
        sent = await stand.post(stand.part(2, 0, total=3, lines=[Line(b, 1, 1)], tx=1))
        assert refusal(sent) == (422, "report_inconsistent", "counters_in_continuation")
        await stand.conn.execute(
            "insert into traffic_lines (node_id, counter_epoch, report_seq, user_id, "
            "period_start, period_end, raw_uplink_bytes, raw_downlink_bytes, billable_bytes, "
            "multiplier_milli, billing_group_id) values ($1, $2, 1, $3, $4, $5, 10, 10, 20, "
            "1000, $6)",
            stand.node_id,
            stand.epoch,
            a,
            stand.at(0),
            stand.at(60),
            uuid.uuid4(),
        )
        twice = await stand.post(stand.part(2, 0, total=3, lines=[Line(a, 1, 1)]))
        assert refusal(twice) == (422, "report_inconsistent", "user_in_another_part")
        assert await stand.report_row(2) is None
        other = await stand.post(stand.part(2, 0, total=3, lines=[Line(b, 1, 1)]))
        assert other.json() == {"last_accepted_seq": 2, "duplicate": False}, other.text
        with pytest.raises(asyncpg.UniqueViolationError):
            await stand.conn.execute(
                "insert into traffic_lines (node_id, counter_epoch, report_seq, user_id, "
                "period_start, period_end, raw_uplink_bytes, raw_downlink_bytes, "
                "billable_bytes, multiplier_milli, billing_group_id) values ($1, $2, 3, $3, $4, "
                "$5, 1, 1, 2, 1000, $6)",
                stand.node_id,
                stand.epoch,
                a,
                stand.at(0),
                stand.at(60),
                uuid.uuid4(),
            )


async def test_intervals_of_a_node_are_monotonic_across_epochs(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Интервалы ноды монотонны и не перекрываются — и внутри эпохи, и через смену эпохи:
    новый интервал, начавшийся раньше конца последнего принятого, — 409 `rejected_time` с
    причиной `overlap`; новая эпоха нумерует с единицы и продолжает время, а её неудержанная
    часть закрывает открытый интервал прежней эпохи (`superseded` — перезапуск Xray посередине
    интервала, README)."""
    async with reporting(pg_dsn, migrate_env) as stand:
        assert (await stand.post(stand.part(1, 0))).status_code == 200
        overlapping = await stand.post(stand.part(2, 30))
        assert refusal(overlapping) == (409, "rejected_time", "overlap"), overlapping.text
        earlier = await stand.post(stand.part(3, -120))
        assert refusal(earlier) == (409, "rejected_time", "overlap"), earlier.text
        assert earlier.json()["error"]["details"]["last_accepted_seq"] == 1, (
            "отказанный номер 2 точку продолжения не двигает"
        )
        epoch, other = uuid.uuid4(), uuid.uuid4()
        restarted = await stand.post(stand.part(1, 60, epoch=epoch))
        assert restarted.json() == {"last_accepted_seq": 1, "duplicate": False}, restarted.text
        crossing = await stand.post(stand.part(1, 90, epoch=other))
        assert refusal(crossing) == (409, "rejected_time", "overlap"), crossing.text
        assert (await stand.post(stand.part(4, 120))).json() == {
            "last_accepted_seq": 4,
            "duplicate": False,
        }, "прежняя эпоха продолжает время после новой — тоже монотонно"
        assert (await stand.post(stand.part(5, 180, total=2))).status_code == 200
        # Номер 1 эпохи `other` уже записан отказом (`crossing`) — продолжаем со второго.
        restart = await stand.post(stand.part(2, 240, epoch=other))
        assert restart.json() == {"last_accepted_seq": 2, "duplicate": False}, restart.text
        assert (await stand.interval(180) or {})["close_reason"] == "superseded"


async def test_the_first_interval_is_anchored_to_the_buffer_depth(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Якорь первого интервала ноды: не раньше суток назад (глубина буфера Н-8) с допуском 5 с,
    откалиброванным с обеих сторон — сутки и 7 с — `rejected_time` с причиной `before_anchor`,
    сутки и 3 с — приём (посадки допуска ×1,5 и ×0,5 краснеют)."""
    async with reporting(pg_dsn, migrate_env) as stand:
        depth = -BUFFER_DEPTH
        too_old = await seconds_from_base(stand, depth - dt.timedelta(seconds=7))
        refused = await stand.post(stand.part(1, too_old))
        assert refusal(refused) == (409, "rejected_time", "before_anchor"), refused.text
        tolerated = await seconds_from_base(stand, depth - dt.timedelta(seconds=3))
        edge = await stand.post(stand.part(2, tolerated, tolerated + 1))
        assert edge.json() == {"last_accepted_seq": 2, "duplicate": False}, edge.text


async def test_the_anchor_binds_only_the_first_interval_of_a_node(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Якорь — только для первого интервала ноды (решение заказчика 2026-10-20, роаст 001.34,
    раунд 2): однокусочный интервал закрывается сразу, и якорь «первого после закрытия всех»
    срабатывал почти на каждом интервале. Дальше держат монотонность и окно приёма 72 ч: нода,
    чей последний интервал закрыт трое суток назад, отчитывается за 25 ч назад (слив буфера после
    недоступности), а интервал, начатый до выдачи новой identity, принимается от неё (ротация,
    Н-28)."""
    async with reporting(pg_dsn, migrate_env) as stand:
        now = await db_now(stand.conn)
        old = now - dt.timedelta(days=3)
        await stand.conn.execute(
            "insert into report_intervals (node_id, counter_epoch, period_start, period_end, "
            "first_seq, parts_total, parts_accepted, accepted_uplink_bytes, "
            "accepted_downlink_bytes, first_received_at, last_received_at, closed_at, "
            "close_reason) values ($1, $2, $3, $3::timestamptz + interval '1 minute', 1, 1, 1, "
            "0, 0, $3, $3, $3, 'completed')",
            stand.node_id,
            stand.epoch,
            old,
        )
        start = await seconds_from_base(stand, -dt.timedelta(hours=25))
        drained = await stand.post(stand.part(2, start))
        assert drained.json() == {"last_accepted_seq": 2, "duplicate": False}, drained.text
        rotated, _ = await insert_identity(stand.conn, stand.node_id, now, generation=2)
        before = await seconds_from_base(stand, -dt.timedelta(seconds=30))
        across = await stand.post(stand.part(3, before, before + 20), headers=rotated)
        assert across.json() == {"last_accepted_seq": 3, "duplicate": False}, across.text


async def test_the_first_interval_is_anchored_to_the_identity_and_the_first_assignment(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Якорь первого интервала — и не раньше выдачи identity запроса (с допуском часов ноды 5 с:
    7 с раньше — отказ, 3 с — приём), и не раньше первого назначения ноды тарифицируемой группе
    (без допуска: раньше него `multiplier.resolve` отвечает 1.0 по умолчанию — передано
    001.23). Якорь держит только первый интервал, поэтому каждый случай — на своей ноде."""
    for lead, start, expected in (
        (10, -7, "before_anchor"),  # identity: за допуском
        (10, -3, None),  # identity: в допуске, назначение раньше
        (2, -3, "before_anchor"),  # назначение позже начала — без допуска
        (2, -1, None),
    ):
        async with reporting(pg_dsn, migrate_env, age=dt.timedelta(hours=1)) as stand:
            issued: dt.datetime = await stand.conn.fetchval(
                "select issued_at from node_identities where node_id = $1", stand.node_id
            )
            await stand.conn.execute(
                "update node_billing_assignments set valid_from = $2 where node_id = $1",
                stand.node_id,
                issued - dt.timedelta(seconds=lead),
            )
            offset = (issued - stand.base).total_seconds()
            response = await stand.post(stand.part(1, offset + start))
            if expected is None:
                assert response.json() == {"last_accepted_seq": 1, "duplicate": False}, (
                    lead,
                    start,
                    response.text,
                )
            else:
                assert refusal(response) == (409, "rejected_time", expected), (lead, start)


async def test_the_anchor_takes_the_first_assignment_and_none_does_not_stop_it(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Якорь — первое назначение ноды (`min(valid_from)`), а не последнее (раунд 3, m3): при двух
    назначениях первый интервал между ними принимается. Ноду без назначений якорь не
    останавливает — её шаг списания отвечает `NoBillingAssignmentError` (001.77; раунд 3, m4)."""
    async with reporting(pg_dsn, migrate_env, age=dt.timedelta(hours=1)) as stand:
        now = await db_now(stand.conn)
        moved = now - dt.timedelta(minutes=20)
        await stand.conn.execute(
            "update node_billing_assignments set valid_to = $2 where node_id = $1",
            stand.node_id,
            moved,
        )
        await stand.conn.execute(
            "insert into node_billing_assignments (node_id, billing_group_id, valid_from) "
            "select node_id, billing_group_id, $2 from node_billing_assignments "
            "where node_id = $1",
            stand.node_id,
            moved,
        )
        between = await seconds_from_base(stand, -dt.timedelta(minutes=30))
        response = await stand.post(stand.part(1, between))
        assert response.json() == {"last_accepted_seq": 1, "duplicate": False}, response.text
    async with reporting(pg_dsn, migrate_env) as stand:
        await stand.conn.execute(
            "delete from node_billing_assignments where node_id = $1", stand.node_id
        )
        start = await seconds_from_base(stand, -dt.timedelta(hours=23))
        response = await stand.post(stand.part(1, start))
        assert response.json() == {"last_accepted_seq": 1, "duplicate": False}, response.text


async def test_moments_at_the_edge_of_the_range_get_a_contract_answer(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Метка у края диапазона `datetime` — ответ по контракту, а не 500 кодирования asyncpg
    (раунд 3, F-5): момент, который в UTC выходит за год 0001 или 9999, — 422 модели; края
    диапазона в UTC доходят до приёма и записи — 409 `too_old` и `future`."""
    async with reporting(pg_dsn, migrate_env) as stand:
        assert (await stand.post(stand.part(1, 0))).status_code == 200  # дальше — окно, не якорь
        for seq, start, end, expected in (
            (2, "0001-01-01T00:00:00+00:01", "0001-01-01T00:01:00Z", (422, "validation_error")),
            (3, "9999-12-31T23:59:00Z", "9999-12-31T23:59:59-00:01", (422, "validation_error")),
            (4, "0001-01-01T00:00:00Z", "0001-01-01T00:01:00Z", (409, "rejected_time", "too_old")),
            (5, "9999-12-31T23:59:00Z", "9999-12-31T23:59:59Z", (409, "rejected_time", "future")),
        ):
            response = await stand.post(
                {**stand.part(seq, 0), "period_start": start, "period_end": end}
            )
            assert refusal(response)[: len(expected)] == expected, (seq, response.text)


async def test_an_interval_closes_on_the_next_interval_the_deadline_and_its_age(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Интервал закрывается частью следующего интервала (`superseded`), сроком
    `REPORT_PARTS_DEADLINE` после приёма последней части (`deadline`) и потолком возраста
    `REPORT_MAX_INTERVAL_AGE` от первой (`age`); момент закрытия по времени — момент истечения,
    а не приёма. Часть к закрытому — 409 `interval_closed`."""
    async with reporting(pg_dsn, migrate_env) as stand:
        assert (await stand.post(stand.part(1, 0, total=2))).status_code == 200
        assert (await stand.post(stand.part(3, 60))).status_code == 200
        superseded = await stand.interval(0)
        assert superseded is not None and superseded["close_reason"] == "superseded"
        after = await stand.post(stand.part(2, 0, total=2))
        assert refusal(after) == (409, "interval_closed", "closed"), after.text

        assert (await stand.post(stand.part(4, 120, total=2))).status_code == 200
        now = await db_now(stand.conn)
        last = now - PARTS_DEADLINE - dt.timedelta(minutes=1)
        await stand.conn.execute(
            "update report_intervals set first_received_at = $2, last_received_at = $2 "
            "where node_id = $1 and period_start = $3",
            stand.node_id,
            last,
            stand.at(120),
        )
        expired = await stand.post(stand.part(5, 120, total=2))
        assert refusal(expired) == (409, "interval_closed", "closed"), expired.text
        deadline = await stand.interval(120)
        assert deadline is not None and deadline["close_reason"] == "deadline"
        assert deadline["closed_at"] == last + PARTS_DEADLINE

        assert (await stand.post(stand.part(6, 180, total=3))).status_code == 200
        first = now - INTERVAL_AGE - dt.timedelta(minutes=1)
        await stand.conn.execute(
            "update report_intervals set first_received_at = $2, last_received_at = $3 "
            "where node_id = $1 and period_start = $4",
            stand.node_id,
            first,
            now - dt.timedelta(hours=1),
            stand.at(180),
        )
        aged = await stand.post(stand.part(7, 180, total=3))
        assert refusal(aged) == (409, "interval_closed", "closed"), aged.text
        age = await stand.interval(180)
        assert age is not None and age["close_reason"] == "age"
        assert age["closed_at"] == first + INTERVAL_AGE

        # Раньше срока и возраста интервал открыт: последняя часть сутки назад — до срока час.
        assert (await stand.post(stand.part(8, 240, total=2))).status_code == 200
        day_ago = now - dt.timedelta(hours=24)
        await stand.conn.execute(
            "update report_intervals set first_received_at = $2, last_received_at = $2 "
            "where node_id = $1 and period_start = $3",
            stand.node_id,
            day_ago,
            stand.at(240),
        )
        alive = await stand.post(stand.part(9, 240, total=2))
        assert alive.json() == {"last_accepted_seq": 9, "duplicate": False}, alive.text
        assert (await stand.interval(240) or {})["close_reason"] == "completed"


async def test_a_new_interval_older_than_the_acceptance_window_is_refused(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Окно приёма — явная проверка (роаст 001.34, раунд 1): якорь действует только для первого
    интервала ноды, и цепочка интервалов, каждый из которых закрывает недосланный предыдущий,
    отставала бы от стенных часов без предела — до «no partition … found» вставки 001.77. Новый
    интервал старше 72 ч (сутки буфера плюс потолок возраста открытого) — 409 `rejected_time` с
    причиной `too_old`, и при открытом интервале тоже; законное отставание в окне — приём."""
    async with reporting(pg_dsn, migrate_env) as stand:
        now = await db_now(stand.conn)
        ancient = now - dt.timedelta(days=20)
        await stand.conn.execute(
            "insert into report_intervals (node_id, counter_epoch, period_start, period_end, "
            "first_seq, parts_total, parts_accepted, accepted_uplink_bytes, "
            "accepted_downlink_bytes, first_received_at, last_received_at) values ($1, $2, $3, "
            "$3::timestamptz + interval '1 minute', 1, 2, 1, 0, 0, $4, $4)",
            stand.node_id,
            stand.epoch,
            ancient,
            now - dt.timedelta(hours=1),
        )
        stale = (ancient + dt.timedelta(minutes=1) - stand.base).total_seconds()
        refused = await stand.post(stand.part(2, stale))
        assert refusal(refused) == (409, "rejected_time", "too_old"), refused.text
        # Граница — литералами с обеих сторон допуска 5 с: 72 ч и 7 с — отказ, 72 ч и 3 с — приём.
        # Отказанная сторона — интервал 60 с: его конец уже в окне, и проверка по `period_end`
        # вместо `period_start` его приняла бы (раунд 3, M3).
        window = dt.timedelta(hours=72)
        beyond = await seconds_from_base(stand, -window - dt.timedelta(seconds=7))
        edge = await stand.post(stand.part(3, beyond))
        assert refusal(edge) == (409, "rejected_time", "too_old"), edge.text
        inside = await seconds_from_base(stand, -window - dt.timedelta(seconds=3))
        tolerated = await stand.post(stand.part(4, inside, inside + 1))
        assert tolerated.json() == {"last_accepted_seq": 4, "duplicate": False}, tolerated.text
        lagging = await seconds_from_base(stand, -dt.timedelta(hours=71))
        accepted = await stand.post(stand.part(5, lagging))
        assert accepted.json() == {"last_accepted_seq": 5, "duplicate": False}, accepted.text


async def test_a_continuation_is_not_bound_by_the_acceptance_window(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Окно приёма — правило нового интервала (раунд 3, m1): часть открытого интервала, первая
    часть которого принята у края окна, принимается и тогда, когда его начало уже старше 72 ч, —
    открытый интервал живёт сроком и возрастом от приёма, а не объявленным временем."""
    async with reporting(pg_dsn, migrate_env) as stand:
        now = await db_now(stand.conn)
        start = (now - dt.timedelta(hours=73) - stand.base).total_seconds()
        await stand.conn.execute(
            "insert into report_intervals (node_id, counter_epoch, period_start, period_end, "
            "first_seq, parts_total, parts_accepted, accepted_uplink_bytes, "
            "accepted_downlink_bytes, first_received_at, last_received_at) "
            "values ($1, $2, $3, $4, 1, 2, 1, 0, 0, $5, $5)",
            stand.node_id,
            stand.epoch,
            stand.at(start),
            stand.at(start + 60),
            now - dt.timedelta(hours=2),
        )
        continued = await stand.post(
            stand.part(2, start, total=2, lines=[Line(stand.users[0], 10, 10)])
        )
        assert continued.json() == {"last_accepted_seq": 2, "duplicate": False}, continued.text
        assert (await stand.interval(start) or {})["close_reason"] == "completed"


async def test_a_held_part_does_not_close_the_interval_before_it(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Удержанная часть предыдущий интервал не закрывает: его оставшиеся части принимаются;
    первая часть нового интервала открывает его с признаком удержания."""
    async with reporting(pg_dsn, migrate_env, bandwidth_mbps=1) as stand:
        a, b, _ = stand.users
        assert (
            await stand.post(stand.part(1, 0, total=2, lines=[Line(a, 1, 1)]))
        ).status_code == 200
        huge = stand.part(
            3, 60, lines=[Line(b, anomaly_threshold(1, dt.timedelta(seconds=60)) + 1)]
        )
        assert (await stand.post(huge)).status_code == 200
        assert (await stand.report_row(3) or {})["status"] == "held_anomaly"
        previous = await stand.interval(0)
        assert previous is not None and previous["closed_at"] is None, "удержание не закрывает"
        held = await stand.interval(60)
        assert held is not None and held["held"] is True
        rest = await stand.post(stand.part(2, 0, total=2, lines=[Line(b, 1, 1)]))
        assert rest.json() == {"last_accepted_seq": 3, "duplicate": False}, rest.text
        assert (await stand.interval(0) or {})["close_reason"] == "completed"


async def test_a_part_that_is_not_held_closes_the_intervals_before_it(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Интервал закрывается частью следующего интервала ноды — и тогда, когда следующий открыт
    удержанной частью: его неудержанное продолжение закрывает открытые интервалы до себя
    (`superseded`), а сам интервал остаётся открытым до своей последней части."""
    async with reporting(pg_dsn, migrate_env, bandwidth_mbps=1) as stand:
        a, b, _ = stand.users
        big = anomaly_threshold(1, dt.timedelta(seconds=60)) + 1
        assert (await stand.post(stand.part(1, 0, total=2, lines=[Line(a, 1)]))).status_code == 200
        held = stand.part(2, 60, total=3, lines=[Line(b, 0, big)])
        assert (await stand.post(held)).status_code == 200
        assert (await stand.interval(0) or {})["closed_at"] is None
        # После удержания сумма интервала на пороге: неудержанным бывает только продолжение без
        # байтов — здесь с одними адресами.
        calm = await stand.post(stand.part(3, 60, total=3, ips=[(a, "198.51.100.10")]))
        assert calm.json() == {"last_accepted_seq": 3, "duplicate": False}, calm.text
        assert (await stand.interval(0) or {})["close_reason"] == "superseded"
        following = await stand.interval(60)
        assert following is not None and following["closed_at"] is None
        assert following["held"] is True, "неудержанное продолжение признак удержания не снимает"


async def test_a_held_continuation_does_not_close_the_intervals_before_it(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Удержанное продолжение, как и удержанная первая часть, открытые интервалы до себя не
    закрывает (раунд 3, F-2/M2): оставшиеся части прежнего интервала принимаются, а не становятся
    разрывом."""
    async with reporting(pg_dsn, migrate_env, bandwidth_mbps=1) as stand:
        a, b, c = stand.users
        big = anomaly_threshold(1, dt.timedelta(seconds=60)) + 1
        first = stand.part(1, 0, total=2, lines=[Line(a, 1, 1)])
        assert (await stand.post(first)).status_code == 200
        assert (
            await stand.post(stand.part(3, 60, total=3, lines=[Line(b, big)]))
        ).status_code == 200
        continued = await stand.post(stand.part(4, 60, total=3, lines=[Line(c, 1)]))
        assert continued.json() == {"last_accepted_seq": 4, "duplicate": False}, continued.text
        assert (await stand.report_row(4) or {})["status"] == "held_anomaly"
        previous = await stand.interval(0)
        assert previous is not None and previous["closed_at"] is None, (
            "удержанное продолжение не закрывает"
        )
        rest = await stand.post(stand.part(2, 0, total=2, lines=[Line(b, 1, 1)]))
        assert rest.json() == {"last_accepted_seq": 4, "duplicate": False}, rest.text
        assert (await stand.interval(0) or {})["close_reason"] == "completed"


async def test_a_clock_stepped_back_between_parts_keeps_the_interval(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Часы базы стенда шагают (prltimesync): приём последней части записан моментом впереди
    нынешних часов. Следующая часть того же интервала принимается, а момент последнего приёма
    назад не идёт — иначе `last_received_at >= first_received_at` упал бы ошибкой вставки (500
    и бесконечный повтор агента)."""
    async with reporting(pg_dsn, migrate_env) as stand:
        assert (await stand.post(stand.part(1, 0, total=3))).status_code == 200
        ahead = await db_now(stand.conn) + dt.timedelta(hours=1)
        await stand.conn.execute(
            "update report_intervals set first_received_at = $2, last_received_at = $2 "
            "where node_id = $1",
            stand.node_id,
            ahead,
        )
        second = await stand.post(stand.part(2, 0, total=3))
        assert second.json() == {"last_accepted_seq": 2, "duplicate": False}, second.text
        interval = await stand.interval(0)
        assert interval is not None and interval["last_received_at"] == ahead


async def _open_intervals(stand: ReportingStand, count: int, start: float) -> None:
    """``count`` открытых удержанных интервалов ноды подряд от ``start`` — состояние, которое
    иначе собирала бы череда удержаний."""
    now = await db_now(stand.conn)
    for n in range(count):
        await stand.conn.execute(
            "insert into report_intervals (node_id, counter_epoch, period_start, period_end, "
            "first_seq, parts_total, parts_accepted, accepted_uplink_bytes, "
            "accepted_downlink_bytes, held, first_received_at, last_received_at) "
            "values ($1, $2, $3, $4, $5, 2, 1, 0, 0, true, $6, $6)",
            stand.node_id,
            stand.epoch,
            stand.at(start + 60 * n),
            stand.at(start + 60 * n + 60),
            100 + 2 * n,
            now,
        )


async def test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed(
    pg_dsn: str, migrate_env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """Открытых интервалов у ноды — не больше восьми (литерал контракта). Истёкший по сроку
    интервал среди открытых не считается: при семи живых и одном истёкшем удержанная часть
    нового интервала ещё открывает его (с признаком удержания); при восьми живых — удерживается,
    её интервал рождается закрытым (`capped`, удержан, без списания — решение заказчика
    2026-10-20) и поднимается алерт. Закрытый интервал держит правила: следующая часть того же
    интервала — `interval_closed`, а не новый интервал с нулевыми суммами (роаст 001.34, раунд
    1), перекрывающая — `overlap`. Часть, которая не удерживается, закрывает открытые
    (`superseded`)."""
    async with reporting(pg_dsn, migrate_env, bandwidth_mbps=1) as stand:
        assert REPORT_MAX_OPEN_INTERVALS == 8
        a, b, _ = stand.users
        big = anomaly_threshold(1, dt.timedelta(seconds=60)) + 1
        await _open_intervals(stand, 7, -480)
        expired = await db_now(stand.conn) - dt.timedelta(hours=26)
        await stand.conn.execute(
            "insert into report_intervals (node_id, counter_epoch, period_start, period_end, "
            "first_seq, parts_total, parts_accepted, accepted_uplink_bytes, "
            "accepted_downlink_bytes, held, first_received_at, last_received_at) "
            "values ($1, $2, $3, $4, 200, 2, 1, 0, 0, true, $5, $5)",
            stand.node_id,
            stand.epoch,
            stand.at(-60),
            stand.at(0),
            expired,
        )
        eighth = await stand.post(stand.part(1, 0, lines=[Line(a, big)], total=2))
        assert eighth.json() == {"last_accepted_seq": 1, "duplicate": False}, eighth.text
        opened = await stand.interval(0)
        assert opened is not None and opened["held"] is True and opened["closed_at"] is None, (
            "истёкший интервал места среди открытых не занимает"
        )
        with caplog.at_level(logging.ERROR, logger=ALERTS):
            over = await stand.post(stand.part(2, 60, total=3, lines=[Line(a, big)], rx=100))
        assert over.json() == {"last_accepted_seq": 2, "duplicate": False}, over.text
        assert (await stand.report_row(2) or {})["status"] == "held_anomaly"
        capped = await stand.interval(60)
        assert capped is not None
        assert (capped["close_reason"], capped["held"]) == ("capped", True)
        assert (capped["accepted_uplink_bytes"], capped["accepted_downlink_bytes"]) == (0, 0), (
            "сверх потолка открытых — без списания"
        )
        assert alerts(caplog, stand.node_id, "open_intervals"), caplog.text
        still = await stand.interval(0)
        assert still is not None and still["closed_at"] is None, (
            "часть сверх потолка открытые интервалы не закрывает (раунд 3, F-3)"
        )
        again = await stand.post(stand.part(3, 60, total=2, lines=[Line(a, 1)], rx=7))
        assert refusal(again) == (409, "interval_closed", "closed"), again.text
        overlapping = await stand.post(stand.part(4, 90, lines=[Line(b, 1)]))
        assert refusal(overlapping) == (409, "rejected_time", "overlap"), overlapping.text
        calm = await stand.post(stand.part(5, 120, lines=[Line(b, 1, 1)]))
        assert calm.json() == {"last_accepted_seq": 5, "duplicate": False}, calm.text
        reasons = await stand.conn.fetch(
            "select close_reason from report_intervals where node_id = $1 and "
            "period_start < $2 order by period_start",
            stand.node_id,
            stand.at(60),
        )
        assert [r["close_reason"] for r in reasons] == ["superseded"] * 7 + [
            "deadline",
            "superseded",
        ]


async def test_a_held_single_part_interval_takes_no_place_among_the_open(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Удержанный однокусочный интервал закрывается сразу (`completed`) и места среди открытых не
    занимает (раунд 3, F-1/M1): при семи открытых он не доводит их до потолка, и следующая
    удержанная часть нового интервала открывает его со списанием в пределах порога, а не
    рождается закрытой без списания."""
    async with reporting(pg_dsn, migrate_env, bandwidth_mbps=1) as stand:
        a, b, _ = stand.users
        big = anomaly_threshold(1, dt.timedelta(seconds=60)) + 1
        await _open_intervals(stand, 7, -480)
        single = await stand.post(stand.part(1, 0, lines=[Line(a, big)]))
        assert single.json() == {"last_accepted_seq": 1, "duplicate": False}, single.text
        held = await stand.interval(0)
        assert held is not None
        assert (held["held"], held["close_reason"]) == (True, "completed")
        following = await stand.post(stand.part(2, 60, total=2, lines=[Line(b, big)]))
        assert following.json() == {"last_accepted_seq": 2, "duplicate": False}, following.text
        opened = await stand.interval(60)
        assert opened is not None and opened["closed_at"] is None, "восьмой открытый — в потолке"
        assert (opened["accepted_uplink_bytes"], opened["accepted_downlink_bytes"]) == (
            7_500_000,
            0,
        )


_EPOCH_SEED = (
    "insert into report_intervals (node_id, counter_epoch, period_start, period_end, "
    "first_seq, parts_total, parts_accepted, accepted_uplink_bytes, "
    "accepted_downlink_bytes, first_received_at, last_received_at, closed_at, "
    "close_reason) values ($1, $2, $3, $4, 1, 1, 1, 0, 0, $5, $5, $5, 'completed')"
)


async def _seed_epochs(
    stand: ReportingStand, epochs: list[uuid.UUID], start: float, ago: dt.timedelta
) -> None:
    """Закрытые интервалы подряд от ``start`` с эпохами ``epochs``, открытые ``ago`` назад."""
    now = await db_now(stand.conn)
    for n, epoch in enumerate(epochs):
        await stand.conn.execute(
            _EPOCH_SEED,
            stand.node_id,
            epoch,
            stand.at(start + 60 * n),
            stand.at(start + 60 * n + 60),
            now - ago,
        )


async def test_epoch_changes_are_capped_per_day(
    pg_dsn: str, migrate_env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """Смен эпохи — не больше 48 за скользящие сутки (литералы контракта): смена — соседние по
    времени интервалы с разными эпохами, и возврат к прежней эпохе — тоже смена (чередование
    двух эпох не обходит потолок — роаст 001.34, раунд 1). 47 смен в окне и сорок восьмая —
    приём; сорок девятая — удержание (интервал открывается с признаком удержания, списание в
    пределах порога идёт, открытый интервал до него не закрывается) и алерт; интервал той же
    эпохи, что последний, — не смена. Окно — сутки: смена между интервалами, открытыми сутки и
    две минуты и сутки и минуту назад, не считается, а в 23 ч 59 мин — считается."""
    async with reporting(pg_dsn, migrate_env) as stand:
        assert REPORT_MAX_EPOCHS_PER_DAY == 48
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        user = stand.users[0]
        await _seed_epochs(stand, [c], -3720, dt.timedelta(hours=24, minutes=2))
        await _seed_epochs(stand, [a], -3660, dt.timedelta(hours=24, minutes=1))
        alternating = [a if n % 2 == 0 else b for n in range(48)]  # 47 смен в окне
        await _seed_epochs(stand, alternating, -3600, dt.timedelta(hours=23, minutes=59))
        changed = await stand.post(stand.part(1, 0, total=2))  # b → эпоха стенда: сорок восьмая
        assert changed.json() == {"last_accepted_seq": 1, "duplicate": False}, changed.text
        assert (await stand.report_row(1) or {})["status"] == "accepted"
        with caplog.at_level(logging.ERROR, logger=ALERTS):
            back = await stand.post(stand.part(9, 60, epoch=a, lines=[Line(user, 1000, 2000)]))
        assert back.status_code == 200, back.text
        assert (await stand.report_row(9, a) or {})["status"] == "held_anomaly"
        held = await stand.interval(60, epoch=a)
        assert held is not None and held["held"] is True
        assert (held["accepted_uplink_bytes"], held["accepted_downlink_bytes"]) == (1000, 2000), (
            "удержание по потолку смен эпохи списания не отменяет"
        )
        assert (await stand.interval(0) or {})["closed_at"] is None, "удержанная не закрывает"
        assert alerts(caplog, stand.node_id, "epochs"), caplog.text
        same = await stand.post(stand.part(10, 120, epoch=a))
        assert same.json() == {"last_accepted_seq": 10, "duplicate": False}, same.text
        assert (await stand.report_row(10, a) or {})["status"] == "accepted"


async def test_an_epoch_change_across_the_window_edge_is_counted(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Смена случилась, когда открыт второй интервал пары: пара, чей первый интервал открыт до
    окна, а второй — в окне, — тоже смена в сутках (роаст 001.34, раунд 2). 46 смен в окне и
    смена на краю — 47; сорок восьмая — приём, сорок девятая — удержание."""
    async with reporting(pg_dsn, migrate_env) as stand:
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await _seed_epochs(stand, [c], -3660, dt.timedelta(hours=24, minutes=1))
        alternating = [a if n % 2 == 0 else b for n in range(47)]  # 46 смен в окне
        await _seed_epochs(stand, alternating, -3600, dt.timedelta(hours=23, minutes=59))
        changed = await stand.post(stand.part(1, 0))
        assert (await stand.report_row(1) or {})["status"] == "accepted", changed.text
        other = uuid.uuid4()
        over = await stand.post(stand.part(1, 60, epoch=other))
        assert over.status_code == 200, over.text
        assert (await stand.report_row(1, other) or {})["status"] == "held_anomaly"


async def test_an_epoch_hold_over_the_open_cap_is_born_closed(
    pg_dsn: str, migrate_env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """Потолок открытых интервалов — к любой удержанной части нового интервала, а не только к
    удержанной порогом (раунд 3, F-4): при восьми открытых часть, удержанная одним потолком смен
    эпохи (объём в пределах порога), рождает интервал закрытым (`capped`) без списания — решение
    заказчика 2026-10-20."""
    async with reporting(pg_dsn, migrate_env) as stand:
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        user = stand.users[0]
        await _seed_epochs(stand, [c], -4320, dt.timedelta(hours=24, minutes=2))
        await _seed_epochs(stand, [a], -4260, dt.timedelta(hours=24, minutes=1))
        alternating = [a if n % 2 == 0 else b for n in range(48)]  # 47 смен в окне
        await _seed_epochs(stand, alternating, -4200, dt.timedelta(hours=23, minutes=59))
        await _open_intervals(stand, 8, -1320)  # эпоха стенда — сорок восьмая смена
        with caplog.at_level(logging.ERROR, logger=ALERTS):
            over = await stand.post(stand.part(1, 0, epoch=a, lines=[Line(user, 1000, 2000)]))
        assert over.json() == {"last_accepted_seq": 1, "duplicate": False}, over.text
        assert (await stand.report_row(1, a) or {})["status"] == "held_anomaly"
        capped = await stand.interval(0, epoch=a)
        assert capped is not None
        assert (capped["close_reason"], capped["held"]) == ("capped", True)
        assert (capped["accepted_uplink_bytes"], capped["accepted_downlink_bytes"]) == (0, 0)
        found = alerts(caplog, stand.node_id, "held_anomaly")
        assert found and "reasons=epochs,open_intervals" in found[0], caplog.text


def _plan_nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plan
    for child in plan.get("Plans", []):
        yield from _plan_nodes(child)


async def test_epoch_changes_are_read_by_the_index_from_the_predecessor_on(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Смены эпохи считаются под блокировкой ноды на каждой части со сменой эпохи: выборка
    ограничена моментом открытия предшественника окна по индексу `(node_id, first_received_at)`,
    а не всей историей ноды (раунд 3, P-1). План при запрете последовательного чтения: каждое
    чтение `report_intervals` — по индексу с условием на `first_received_at`."""
    async with reporting(pg_dsn, migrate_env) as stand:
        window = await db_now(stand.conn) - dt.timedelta(hours=24)
        async with stand.conn.transaction():
            await stand.conn.execute("set local enable_seqscan = off")
            raw = await stand.conn.fetchval(
                "explain (format json) " + _EPOCH_CHANGES, stand.node_id, window
            )
        reads = [
            node
            for node in _plan_nodes(json.loads(raw)[0]["Plan"])
            if node.get("Relation Name") == "report_intervals"
        ]
        assert len(reads) >= 2, raw
        for node in reads:
            condition = node.get("Index Cond", "") + node.get("Recheck Cond", "")
            assert "first_received_at" in condition, raw


async def test_lines_and_addresses_of_users_outside_the_node_are_dropped(
    pg_dsn: str, migrate_env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """Нода недоверенная (§11.3): строка и адрес пользователя, которого нет в составе ноды
    (`node_user_state`), отбрасываются с алертом — иначе нода списала бы трафик чужому
    пользователю и блокировала бы чужие адреса; остальной отчёт принимается. Чужой — и
    несуществующий идентификатор, и настоящий пользователь другой ноды, и пользователь вне
    составов (раунд 3, C2): состав — этой ноды, а не «пользователь существует»."""
    async with reporting(pg_dsn, migrate_env) as stand:
        own, removed, stranger = stand.users[0], stand.users[1], uuid.uuid4()
        elsewhere = await insert_user(stand.conn, await insert_node(stand.conn, stand.node_id))
        nobody = await insert_user(stand.conn)
        # Член состава — строка `node_user_state` в любом состоянии: `removed` до уплотнения
        # тоже (буфер досылает трафик до отзыва).
        await stand.conn.execute(
            "update node_user_state set state = 'removed' where node_id = $1 and user_id = $2",
            stand.node_id,
            removed,
        )
        body = stand.part(
            1,
            0,
            lines=[
                Line(own, 10, 20),
                Line(removed, 5, 5),
                Line(stranger, 1_000_000, 1_000_000),
                Line(elsewhere, 2_000_000, 0),
                Line(nobody, 0, 3_000_000),
            ],
            ips=[
                (own, "198.51.100.8"),
                (stranger, "198.51.100.9"),
                (elsewhere, "198.51.100.10"),
                (nobody, "198.51.100.11"),
            ],
        )
        with caplog.at_level(logging.ERROR, logger=ALERTS):
            response = await stand.post(body)
        assert response.json() == {"last_accepted_seq": 1, "duplicate": False}, response.text
        interval = await stand.interval(0)
        assert interval is not None
        assert (interval["accepted_uplink_bytes"], interval["accepted_downlink_bytes"]) == (15, 25)
        found = alerts(caplog, stand.node_id, "foreign_users")
        assert found and "lines=3 " in found[0] and "ips=3 " in found[0], caplog.text
        caplog.clear()
        ahead = await seconds_from_base(stand, dt.timedelta(seconds=10 - 60))
        with caplog.at_level(logging.ERROR, logger=ALERTS):
            refused = await stand.post(stand.part(2, ahead, lines=[Line(stranger, 1, 1)]))
        assert refusal(refused)[:2] == (409, "rejected_time")
        assert alerts(caplog, stand.node_id, "foreign_users"), (
            "попытка списать чужому пользователю — сигнал и тогда, когда часть отказана"
        )


async def test_a_revocation_during_the_request_stops_the_report(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Identity сверяется в начале запроса, а приём — своя транзакция: отзыв, зафиксированный
    между опознанием и записью, обязан остановить отчёт (001.25, раунд 2). Встречная операция
    держит строку ноды тем же оператором, что отзыв и обмен (`FOR NO KEY UPDATE`, `nodes.py`; с
    `FOR UPDATE` держателя страж не отличал бы ослабленную блокировку приёма — раунд 3, C3);
    приём ждёт её и перепроверяет под блокировкой ровно ту identity, которой
    запрос представился: отозвана только она (у ноды есть и другая, живая — повторный обмен),
    истекла, нода выведена — 401; повторное опознание как `pending` — 403. Без записи."""
    async with reporting(pg_dsn, migrate_env) as stand:
        issued: dt.datetime = await stand.conn.fetchval(
            "select issued_at from node_identities where node_id = $1", stand.node_id
        )
        await insert_identity(stand.conn, stand.node_id, issued, generation=2)
        presented = (stand.fingerprint,)
        node = (stand.node_id,)
        for change, args, expected in (
            (
                "update node_identities set revoked_at = now() where cert_fingerprint = $1",
                presented,
                (401, "unauthenticated"),
            ),
            (
                "update node_identities set expires_at = now() where cert_fingerprint = $1",
                presented,
                (401, "unauthenticated"),
            ),
            (
                "update nodes set decommissioned_at = now() where id = $1",
                node,
                (401, "unauthenticated"),
            ),
            (
                "update nodes set status = 'pending' where id = $1",
                node,
                (403, "node_not_approved"),
            ),
        ):
            holder = await asyncpg.connect(pg_dsn)
            try:
                transaction = holder.transaction()
                await transaction.start()
                await holder.execute(REVOCATION_LOCK, stand.node_id)
                await holder.execute(change, *args)
                task = asyncio.create_task(stand.post(stand.part(1, 0)))
                try:
                    await wait_blocked_by(stand.conn, holder.get_server_pid())
                except BaseException:
                    task.cancel()
                    raise
                await transaction.commit()
                response = await task
            finally:
                await holder.close()
            assert (response.status_code, response.json()["error"]["code"]) == expected
            assert await stand.statuses() == []
            await stand.conn.execute(
                "update node_identities set revoked_at = null, expires_at = now() + "
                "interval '90 days' where node_id = $1",
                stand.node_id,
            )
            await stand.conn.execute(
                "update nodes set status = 'active', decommissioned_at = null where id = $1",
                stand.node_id,
            )


async def test_two_acceptances_of_a_node_take_turns(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Один приём ноды за раз (раунд 3, C3): правила интервала читают и пишут его суммы, и второй
    приём обязан ждать первого под блокировкой строки ноды, а не считать порог по суммам до него.
    Первый — настоящий приём в открытой транзакции, второй — запрос раздела: второй ждёт, затем
    видит сумму первого и удерживается с долей до порога; суммы интервала — ровно порог (1 Мбит/с
    × 60 с = 7 500 000 байт: 2,5 + 2,5 МБ приняты, из 4 МБ — 2,5)."""
    async with reporting(pg_dsn, migrate_env, bandwidth_mbps=1) as stand:
        a, b, c = stand.users
        opening = stand.part(1, 0, total=3, lines=[Line(a, 2_500_000)])
        assert (await stand.post(opening)).status_code == 200
        holder = await asyncpg.connect(pg_dsn)
        try:
            transaction = holder.transaction()
            await transaction.start()
            first = ReportIn.model_validate(stand.part(2, 0, total=3, lines=[Line(b, 2_500_000)]))
            outcome = await _Acceptance(stand.node_id, stand.fingerprint, first).run(holder)
            assert outcome.status == "accepted"
            task = asyncio.create_task(
                stand.post(stand.part(3, 0, total=3, lines=[Line(c, 4_000_000)]))
            )
            try:
                await wait_blocked_by(stand.conn, holder.get_server_pid())
            except BaseException:
                task.cancel()
                raise
            await transaction.commit()
            second = await task
        finally:
            await holder.close()
        assert second.json() == {"last_accepted_seq": 3, "duplicate": False}, second.text
        assert (await stand.report_row(3) or {})["status"] == "held_anomaly", (
            "второй приём видит сумму первого"
        )
        interval = await stand.interval(0)
        assert interval is not None
        assert (interval["accepted_uplink_bytes"], interval["accepted_downlink_bytes"]) == (
            7_500_000,
            0,
        )
        assert interval["close_reason"] == "completed"


async def test_the_continuation_point_and_repeated_refusals(
    pg_dsn: str, migrate_env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """`last_accepted_seq` — наибольший принятый номер эпохи (принятые и удержанные), а не номер
    запроса: повтор старого отчёта отвечает текущей точкой. Повтор отказанного — тот же отказ
    (`reason: repeated`, WARNING — потерянный ответ, а не новое событие); тот же номер с другими
    записанными полями — любой из границ, числом частей, счётчиками интерфейса — 422
    `seq_reused`: ключ уже лёг на другую часть, и ответ «дубликат» молча выбросил бы отчёт из
    буфера."""
    async with reporting(pg_dsn, migrate_env) as stand:
        assert (await stand.post(stand.part(1, 0))).status_code == 200
        ahead = stand.part(2, await seconds_from_base(stand, dt.timedelta(seconds=30)))
        assert refusal(await stand.post(ahead))[:2] == (409, "rejected_time")
        assert (await stand.post(stand.part(3, 60))).json()["last_accepted_seq"] == 3
        with caplog.at_level(logging.WARNING, logger=ALERTS):
            repeated = await stand.post(ahead)
        assert refusal(repeated) == (409, "rejected_time", "repeated"), repeated.text
        assert repeated.json()["error"]["details"]["last_accepted_seq"] == 3
        assert any(
            r.levelno == logging.WARNING and "repeated" in r.getMessage()
            for r in caplog.records
            if r.name == ALERTS
        ), caplog.text
        first = stand.part(1, 0)
        for changed in (
            {**first, "period_start": stand.at(1).isoformat()},
            {**first, "period_end": stand.at(61).isoformat()},
            {**first, "parts_total": 2},
            {**first, "node_rx_bytes": 1},
            {**first, "node_tx_bytes": 1},
            stand.part(2, 120),
        ):
            reused = await stand.post(changed)
            assert refusal(reused) == (422, "report_inconsistent", "seq_reused"), changed
        old = await stand.post(stand.part(1, 0))
        assert old.json() == {"last_accepted_seq": 3, "duplicate": True}, old.text


async def test_every_approved_status_has_its_reports_accepted(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Столбец «Учитывается трафик» §4.6: отчёт принимается от ноды в любом статусе, кроме
    `pending` (вентиль раздела, 403), — в `provisioning`, чтобы нода освободила буфер (не
    тарифицируется — 001.77), в `offline`, `disabled`, `suspended` — из буфера."""
    async with reporting(pg_dsn, migrate_env) as stand:
        approved = sorted(status for status, gate in STREAM_GATE.items() if not gate.refused)
        for n, status in enumerate(approved):
            await stand.conn.execute(
                "update nodes set status = $2 where id = $1", stand.node_id, status
            )
            response = await stand.post(stand.part(n + 1, 60 * n))
            assert response.status_code == 200, (status, response.text)
        assert len(approved) == 7
        await stand.conn.execute("update nodes set status = 'pending' where id = $1", stand.node_id)
        gated = await stand.post(stand.part(9, 600))
        assert (gated.status_code, gated.json()["error"]["code"]) == (403, "node_not_approved")


async def test_interval_durations_are_a_metric_per_node(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Штатный интервал — обязанность агента, и Control Plane её наблюдает (§5.9, README):
    длительности принятых и удержанных за час интервалов (удержанный — тоже интервал агента;
    раунд 3, m2) — ряды `control_plane_report_intervals` по ноде с
    верхней границей `le` (накопительно, как у гистограммы); алерт на систематическое
    превышение 60 с ставит 001.68."""
    async with reporting(pg_dsn, migrate_env) as stand:
        # Окно метрики — последний час (литерал): интервал, открытый 61 минуту назад, — за ним,
        # 59 минут назад — в нём.
        for minutes, start, held in ((61, -120, False), (59, -240, True)):
            await stand.conn.execute(
                "insert into report_intervals (node_id, counter_epoch, period_start, period_end, "
                "first_seq, parts_total, parts_accepted, accepted_uplink_bytes, "
                "accepted_downlink_bytes, held, first_received_at, last_received_at) values ($1, "
                "$2, $3, $4, 1, 1, 1, 0, 0, $6, now() - make_interval(mins => $5), "
                "now() - make_interval(mins => $5))",
                stand.node_id,
                uuid.uuid4(),
                stand.at(start),
                stand.at(start + 60),
                minutes,
                held,
            )
        assert (await stand.post(stand.part(1, 0))).status_code == 200
        assert (await stand.post(stand.part(2, 60, 360))).status_code == 200
        transport = httpx.ASGITransport(app=stand.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            text = (await client.get("/metrics")).text
        node = str(stand.node_id)
        series = {
            line.split(" ")[0]: line.split(" ")[1]
            for line in text.splitlines()
            if line.startswith("control_plane_report_intervals{") and node in line
        }
        assert series == {
            f'control_plane_report_intervals{{node_id="{node}",le="{bound}"}}': value
            for bound, value in (
                ("60", "2"),
                ("120", "2"),
                ("300", "3"),
                ("900", "3"),
                ("3600", "3"),
                ("+Inf", "3"),
            )
        }, text
        assert "# TYPE control_plane_report_intervals gauge" in text


async def test_a_failing_interval_series_does_not_mark_the_database_down(
    pg_dsn: str, migrate_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ряд длительностей интервалов — учёт, а не признак доступности базы: его сбой (таблица снята
    откатом, ошибка запроса) оставляет `control_plane_db_up 1` и остальные ряды, а самого ряда
    нет — иначе алерт «база недоступна» поднимался бы на ошибке одного запроса."""

    async def broken(conn: Any, window: Any = None) -> dict[Any, Any]:
        raise asyncpg.UndefinedTableError('relation "report_intervals" does not exist')

    monkeypatch.setattr(metrics, "interval_durations", broken)
    async with reporting(pg_dsn, migrate_env) as stand:
        transport = httpx.ASGITransport(app=stand.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            text = (await client.get("/metrics")).text
    assert "control_plane_db_up 1" in text, text
    assert "control_plane_jobs{" in text
    assert "control_plane_report_intervals{" not in text


CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "agent_v1"


def _shifted(body: dict[str, Any], delta: dt.timedelta) -> dict[str, Any]:
    """Тело фикстуры с метками, сдвинутыми на ``delta``: ``given.server_time`` фикстуры
    переносится на часы базы, всё остальное — как записано."""

    def move(stamp: str) -> str:
        return (dt.datetime.fromisoformat(stamp) + delta).isoformat()

    moved: dict[str, Any] = json.loads(json.dumps(body))
    moved["period_start"] = move(moved["period_start"])
    moved["period_end"] = move(moved["period_end"])
    for row in moved["online_ips"]:
        row["last_seen"] = move(row["last_seen"])
    return moved


async def test_the_recorded_report_answers_are_what_the_live_service_gives(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Фикстуры отчёта (`contracts/agent_v1/report*.json`) — ответы настоящего приёма, а не
    записанные вручную: при предусловии фикстуры — нода и пользователи, как записано, часы
    сервера `given.server_time` (метки тела сдвигаются на разность с часами базы), принятые до
    неё фикстуры `given.accepted` по порядку — живой приём отвечает ровно записанным кодом,
    телом и заголовками; каждая предшествующая фикстура — тоже своим. Герметичный прогон
    (`tests/contract`) отвечает записанным; этот тест держит записанное правдой."""
    fixtures = {path.stem: json.loads(path.read_text()) for path in CONTRACTS.glob("report*.json")}
    assert len(fixtures) == 7, sorted(fixtures)
    members = sorted(
        {
            uuid.UUID(row["user_id"])
            for fixture in fixtures.values()
            for key in ("lines", "online_ips")
            for row in fixture["request"]["body"][key]
        }
    )
    identity = {"X-Client-Cert", "X-Node-Identity"}  # identity стенда, не фикстуры
    for stem, fixture in sorted(fixtures.items()):
        async with reporting(
            pg_dsn,
            migrate_env,
            users=0,
            node_id=STUB_NODE_ID,
            member_ids=members,
            status=fixture["given"]["node_status"],
        ) as stand:
            server_time = dt.datetime.fromisoformat(fixture["given"]["server_time"])
            delta = await db_now(stand.conn) - server_time
            for name in [*fixture["given"].get("accepted", []), stem]:
                recorded = fixtures[name]
                headers = {
                    **{
                        k: v for k, v in recorded["request"]["headers"].items() if k not in identity
                    },
                    **{k: v for k, v in stand.headers.items() if k in identity},
                }
                response = await stand.post(
                    _shifted(recorded["request"]["body"], delta), headers=headers
                )
                expected = recorded["response"]
                assert response.status_code == expected["status"], (stem, name, response.text)
                assert response.json() == expected["body"], (stem, name)
                for header, value in expected["headers"].items():
                    assert response.headers.get(header) == value, (stem, name, header)
                if expected["status"] == 409:
                    assert "retry-after" not in response.headers, (stem, name)
