"""Сквозные проверки отчётов о трафике и запроса гранта квоты на заглушках (задача 001.33;
R-19, R-21…R-24, R-26, R-27, R-29, R-48; UC-04): контракт обеих операций в OpenAPI, приём отчёта
(TC-E2E-01), грант, признаки identity и вентиль §4.6 по инвентарю раздела, форма отчёта (границы
колонок §4.2.5, порядок границ периода, уникальность ключей строк и адресов, пределы размера
§5.7), правило «нода отчитывается только за себя», разобранное тело доходит до домена.

Байтовая форма ответов закреплена отдельно — `tests/contract/test_agent_v1.py` по фикстурам
`contracts/agent_v1/report*.json` и `quota-request.json`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from app.accounting import quota, service
from app.accounting.device_limit import DeviceLimitService
from app.accounting.quota import QuotaGrant, QuotaService
from app.accounting.service import (
    BODY_MAX_COMMAS,
    BODY_MAX_OPENERS,
    REPORT_MAX_LINES,
    REPORT_MAX_ONLINE_IPS,
    Accept,
    AccountingService,
    ReportIn,
)
from app.agent_api.deps import (
    CurrentNode,
    current_node,
    get_accounting_service,
    get_device_limit_service,
    get_quota_service,
)
from app.agent_api.router import router as agent_router
from app.db.pool import db_pool
from app.domain import composition
from app.domain.nodes import STUB_NODE_ID, Node, NodeStatus, stub_node
from app.domain.statuses import INT8_MAX, STREAM_GATE
from app.errors import VALIDATION_ERRORS_MAX, VALIDATION_LOC_MAX_CHARS
from app.main import create_app
from fastapi import APIRouter
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute

from tests._reports import USER_A, USER_B, VALID_REPORT, report_with, widest_report

FINGERPRINT = "9f8a3c17d4e05b2619c7a8f403d2e15b6c7a8d9e"
IDENTITY = "stub-identity-token-000000000000000000000000000"
HEADERS = {
    "X-Agent-Version": "0.1.0",
    "X-Client-Fingerprint": FINGERPRINT,
    "X-Node-Identity": IDENTITY,
}
REPORTS = "/agent/v1/reports"
QUOTA = "/agent/v1/quota/request"
OTHER_NODE_ID = uuid.UUID("00000000-0000-7000-8000-0000000000bb")
VALID_QUOTA: dict[str, Any] = {"user_id": USER_A, "consumed_bytes": 536870912}


class NoDatabase:
    """Заглушки раздела в базу не ходят — обращение к пулу валит тест."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"обращение к базе: {name}")


def as_node(status: NodeStatus = "active", node_id: uuid.UUID = STUB_NODE_ID) -> Any:
    async def override() -> CurrentNode:
        return CurrentNode(
            node=stub_node(status=status, node_id=node_id),
            fingerprint=FINGERPRINT,
            identity_token=IDENTITY,
            agent_version="0.1.0",
        )

    return override


@asynccontextmanager
async def agent(
    status: NodeStatus | None = None, overrides: dict[Any, Any] | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    """Клиент раздела без базы; `status` подменяет ноду запроса (заглушка `current_node` отдаёт
    `active`), `overrides` — прочие зависимости."""
    app = create_app()
    app.dependency_overrides[db_pool] = NoDatabase
    if status is not None:
        app.dependency_overrides[current_node] = as_node(status)
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
    for path, model in ((REPORTS, "Accept"), (QUOTA, "QuotaGrant")):
        operation = paths[path]["post"]
        assert operation["tags"] == ["agent"], path
        assert set(operation["responses"]) == {
            "200",
            "400",
            "401",
            "403",
            "411",
            "413",
            "422",
            "426",
            "429",
            "503",
        }
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


async def test_a_report_is_accepted_with_the_stub_answer() -> None:
    """TC-E2E-01: валидный отчёт → 200 `{last_accepted_seq: <номер части>, duplicate: false}`
    (заглушка без состояния: последний принятый — только что принятая часть) — с
    идентификатором своей ноды в теле и без него, первая часть и продолжение."""
    async with agent() as client:
        for body in (
            VALID_REPORT,
            {**VALID_REPORT, "node_id": str(STUB_NODE_ID)},
            {**VALID_REPORT, "report_seq": 1},
            {**VALID_REPORT, "report_seq": 4, "parts_total": 2, "node_rx_bytes": 0},
        ):
            response = await client.post(REPORTS, json=body, headers=HEADERS)
            assert response.status_code == 200, response.text
            assert response.json() == {"last_accepted_seq": body["report_seq"], "duplicate": False}
            assert response.headers["content-type"].startswith("application/json")


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
    app.dependency_overrides[current_node] = as_node()
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
    async with agent(overrides={current_node: as_node(node_id=OTHER_NODE_ID)}) as client:
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
        self.reports: list[tuple[uuid.UUID, ReportIn]] = []

    async def accept_report(self, node: Node, report: ReportIn) -> Accept:
        self.reports.append((node.id, report))
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
        current_node: as_node(node_id=OTHER_NODE_ID),
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
    assert [node_id for node_id, _ in accounting.reports] == [OTHER_NODE_ID]
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
