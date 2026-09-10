"""Сквозные проверки Node API `/agent/v1` на заглушках (задача 001.28; R-04, R-05, R-07;
UC-01 шаг 8, UC-07, UC-11): контракт в OpenAPI, состояние с дельтами и снапшотом, вентиль по
статусу ноды §4.6 в обеих формах запроса, подтверждение курсоров, heartbeat, телеметрия и
результат команды.

Раздел не знает ни сессий, ни CSRF: нода предъявляет отпечаток клиентского сертификата и токен
identity (§7.1), а версию агента — заголовком. Байтовая форма ответов закреплена отдельно —
`tests/contract/test_agent_v1.py` по фикстурам `contracts/agent_v1/`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any, get_args

import httpx
import pytest
from app.agent_api import deps
from app.agent_api.deps import (
    CurrentNode,
    current_node,
    get_command_service,
    get_composition_service,
    get_status_service,
)
from app.db.pool import db_pool
from app.domain import composition
from app.domain.commands import (
    STUB_COMMAND_ID,
    AgentCommandStatus,
    Command,
    CommandService,
)
from app.domain.composition import CompositionService
from app.domain.nodes import Node, NodeStatus, Version, stub_node
from app.domain.statuses import STREAM_GATE, HeartbeatIn, NodeMetrics, StatusService
from app.errors import ApiError
from app.jobs.handlers import HANDLERS
from app.jobs.handlers.composition import PUBLISH_USER
from app.main import create_app

AGENT_PATHS = {
    "/agent/v1/enroll",
    "/agent/v1/state",
    "/agent/v1/ack",
    "/agent/v1/heartbeat",
    "/agent/v1/metrics",
    "/agent/v1/commands/{command_id}/result",
}
BODY_SCHEMAS = ("AckIn", "HeartbeatIn", "NodeMetrics", "CommandResultIn")
FINGERPRINT = "9f8a3c17d4e05b2619c7a8f403d2e15b6c7a8d9e"
IDENTITY = "stub-identity-token-000000000000000000000000000"
HEADERS = {
    "X-Agent-Version": "0.1.0",
    "X-Client-Fingerprint": FINGERPRINT,
    "X-Node-Identity": IDENTITY,
}
BEHIND = {"config_version": 0, "users_seq": 0, "generation": 1}
CURRENT = {
    "config_version": composition.STUB_CONFIG_VERSION,
    "users_seq": composition.STUB_USERS_SEQ,
    "generation": 1,
}
SNAPSHOT = {**CURRENT, "full": 1}
VALID_HEARTBEAT: dict[str, Any] = {
    "agent_version": "0.1.0",
    "xray_version": "26.9.1",
    "node_time": "2026-09-10T12:00:00Z",
    # Числа намеренно не равны STUB_CONFIG_VERSION (1) и STUB_USERS_SEQ (4): совпадение сделало бы
    # страж «маршрут передал тело» неотличимым от «маршрут подставил константы заглушки».
    "applied_config_version": 0,
    "applied_users_seq": 3,
}
VALID_METRICS: dict[str, Any] = {
    "ts": "2026-09-10T12:00:00Z",
    "cpu_pct": 3.5,
    "mem_pct": 41.25,
    "disk_pct": 12.5,
    "net_rx_bytes": 918273645,
    "net_tx_bytes": 1827364509,
    "online_ips": 17,
    "connections": 43,
}
RESULT_PATH = f"/agent/v1/commands/{STUB_COMMAND_ID}/result"
OTHER_NODE_ID = uuid.UUID("00000000-0000-7000-8000-0000000000bb")


async def as_other_node() -> CurrentNode:
    """Нода запроса с идентификатором, отличным от `STUB_NODE_ID`."""
    return CurrentNode(
        node=stub_node(node_id=OTHER_NODE_ID),
        fingerprint=FINGERPRINT,
        identity_token=IDENTITY,
        agent_version="0.1.0",
    )


OPERATIONS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/agent/v1/state", None),
    ("POST", "/agent/v1/ack", {"applied_config_version": 0, "applied_users_seq": 0}),
    ("POST", "/agent/v1/heartbeat", VALID_HEARTBEAT),
    ("POST", "/agent/v1/metrics", VALID_METRICS),
    ("POST", RESULT_PATH, {"status": "applied"}),
]


class NoDatabase:
    """Заглушки раздела в базу не ходят — обращение к пулу валит тест."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"обращение к базе: {name}")


@asynccontextmanager
async def agent(status: NodeStatus | None = None) -> AsyncIterator[httpx.AsyncClient]:
    """Клиент раздела `/agent/v1` без базы. `status` подменяет ноду запроса: заглушка
    `current_node` отдаёт `active`, а вентиль §4.6 надо проверить на всех восьми статусах."""
    app = create_app()
    app.dependency_overrides[db_pool] = NoDatabase
    if status is not None:

        async def as_status() -> CurrentNode:
            return CurrentNode(
                node=stub_node(status=status),
                fingerprint=FINGERPRINT,
                identity_token=IDENTITY,
                agent_version="0.1.0",
            )

        app.dependency_overrides[current_node] = as_status
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://control-plane") as client:
        yield client


def rows_of(body: dict[str, Any]) -> list[tuple[int, str]]:
    return [(row["updated_seq"], row["state"]) for row in body["users"]["rows"]]


def fields(schema: dict[str, Any], name: str) -> set[str]:
    return set(schema["components"]["schemas"][name]["properties"])


def required(schema: dict[str, Any], name: str) -> set[str]:
    return set(schema["components"]["schemas"][name].get("required", ()))


def enum_of(schema: dict[str, Any], name: str, field: str) -> list[str]:
    prop = schema["components"]["schemas"][name]["properties"][field]
    if "enum" in prop:
        return list(prop["enum"])
    return list(next(variant for variant in prop["anyOf"] if "enum" in variant)["enum"])


async def test_agent_api_contract_in_openapi(app_client: httpx.AsyncClient) -> None:
    """Критерии приёмки: все операции §5.2 этой задачи — в схеме, версия API в пути, `426`
    объявлен. Наборы полей и обязательность сверяются равенством: выпавшее поле контракта или
    ставшее необязательным иначе прошло бы молча."""
    schema = (await app_client.get("/openapi.json")).json()
    paths = schema["paths"]
    assert {p for p in paths if p.startswith("/agent")} == AGENT_PATHS
    assert {p for p in paths if p.startswith("/agent")} == {
        p for p in paths if p.startswith("/agent/v1/")
    }, "версия API в пути у каждой операции раздела"
    for path in AGENT_PATHS:
        for operation in paths[path].values():
            assert operation["tags"] == ["agent"], path
            assert "x-permission" not in operation, (path, "не операция панели")

    state = paths["/agent/v1/state"]["get"]
    assert set(state["responses"]) == {"200", "204", "401", "403", "409", "422", "426", "429"}
    query = {p["name"]: p["required"] for p in state["parameters"] if p["in"] == "query"}
    assert query == {
        "config_version": True,
        "users_seq": True,
        "generation": True,
        "full": False,
    }, "курсоры обязательны в обеих формах, снапшот — необязательный признак"
    for path, method in (
        ("/agent/v1/ack", "post"),
        ("/agent/v1/heartbeat", "post"),
        ("/agent/v1/metrics", "post"),
        ("/agent/v1/commands/{command_id}/result", "post"),
    ):
        assert set(paths[path][method]["responses"]) == {"204", "401", "403", "422", "426", "429"}

    assert fields(schema, "StateResponse") == {
        "config",
        "users",
        "generation",
        "resync_required",
        "commands",
    }
    assert required(schema, "StateResponse") == {
        "users",
        "generation",
        "resync_required",
        "commands",
    }, "конфигурации в ответе может не быть, остальное обязательно"
    assert fields(schema, "StateConfig") == {"version", "checksum", "json"}
    assert required(schema, "StateConfig") == {"version", "checksum", "json"}
    assert fields(schema, "Users") == {"seq", "full", "rows"}
    assert required(schema, "Users") == {"seq", "full", "rows"}
    assert fields(schema, "UserRow") == {
        "user_id",
        "state",
        "xray_email",
        "quota_grant_bytes",
        "blocked_ips",
        "updated_seq",
        "credentials",
    }, "тег — у каждой строки: им нода применяет и отзыв, для которого ключей нет"
    assert fields(schema, "Credentials") == {"vless_uuid", "trojan_password", "version"}
    assert fields(schema, "Command") == {"id", "type", "payload", "issued_at", "expires_at"}
    assert required(schema, "UserRow") == {
        "user_id",
        "state",
        "xray_email",
        "quota_grant_bytes",
        "blocked_ips",
        "updated_seq",
    }, "credentials необязательны — их нет у строк, снимающих доступ; тег обязателен"
    assert required(schema, "Credentials") == set(fields(schema, "Credentials"))
    assert required(schema, "Command") == {"id", "type", "issued_at", "expires_at"}, (
        "срок обязателен: агент сверяет expires_at перед исполнением"
    )
    assert fields(schema, "AckIn") == {"applied_config_version", "applied_users_seq"}
    assert fields(schema, "HeartbeatIn") == {
        "agent_version",
        "xray_version",
        "node_time",
        "applied_config_version",
        "applied_users_seq",
    }
    assert fields(schema, "NodeMetrics") == {
        "ts",
        "cpu_pct",
        "mem_pct",
        "disk_pct",
        "net_rx_bytes",
        "net_tx_bytes",
        "online_ips",
        "connections",
    }, "колонки node_metrics §4.2.3 без node_id: ноду даёт identity запроса"
    assert fields(schema, "CommandResultIn") == {"status", "error"}
    # Обязательность входных тел — равенством: поле, ставшее необязательным, молча подменяется
    # умолчанием, и нода начинает отчитываться нулём вместо измерения.
    assert required(schema, "AckIn") == {"applied_config_version", "applied_users_seq"}
    assert required(schema, "HeartbeatIn") == set(fields(schema, "HeartbeatIn"))
    assert required(schema, "NodeMetrics") == set(fields(schema, "NodeMetrics"))
    assert required(schema, "CommandResultIn") == {"status"}, (
        "текст ошибки необязателен: перекрёстного правила «у failed он обязан быть» нет — "
        "нода вправе не объяснять отказ, а разбор причин — 001.76"
    )
    assert enum_of(schema, "UserRow", "state") == [
        "active",
        "suspended_quota",
        "suspended_admin",
        "expired",
        "removed",
    ], "перечисление user_node_state §4.2.3"
    assert enum_of(schema, "Command", "type") == [
        "restart_xray",
        "rotate_credentials",
        "collect_diagnostics",
        "update_agent",
    ], "перечисление command_type §4.2.3"
    assert enum_of(schema, "CommandResultIn", "status") == ["applied", "failed", "expired"], (
        "issued и delivered ставит Control Plane, от ноды они не принимаются"
    )
    for name in BODY_SCHEMAS:
        assert schema["components"]["schemas"][name]["additionalProperties"] is False, name
    # Урок WI-6: параметр зависимости, не объявленный как зависимость, уносит схему настроек в
    # неаутентифицированный /openapi.json. Раздел завёл четыре фабрики над `Depends(db_pool)`.
    assert "Settings" not in schema["components"]["schemas"], "настройки не публикуются"
    for path in AGENT_PATHS:
        for method, operation in paths[path].items():
            if method == "get":
                assert "requestBody" not in operation, path


async def test_state_answers_the_agent_and_needs_its_version() -> None:
    """TC-E2E-01: нода `active`, отставшая по курсорам, получает 200 с фиксированным
    `StateResponse`; без `X-Agent-Version` — 426."""
    async with agent() as client:
        response = await client.get("/agent/v1/state", params=BEHIND, headers=HEADERS)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["generation"] == composition.STUB_GENERATION
        assert body["resync_required"] is False
        assert body["config"]["version"] == composition.STUB_CONFIG_VERSION
        assert body["config"]["checksum"] == composition.STUB_CONFIG_CHECKSUM
        assert body["users"]["seq"] == composition.STUB_USERS_SEQ
        assert body["users"]["full"] is False, "дельта не полна: удалять по ней нельзя"
        assert rows_of(body) == [
            (1, "active"),
            (2, "suspended_quota"),
            (3, "removed"),
            (4, "active"),
        ]
        assert [command["id"] for command in body["commands"]] == [str(STUB_COMMAND_ID)]
        assert response.headers["cache-control"] == "no-store", "в ответе credentials §5.2"
        assert response.headers["content-type"].startswith("application/json"), (
            "тип ответа ставит литерал в маршруте, раз response_model снят, — и OpenAPI обещает "
            "именно его"
        )
        assert [row["credentials"] is not None for row in body["users"]["rows"]] == [
            True,
            False,
            False,
            True,
        ], "ключи несут только строки, выдающие доступ"

        headless = {k: v for k, v in HEADERS.items() if k != "X-Agent-Version"}
        refused = await client.get("/agent/v1/state", params=BEHIND, headers=headless)
        assert refused.status_code == 426, refused.text
        assert refused.json()["error"]["code"] == "unsupported_agent_version"
        assert refused.headers["retry-after"] == str(deps.RETRY_AFTER_UPGRADE)


async def test_heartbeat_and_telemetry_answer_without_body() -> None:
    """TC-E2E-02: heartbeat → 204 без тела; телеметрия, подтверждение курсоров и результат
    команды — тоже."""
    async with agent() as client:
        for method, path, body in OPERATIONS[1:]:
            response = await client.request(method, path, json=body, headers=HEADERS)
            assert response.status_code == 204, (path, response.text)
            assert response.content == b"", path


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Agent-Version": "0.1.0"},
        {"X-Agent-Version": "0.1.0", "X-Client-Fingerprint": FINGERPRINT},
        {"X-Agent-Version": "0.1.0", "X-Node-Identity": IDENTITY},
        {**HEADERS, "X-Client-Fingerprint": ""},
        {**HEADERS, "X-Node-Identity": ""},
        {**HEADERS, "X-Client-Fingerprint": "zz" * 20},
        {**HEADERS, "X-Client-Fingerprint": FINGERPRINT[:39]},
        {**HEADERS, "X-Client-Fingerprint": FINGERPRINT.upper()},
        {**HEADERS, "X-Client-Fingerprint": "a" * 41},
        {**HEADERS, "X-Client-Fingerprint": "a" * 63},
        {**HEADERS, "X-Client-Fingerprint": "a" * 65},
        {**HEADERS, "X-Node-Identity": "short"},
        {**HEADERS, "X-Node-Identity": "x" * 31},
        {**HEADERS, "X-Node-Identity": "x" * 129},
        {**HEADERS, "X-Node-Identity": "x" * 40 + "!"},
    ],
)
async def test_operations_need_both_identity_marks(headers: dict[str, str]) -> None:
    """Оба признака identity обязательны на каждой операции раздела и проверяются по форме:
    отпечаток — hex SHA-1 или SHA-256, токен — от 32 до 128 символов. Пустой отпечаток ставит
    сам прокси на публичном и enrollment `server` (§7.1)."""
    async with agent() as client:
        for method, path, body in OPERATIONS:
            response = await client.request(
                method, path, params=BEHIND if method == "GET" else None, json=body, headers=headers
            )
            assert response.status_code == 401, (path, response.text)
            assert response.json()["error"]["code"] == "unauthenticated", path


@pytest.mark.parametrize(
    "headers",
    [
        {**HEADERS, "X-Client-Fingerprint": "b" * 64},
        {**HEADERS, "X-Node-Identity": "x" * 32},
        {**HEADERS, "X-Node-Identity": "x" * 128},
    ],
)
async def test_both_fingerprint_widths_and_both_token_bounds_are_accepted(
    headers: dict[str, str],
) -> None:
    """Границы принимаются, а не только отвергаются. Отпечаток SHA-256 (64 символа) — та ветка,
    которая пойдёт в работу: `node_identities.cert_fingerprint` объявлен как 64 hex, а SHA-1 от
    `$ssl_client_fingerprint` — временное состояние до решения 001.25. Без этого случая ветка
    не исполняется ничем, и её потеря обнаружилась бы отказом всему парку."""
    assert deps.IDENTITY_MIN_CHARS == 32
    assert deps.IDENTITY_MAX_CHARS == 128
    async with agent() as client:
        response = await client.get("/agent/v1/state", params=BEHIND, headers=headers)
    assert response.status_code == 200, response.text


async def test_the_pauses_the_server_asks_for_are_the_declared_ones() -> None:
    """`Retry-After` — единственный рычаг, которым сервер управляет частотой опроса агента.
    Значения закреплены литералами: ассерт, сверяющий заголовок с той же константой, растёт
    вместе с ней и не заметит, как пауза станет нулевой."""
    assert deps.RETRY_AFTER_UPGRADE == 3600, "обновление парка — действие администратора (UC-11)"
    assert composition.RETRY_AFTER_APPROVAL == 300, "подтверждение ноды — тоже (UC-01 шаг 7)"
    assert composition.RETRY_AFTER_RESYNC == 1, "снапшот нода берёт сама, немедленно"


async def test_the_node_marks_reach_the_dependency_unswapped() -> None:
    """Отпечаток и токен попадают в свои поля: по ним 001.25 ищет ноду и сверяет identity, а
    перепутанные местами они дадут поиск по токену и сверку по отпечатку."""
    seen: list[CurrentNode] = []

    async def capture(node: deps.Agent) -> CurrentNode:
        seen.append(node)
        return node

    app = create_app()
    app.dependency_overrides[db_pool] = NoDatabase
    app.dependency_overrides[deps.served_node] = capture
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://control-plane") as client:
        assert (
            await client.get("/agent/v1/state", params=BEHIND, headers=HEADERS)
        ).status_code == 200
    assert len(seen) == 1
    assert (seen[0].fingerprint, seen[0].identity_token) == (FINGERPRINT, IDENTITY)
    assert seen[0].agent_version == "0.1.0"


async def test_unsupported_agent_versions_are_refused() -> None:
    """426 — на отсутствие заголовка, неразобранное значение, длину сверх предела и версию ниже
    поддерживаемой. Границы заданы литералами, значения констант закреплены отдельно: иначе
    страж рос бы вместе с тем, что проверяет. Принимаемые значения включают саму границу."""
    assert deps.MIN_AGENT_VERSION == (0, 1, 0)
    assert deps.AGENT_VERSION_MAX_CHARS == 64
    too_long = "0.1.0-" + "a" * 59  # 65 символов
    at_limit = "0.1.0-" + "a" * 58  # ровно 64
    assert (len(too_long), len(at_limit)) == (65, 64)
    async with agent() as client:
        for value in ("", "0.0.9", "0.0.999", "0.1", "v0.1.0", "latest", "0.1.0.0", too_long):
            response = await client.get(
                "/agent/v1/state", params=BEHIND, headers={**HEADERS, "X-Agent-Version": value}
            )
            assert response.status_code == 426, (value, response.text)
        for value in ("0.1.0", "0.1.1", "1.0.0", "0.1.0-rc1", "0.1.0+build.7", "10.0.0", at_limit):
            response = await client.get(
                "/agent/v1/state", params=BEHIND, headers={**HEADERS, "X-Agent-Version": value}
            )
            assert response.status_code == 200, (value, response.text)
    # Юникодные цифры до приложения по HTTP не доходят (заголовки декодируются как latin-1),
    # поэтому ``re.ASCII`` проверяется прямым вызовом зависимости, а не через клиент.
    with pytest.raises(ApiError) as refused:
        await deps.supported_agent_version("١.١.٠")
    assert refused.value.status == 426


@pytest.mark.parametrize("status", sorted(STREAM_GATE))
@pytest.mark.parametrize("form", ["delta", "snapshot"])
async def test_status_gate_decides_what_the_node_receives(status: NodeStatus, form: str) -> None:
    """Вентиль §4.6 — единственный источник правил, и снапшот его не обходит: `?full=1` идёт
    через тот же вентиль, что и дельта. `pending` — отказ 403 на любой форме; `provisioning` —
    конфигурация без состава; `disabled` и `suspended` — только строки, снимающие доступ, с
    `resync_required`, без команд и **без признака полноты**: приняв такой ответ за снапшот,
    нода удалила бы у себя всех, кого скрыл вентиль."""
    gate = STREAM_GATE[status]
    params = BEHIND if form == "delta" else {**BEHIND, "full": 1}
    async with agent(status) as client:
        response = await client.get("/agent/v1/state", params=params, headers=HEADERS)
        if gate.refused:
            assert response.status_code == 403, response.text
            assert response.json()["error"]["code"] == "node_not_approved"
            assert response.headers["retry-after"] == str(composition.RETRY_AFTER_APPROVAL)
            return
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["config"] is not None) is gate.config
        if gate.composition == "none":
            assert rows_of(body) == [] and body["users"]["seq"] == BEHIND["users_seq"]
        elif gate.composition == "revocations":
            # Удаление обязано ехать строкой и в снапшоте: отсутствие значит «удали» только в
            # ответе, объявившем себя полным, а этот полным не является.
            assert rows_of(body) == [(2, "suspended_quota"), (3, "removed")]
            assert body["users"]["seq"] == composition.STUB_USERS_SEQ, (
                "курсор идёт по выборке до фильтра по статусу, а не по выданным строкам: иначе "
                "нода застревала бы всякий раз, когда самое свежее изменение вентиль скрыл"
            )
            assert max(row["updated_seq"] for row in body["users"]["rows"]) < body["users"]["seq"]
        else:
            expected = (
                [(1, "active"), (2, "suspended_quota"), (4, "active")]
                if form == "snapshot"
                else [(1, "active"), (2, "suspended_quota"), (3, "removed"), (4, "active")]
            )
            assert rows_of(body) == expected
            assert body["users"]["seq"] == composition.STUB_USERS_SEQ
        assert body["users"]["full"] is (form == "snapshot" and gate.composition == "all")
        assert body["resync_required"] is gate.filters_composition
        assert bool(body["commands"]) is gate.commands


@pytest.mark.parametrize(("method", "path", "body"), OPERATIONS)
async def test_a_pending_node_is_served_nothing(
    method: str, path: str, body: dict[str, Any] | None
) -> None:
    """§4.6: неподтверждённой ноде раздел не выдаёт ничего — ни потоков, ни приёма
    подтверждений. Иначе нода, которой состояние отвечает 403, объявила бы себя применившей
    конфигурацию через `POST /ack` и подтолкнула бы переход `provisioning` → `active`."""
    async with agent("pending") as client:
        response = await client.request(
            method, path, params=BEHIND if method == "GET" else None, json=body, headers=HEADERS
        )
        assert response.status_code == 403, (path, response.text)
        assert response.json()["error"]["code"] == "node_not_approved", path


async def test_snapshot_and_delta_differ_the_way_the_agent_expects() -> None:
    """Снапшот не несёт `removed` (нода удаляет отсутствующих) и ставит курсор на текущий номер
    изменения, а не на наибольший из выданных строк. Нода на текущих курсорах получает 204."""
    async with agent() as client:
        snapshot = await client.get("/agent/v1/state", params=SNAPSHOT, headers=HEADERS)
        assert snapshot.status_code == 200, snapshot.text
        body = snapshot.json()
        assert rows_of(body) == [(1, "active"), (2, "suspended_quota"), (4, "active")]
        assert body["users"]["seq"] == composition.STUB_USERS_SEQ
        assert body["users"]["full"] is True
        assert composition.STUB_USERS_SEQ == max(
            row.updated_seq for row in composition.stub_rows(stub_node().id)
        ), "курсор снапшота — текущий номер изменения, включая строки, в снапшот не попавшие"

        nothing = await client.get("/agent/v1/state", params=CURRENT, headers=HEADERS)
        assert nothing.status_code == 204 and nothing.content == b""
        assert nothing.headers["cache-control"] == "no-store"

        for cursor in ("config_version", "users_seq"):
            behind = {**CURRENT, cursor: int(CURRENT[cursor]) - 1}
            response = await client.get("/agent/v1/state", params=behind, headers=HEADERS)
            assert response.status_code == 200, (cursor, response.text)
            fresh = response.json()
            assert (fresh["config"] is not None) is (cursor == "config_version"), (
                "поток, по которому нода не отстала, в дельту не попадает"
            )


async def test_a_provisioning_node_that_applied_the_config_is_held() -> None:
    """§5.2 про `provisioning`: состав пуст, пробуждение немедленного ответа не даёт, удержание
    идёт до таймаута — то есть 204. Курсор состава у такой ноды не двигается, и считать её
    отставшей по нему значило бы отвечать ей 200 на каждый запрос вечно."""
    async with agent("provisioning") as client:
        applied = {
            "config_version": composition.STUB_CONFIG_VERSION,
            "users_seq": 0,
            "generation": 1,
        }
        assert (
            await client.get("/agent/v1/state", params=applied, headers=HEADERS)
        ).status_code == 204
        behind = await client.get("/agent/v1/state", params=BEHIND, headers=HEADERS)
        assert behind.status_code == 200, "конфигурацию, которой у неё нет, нода получает"
        assert behind.json()["users"]["rows"] == []


async def test_a_cursor_ahead_of_the_server_is_refused_not_answered_with_204() -> None:
    """Нода недоверенная (§11.3): курсор больше выданного — это либо восстановление Control
    Plane из копии, либо подделка. Молчаливый 204 здесь означал бы, что нода одним числом в
    строке запроса навсегда отключила себе доставку отзывов, а сервер считает её
    синхронизированной."""
    async with agent() as client:
        for params in (
            {**CURRENT, "users_seq": composition.STUB_USERS_SEQ + 1},
            {**CURRENT, "config_version": composition.STUB_CONFIG_VERSION + 1},
            {**CURRENT, "users_seq": 2**63 - 1},
            {**CURRENT, "config_version": 2**31 - 1},
        ):
            response = await client.get("/agent/v1/state", params=params, headers=HEADERS)
            assert response.status_code == 409, (params, response.text)
            assert response.json()["error"]["code"] == "cursor_ahead", response.text
            assert response.headers["retry-after"] == str(composition.RETRY_AFTER_RESYNC)


async def test_a_snapshot_is_the_way_out_of_being_ahead() -> None:
    """Отказ `cursor_ahead` велит взять снапшот — значит снапшот обязан обслуживаться, иначе
    выхода нет: после восстановления Control Plane из копии впереди окажется весь парк сразу, и
    каждая нода будет долбить отказ с `Retry-After: 1` вечно."""
    async with agent() as client:
        ahead = {**CURRENT, "users_seq": composition.STUB_USERS_SEQ + 5}
        assert (
            await client.get("/agent/v1/state", params=ahead, headers=HEADERS)
        ).status_code == 409, "дельта по-прежнему отклоняется"
        snapshot = await client.get("/agent/v1/state", params={**ahead, "full": 1}, headers=HEADERS)
        assert snapshot.status_code == 200, snapshot.text
        body = snapshot.json()
        assert body["users"]["full"] is True
        assert body["users"]["seq"] == composition.STUB_USERS_SEQ, "курсор ставит сервер"


async def test_a_node_that_owes_a_resync_is_told_so_even_on_current_cursors() -> None:
    """§5.2: признак нода получает **при выходе** из фильтрующего статуса — то есть уже на
    текущих курсорах. Молчаливый 204 съел бы поручение ровно там, где оно нужно, и строки,
    скрытые вентилем, не пришли бы никогда."""
    app = create_app()
    app.dependency_overrides[db_pool] = NoDatabase

    async def owing() -> CurrentNode:
        return CurrentNode(
            node=stub_node().model_copy(update={"resync_required": True}),
            fingerprint=FINGERPRINT,
            identity_token=IDENTITY,
            agent_version="0.1.0",
        )

    app.dependency_overrides[current_node] = owing
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://control-plane") as client:
        response = await client.get("/agent/v1/state", params=CURRENT, headers=HEADERS)
    assert response.status_code == 200, "204 здесь потерял бы поручение о пересинхронизации"
    assert response.json()["resync_required"] is True
    async with agent() as plain:
        assert (
            await plain.get("/agent/v1/state", params=CURRENT, headers=HEADERS)
        ).status_code == 204, "без признака — по-прежнему 204"


async def test_an_empty_selection_leaves_the_cursor_where_the_node_put_it() -> None:
    """«При пустой выборке курсор не двигается и равен присланному» — правило, а не совпадение
    с нулём: нода, отставшая только по конфигурации, не должна получать откат курсора состава,
    иначе следующей дельтой ей поедет весь состав с ключами."""
    async with agent() as client:
        behind_on_config = {**CURRENT, "config_version": 0}
        answer = (
            await client.get("/agent/v1/state", params=behind_on_config, headers=HEADERS)
        ).json()
        assert answer["config"] is not None and answer["users"]["rows"] == []
        assert answer["users"]["seq"] == composition.STUB_USERS_SEQ, "курсор на месте"
    async with agent("provisioning") as client:
        held = {**CURRENT, "config_version": 0}
        answer = (await client.get("/agent/v1/state", params=held, headers=HEADERS)).json()
        assert answer["users"]["seq"] == composition.STUB_USERS_SEQ, (
            "закрытый поток курсор не трогает"
        )


async def test_a_pending_node_does_not_even_get_its_heartbeat_recorded() -> None:
    """Вентиль маршрута нужен сам по себе: без него `heartbeat` успел бы отметить удар до того,
    как откажет подтверждение курсоров, — и неподтверждённая нода поддерживала бы себя живой в
    `nodes.last_heartbeat_at` (001.30)."""
    statuses = RecordingStatuses()
    app = create_app()
    app.dependency_overrides[db_pool] = NoDatabase
    app.dependency_overrides[get_status_service] = lambda: statuses

    async def as_pending() -> CurrentNode:
        return CurrentNode(
            node=stub_node(status="pending"),
            fingerprint=FINGERPRINT,
            identity_token=IDENTITY,
            agent_version="0.1.0",
        )

    app.dependency_overrides[current_node] = as_pending
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://control-plane") as client:
        response = await client.post("/agent/v1/heartbeat", json=VALID_HEARTBEAT, headers=HEADERS)
    assert response.status_code == 403, response.text
    assert statuses.beats == [], "удар не отмечен: маршрут отказал до домена"


async def test_wrong_generation_demands_a_snapshot() -> None:
    """Поколение identity, отличное от действующего, — 409: дельты к её состоянию неприменимы
    (§5.2). Отказ идёт и на запрос снапшота: поколение сверяется до всего остального."""
    async with agent() as client:
        for params in ({**BEHIND, "generation": 2}, {**BEHIND, "generation": 2, "full": 1}):
            response = await client.get("/agent/v1/state", params=params, headers=HEADERS)
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == "generation_mismatch"
            assert response.headers["retry-after"] == str(composition.RETRY_AFTER_RESYNC)


@pytest.mark.parametrize(
    ("path", "field", "value"),
    [
        ("/agent/v1/metrics", field, value)
        for field in ("cpu_pct", "mem_pct", "disk_pct")
        for value in (-0.1, 100.5)
    ]
    + [
        ("/agent/v1/metrics", field, value)
        for field in ("net_rx_bytes", "net_tx_bytes")
        for value in (-1, 2**63)
    ]
    + [
        ("/agent/v1/metrics", field, value)
        for field in ("online_ips", "connections")
        for value in (-1, 2**31)
    ]
    + [("/agent/v1/heartbeat", "applied_config_version", value) for value in (-1, 2**31)]
    + [("/agent/v1/heartbeat", "applied_users_seq", value) for value in (-1, 2**63)],
)
async def test_every_bounded_field_is_bounded_on_both_sides(
    path: str, field: str, value: float
) -> None:
    """Каждое поле с объявленной границей проверяется с обеих сторон. Перечислять случаи вручную
    значит покрыть по одному полю на класс и решить, что покрыт класс: у соседей границу можно
    снять молча. Ширина колонок — §4.2.3, доли ресурсов — §4.7."""
    body = dict(VALID_METRICS if path.endswith("metrics") else VALID_HEARTBEAT)
    body[field] = value
    async with agent() as client:
        response = await client.post(path, json=body, headers=HEADERS)
    assert response.status_code == 422, (field, value, response.text)
    assert response.json()["error"]["code"] == "validation_error"


async def test_cursors_and_bodies_are_validated() -> None:
    """Курсоры — целые от нуля до ширины колонки §4.2.3; тела закрыты для лишних полей, доли
    ресурсов ограничены сотней, счётчики неотрицательны и не шире колонки, время ноды — со
    смещением. Границы задаются литералами, значения констант закреплены отдельно."""
    from app.domain.statuses import INT4_MAX, INT8_MAX

    assert (INT4_MAX, INT8_MAX) == (2**31 - 1, 2**63 - 1)
    assert get_args(Version)[1].max_length == 64, "длина версии в теле — как у заголовка"
    async with agent() as client:
        bad_queries: Iterable[dict[str, Any]] = (
            {"users_seq": 0, "generation": 0},
            {**BEHIND, "config_version": -1},
            {**BEHIND, "users_seq": "abc"},
            {**BEHIND, "generation": 1, "full": "maybe"},
            {**BEHIND, "config_version": 2**31},
            {**BEHIND, "users_seq": 2**63},
            {**BEHIND, "generation": 2**31},
        )
        for params in bad_queries:
            response = await client.get("/agent/v1/state", params=params, headers=HEADERS)
            assert response.status_code == 422, (params, response.text)
            assert response.json()["error"]["code"] == "validation_error"

        bad_bodies: Iterable[tuple[str, dict[str, Any]]] = (
            ("/agent/v1/ack", {}),
            ("/agent/v1/ack", {"applied_config_version": -1, "applied_users_seq": 0}),
            ("/agent/v1/ack", {"applied_config_version": 0, "applied_users_seq": -1}),
            ("/agent/v1/ack", {"applied_config_version": 2**31, "applied_users_seq": 0}),
            ("/agent/v1/ack", {"applied_config_version": 0, "applied_users_seq": 2**63}),
            ("/agent/v1/ack", {"applied_config_version": 0, "applied_users_seq": 0, "x": 1}),
            ("/agent/v1/heartbeat", {k: v for k, v in VALID_HEARTBEAT.items() if k != "node_time"}),
            ("/agent/v1/heartbeat", {**VALID_HEARTBEAT, "node_time": "2026-09-10T12:00:00"}),
            ("/agent/v1/heartbeat", {**VALID_HEARTBEAT, "agent_version": ""}),
            ("/agent/v1/heartbeat", {**VALID_HEARTBEAT, "xray_version": ""}),
            ("/agent/v1/heartbeat", {**VALID_HEARTBEAT, "xray_version": "26.9.1‮"}),
            ("/agent/v1/metrics", {**VALID_METRICS, "cpu_pct": 100.5}),
            ("/agent/v1/metrics", {**VALID_METRICS, "mem_pct": -0.1}),
            ("/agent/v1/metrics", {**VALID_METRICS, "disk_pct": 101}),
            ("/agent/v1/metrics", {**VALID_METRICS, "connections": -1}),
            ("/agent/v1/metrics", {**VALID_METRICS, "online_ips": 2**31}),
            ("/agent/v1/metrics", {**VALID_METRICS, "net_rx_bytes": 2**63}),
            ("/agent/v1/metrics", {k: v for k, v in VALID_METRICS.items() if k != "online_ips"}),
            ("/agent/v1/metrics", {**VALID_METRICS, "ts": "вчера"}),
            ("/agent/v1/metrics", {**VALID_METRICS, "ts": "2026-09-10T12:00:00"}),
            ("/agent/v1/heartbeat", {**VALID_HEARTBEAT, "agent_version": "0" * 65}),
            (RESULT_PATH, {"status": "issued"}),
            (RESULT_PATH, {"status": "delivered"}),
            (RESULT_PATH, {"status": "applied", "error": "x" * 2001}),
            (RESULT_PATH, {}),
        )
        for path, body in bad_bodies:
            response = await client.post(path, json=body, headers=HEADERS)
            assert response.status_code == 422, (path, str(body)[:60], response.text)
            assert response.json()["error"]["code"] == "validation_error"
        # Сами границы принимаются: страж меряет предел, а не «что-то большое».
        from app.domain.commands import ERROR_MAX_CHARS

        assert ERROR_MAX_CHARS == 2000
        floors: dict[str, Any] = dict.fromkeys(("cpu_pct", "mem_pct", "disk_pct"), 0)
        floors |= dict.fromkeys(("net_rx_bytes", "net_tx_bytes", "online_ips", "connections"), 0)
        ceilings: dict[str, Any] = dict.fromkeys(("cpu_pct", "mem_pct", "disk_pct"), 100)
        ceilings |= dict.fromkeys(("net_rx_bytes", "net_tx_bytes"), 2**63 - 1)
        ceilings |= dict.fromkeys(("online_ips", "connections"), 2**31 - 1)
        for path, body in (
            ("/agent/v1/metrics", {**VALID_METRICS, **floors}),
            ("/agent/v1/metrics", {**VALID_METRICS, **ceilings}),
            ("/agent/v1/heartbeat", {**VALID_HEARTBEAT, "applied_users_seq": 0}),
            (
                "/agent/v1/heartbeat",
                {
                    **VALID_HEARTBEAT,
                    "applied_config_version": 2**31 - 1,
                    "applied_users_seq": 2**63 - 1,
                },
            ),
            (RESULT_PATH, {"status": "failed", "error": "x" * 2000}),
            (
                "/agent/v1/ack",
                {"applied_config_version": 2**31 - 1, "applied_users_seq": 2**63 - 1},
            ),
        ):
            response = await client.post(path, json=body, headers=HEADERS)
            assert response.status_code == 204, (path, str(body)[:70], response.text)
        broken = await client.post(
            "/agent/v1/commands/not-a-uuid/result", json={"status": "applied"}, headers=HEADERS
        )
        assert broken.status_code == 422, broken.text


class RecordingStatuses(StatusService):
    """Служба статусов, которая запоминает вызовы вместо записи в базу."""

    def __init__(self) -> None:
        super().__init__(None)
        self.beats: list[tuple[uuid.UUID, dt.datetime, HeartbeatIn]] = []
        self.telemetry: list[tuple[uuid.UUID, NodeMetrics]] = []

    async def on_heartbeat(self, node_id: uuid.UUID, at: dt.datetime, beat: HeartbeatIn) -> None:
        self.beats.append((node_id, at, beat))

    async def on_metrics(self, node_id: uuid.UUID, metrics: NodeMetrics) -> None:
        self.telemetry.append((node_id, metrics))


class RecordingStreams(CompositionService):
    def __init__(self) -> None:
        super().__init__(None)
        self.acks: list[tuple[uuid.UUID, int, int]] = []

    async def ack(self, node: Node, config_version: int, users_seq: int) -> None:
        self.acks.append((node.id, config_version, users_seq))


class RecordingCommands(CommandService):
    def __init__(self) -> None:
        super().__init__(None)
        self.results: list[tuple[uuid.UUID, uuid.UUID, AgentCommandStatus, str | None]] = []
        self.asked: list[uuid.UUID] = []

    async def pending(self, node: Node) -> list[Command]:
        self.asked.append(node.id)
        return []

    async def result(
        self,
        node: Node,
        command_id: uuid.UUID,
        status: AgentCommandStatus,
        error: str | None = None,
    ) -> None:
        self.results.append((node.id, command_id, status, error))


async def test_routes_hand_the_parsed_body_to_the_domain() -> None:
    """Маршрут, который проверил тело и выбросил его, отвечает 204 так же, как рабочий: код
    ответа этого не показывает. Поэтому службы подменяются записывающими — heartbeat обязан
    отметить удар **временем сервера** (пороги Н-15 меряет Control Plane, а не часы ноды) и
    подтвердить применённые курсоры, телеметрия и результат команды — дойти до своих служб с
    разобранными значениями, а результат — ещё и с идентификатором отчитавшейся ноды."""
    statuses, streams, commands = RecordingStatuses(), RecordingStreams(), RecordingCommands()
    app = create_app()
    app.dependency_overrides[db_pool] = NoDatabase
    # Нода запроса намеренно не равна фиксированной: ожидание, побайтово совпадающее с
    # `STUB_NODE_ID`, не отличило бы «маршрут донёс ноду» от «маршрут подставил константу».
    app.dependency_overrides[current_node] = as_other_node
    app.dependency_overrides[get_status_service] = lambda: statuses
    app.dependency_overrides[get_composition_service] = lambda: streams
    app.dependency_overrides[get_command_service] = lambda: commands
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    before = dt.datetime.now(dt.UTC)
    async with httpx.AsyncClient(transport=transport, base_url="http://control-plane") as client:
        assert (
            await client.post("/agent/v1/heartbeat", json=VALID_HEARTBEAT, headers=HEADERS)
        ).status_code == 204
        assert (
            await client.post("/agent/v1/metrics", json=VALID_METRICS, headers=HEADERS)
        ).status_code == 204
        assert (
            await client.post(
                "/agent/v1/ack",
                json={"applied_config_version": 7, "applied_users_seq": 9},
                headers=HEADERS,
            )
        ).status_code == 204
        assert (
            await client.post(
                RESULT_PATH, json={"status": "failed", "error": "xray не поднялся"}, headers=HEADERS
            )
        ).status_code == 204
    after = dt.datetime.now(dt.UTC)

    node_id = OTHER_NODE_ID
    assert node_id != stub_node().id, "страж сверяет запрошенную ноду, а не фиксированную"
    assert [beat[0] for beat in statuses.beats] == [node_id]
    stamped, beat = statuses.beats[0][1], statuses.beats[0][2]
    assert before <= stamped <= after, "отметка — время Control Plane"
    assert beat.node_time == dt.datetime(2026, 9, 10, 12, tzinfo=dt.UTC), "время ноды идёт рядом"
    assert stamped != beat.node_time, "часы ноды не подменяют измерение сервера"
    assert (beat.agent_version, beat.xray_version) == ("0.1.0", "26.9.1"), (
        "версии из heartbeat доходят до домена: это единственное место, где нода сообщает их "
        "после enrollment, и колонки nodes.agent_version/xray_version §4.2.3 берутся отсюда"
    )
    assert [item[0] for item in statuses.telemetry] == [node_id]
    recorded = statuses.telemetry[0][1]
    assert (recorded.cpu_pct, recorded.connections) == (
        VALID_METRICS["cpu_pct"],
        VALID_METRICS["connections"],
    ), "телеметрия дошла до домена разобранной, а не была отброшена маршрутом"
    assert streams.acks == [(node_id, 0, 3), (node_id, 7, 9)], (
        "курсоры подтверждает и heartbeat (§5.2 несёт applied_*), и отдельная операция"
    )
    assert commands.results == [(node_id, STUB_COMMAND_ID, "failed", "xray не поднялся")]


async def test_the_command_channel_is_the_one_from_the_dependency_graph() -> None:
    """Служба команд у выдачи состояния — та же, что в графе зависимостей: своя, созданная
    внутри `CompositionService`, разошлась бы с настоящей в 001.76 молча."""

    commands = RecordingCommands()
    app = create_app()
    app.dependency_overrides[db_pool] = NoDatabase
    app.dependency_overrides[current_node] = as_other_node
    app.dependency_overrides[get_command_service] = lambda: commands
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://control-plane") as client:
        response = await client.get("/agent/v1/state", params=BEHIND, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["commands"] == [], "подменённая служба дошла до выдачи состояния"
    assert commands.asked == [OTHER_NODE_ID], (
        "канал спрошен про ту ноду, что пришла: подстановка фиксированного идентификатора "
        "выдала бы в 001.76 чужие команды"
    )


async def test_issued_command_expires_after_its_ttl() -> None:
    """`issue` считает срок от момента выдачи: команда без срока исполнялась бы нодой, которая
    получила её после возвращения из offline через неделю (§5.2, сверка `expires_at`)."""
    issued_at = dt.datetime(2026, 9, 10, 12, tzinfo=dt.UTC)
    command = await CommandService(None).issue(
        stub_node().id, "restart_xray", {}, dt.timedelta(minutes=10), now=issued_at
    )
    assert command.issued_at == issued_at
    assert command.expires_at == issued_at + dt.timedelta(minutes=10)
    assert command.type == "restart_xray" and command.payload == {}


async def test_composition_publish_job_type_is_registered() -> None:
    """Задачи `composition.publish_user` очередь принимает уже сейчас: исполнитель выбирает
    только типы из реестра, и без обработчика они копились бы в `pending` (§5.4)."""
    from app.jobs.handlers.composition import publish_user

    assert PUBLISH_USER == "composition.publish_user"
    assert HANDLERS[PUBLISH_USER] is publish_user
