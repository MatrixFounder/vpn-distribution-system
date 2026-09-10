"""Сквозной сценарий UC-01 на заглушках (задача 001.24; R-02), живой стенд: контракт
``/api/v1/admin/nodes/*`` и ``POST /agent/v1/enroll`` в OpenAPI (схема enroll содержит сертификат,
CA и токен identity — критерий приёмки), TC-E2E-01 — запись ноды → bootstrap-токен (Н-24) →
обмен на identity → сверка отпечатка и версий → подтверждение (``pending`` → ``provisioning``)
→ карточка ноды; ручные статусы §4.6 с причиной; отзыв identity (UC-12 шаг 1, UC-01 A2); вывод
из эксплуатации. Enrollment — без сессии и CSRF (отдельный ``server`` nginx без клиентского
сертификата, §7.1; статический страж границы — ``tests/unit/test_proxy_contract.py``), но со
строгой валидацией тела. Сессия администратора, CSRF и ``x-permission`` для операций нод —
параметризованные тесты ``test_catalog`` по ``ADMIN_OPERATIONS``."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from app.agent_api.enroll import CSR_MAX_CHARS, get_node_service
from app.domain import nodes as domain
from app.main import create_app
from app.security.ca import STUB_CA_PEM, STUB_CLIENT_CERT_PEM, InternalCA, pem_der

from ._admin import NODE_ID, VALID_NODE, admin_stand

VALID_ENROLL: dict[str, Any] = {
    "bootstrap_token": "x" * 43,
    "csr_pem": domain.STUB_CSR_PEM,
    "agent_version": "0.1.0",
    "xray_version": "26.9.1",
}
NODE_STATUSES = [
    "pending",
    "provisioning",
    "active",
    "degraded",
    "offline",
    "maintenance",
    "disabled",
    "suspended",
]
OTHER_NODE_ID = "11111111-1111-7111-8111-111111111111"


def _ref(schema: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    name = ref["$ref"].rsplit("/", 1)[1]
    return schema["components"]["schemas"][name]  # type: ignore[no-any-return]


def _fields(schema: dict[str, Any], name: str) -> set[str]:
    return set(schema["components"]["schemas"][name]["properties"])


async def test_node_and_enroll_contracts_in_openapi(app_client: httpx.AsyncClient) -> None:
    """Критерии приёмки задачи: маршруты в OpenAPI, ответ enroll несёт сертификат, CA и токен
    identity. Наборы полей всех схем сверяются равенством: выпавшее поле контракта не проходит
    молча (pydantic просто игнорирует лишний аргумент конструктора заглушки)."""
    schema = (await app_client.get("/openapi.json")).json()
    paths = schema["paths"]

    enroll = paths["/agent/v1/enroll"]["post"]
    assert enroll["tags"] == ["agent"] and "x-permission" not in enroll, "не операция панели"
    # Enrollment опубликован ровно одним путём: второй монтаж роутера (в /api/v1 или в раздел
    # панели) виден здесь, а не только на стенде.
    assert {p for p in paths if "enroll" in p} == {"/agent/v1/enroll"}, sorted(paths)
    request = _ref(schema, enroll["requestBody"]["content"]["application/json"]["schema"])
    assert set(request["properties"]) == {
        "bootstrap_token",
        "csr_pem",
        "agent_version",
        "xray_version",
    }
    assert (
        set(request["required"]) == set(request["properties"])
        and request["additionalProperties"] is False
    )
    response = _ref(schema, enroll["responses"]["200"]["content"]["application/json"]["schema"])
    assert set(response["properties"]) == {"client_cert_pem", "ca_pem", "identity_token", "node_id"}
    assert set(response["required"]) == set(response["properties"]), "все четыре поля обязательны"
    assert {"401", "422"} <= set(enroll["responses"]), "отказы UC-01 A1 и по телу объявлены"

    node_in = _fields(schema, "NodeIn")
    assert node_in == {
        "code",
        "name",
        "country",
        "city",
        "provider",
        "public_ipv4",
        "public_ipv6",
        "fqdn",
        "billing_group_id",
        "access_group_ids",
        "bandwidth_mbps",
        "max_conn_per_ip",
        "legal_profile",
        "monthly_cost",
        "currency",
        "provider_account",
        "cost_valid_from",
        "cost_valid_to",
        "traffic_included_bytes",
        "traffic_overage_cost",
    }, "поля §4.2.3 nodes, задаваемые администратором"
    assert set(schema["components"]["schemas"]["NodeIn"]["required"]) == {
        "code",
        "name",
        "country",
        "city",
        "provider",
        "public_ipv4",
        "billing_group_id",
        "bandwidth_mbps",
        "max_conn_per_ip",
    }, "NOT NULL без умолчаний в базе — обязательны в теле"
    assert _fields(schema, "NodePatch") == node_in
    assert "required" not in schema["components"]["schemas"]["NodePatch"], "PATCH — подмножество"
    assert _fields(schema, "Node") == node_in | {
        "id",
        "status",
        "status_changed_at",
        "last_heartbeat_at",
        "agent_version",
        "xray_version",
        "multiplier_milli",
        "resync_required",
        "created_at",
        "decommissioned_at",
    }, "карточка = запись + состояние §4.6"
    assert schema["components"]["schemas"]["Node"]["properties"]["status"]["enum"] == NODE_STATUSES
    manual = schema["components"]["schemas"]["ManualStatusIn"]
    assert set(manual["properties"]) == {"status", "reason"}
    assert next(v for v in manual["properties"]["status"]["anyOf"] if "enum" in v)["enum"] == [
        "maintenance",
        "disabled",
        "suspended",
    ], "ручные статусы §4.6"
    assert manual["required"] == ["status"], "снятие — явный null"
    assert _fields(schema, "NodeState") == {
        "node_id",
        "status",
        "status_changed_at",
        "cursors",
        "resync_required",
        "heartbeat",
        "agent_version",
        "xray_version",
        "identity",
        "bootstrap_token_expires_at",
        "bootstrap_token_used_at",
    }
    assert _fields(schema, "Cursors") == {
        "desired_config_version",
        "applied_config_version",
        "desired_users_seq",
        "applied_users_seq",
        "applied_at",
    }, "оба потока §5.2"
    assert _fields(schema, "Heartbeat") == {"last_at", "missed", "ok"}
    assert _fields(schema, "Identity") == {
        "generation",
        "cert_fingerprint",
        "issued_at",
        "expires_at",
        "revoked_at",
    }, "node_identities §4.2.3"
    assert _fields(schema, "BootstrapToken") == {"node_id", "token", "expires_at"}


async def test_uc01_node_onboarding_on_stubs(pg_dsn: str, redis_url: str) -> None:
    """TC-E2E-01: шаги 1, 2–3, 5, 6, 7 и карточка ноды в фиксированных ответах; затем ручные
    статусы, отзыв identity и вывод из эксплуатации."""
    async with admin_stand(pg_dsn, redis_url) as admin:
        client, headers = admin.client, admin.headers
        # Шаг 1: запись ноды — pending, агент ещё ничего не предъявил.
        created = await client.post("/api/v1/admin/nodes", json=VALID_NODE, headers=headers)
        assert created.status_code == 201, created.text
        node = created.json()
        assert node["id"] == NODE_ID and node["status"] == "pending"
        assert node["code"] == "JP-Tokyo-01" and node["public_ipv4"] == "203.0.113.10"
        assert node["agent_version"] is None and node["last_heartbeat_at"] is None
        assert node["legal_profile"] == {"jurisdiction": "JP"}
        # Шаги 2–3: одноразовый токен на 60 минут (Н-24); повторная выдача — новый токен.
        before = dt.datetime.now(dt.UTC)
        issued = await client.post(
            f"/api/v1/admin/nodes/{NODE_ID}/bootstrap-token", headers=headers
        )
        assert issued.status_code == 201, issued.text
        token = issued.json()
        assert token["node_id"] == NODE_ID and len(token["token"]) == 43
        assert set(token["token"]) <= set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )
        after = dt.datetime.now(dt.UTC)
        expires_at = dt.datetime.fromisoformat(token["expires_at"])
        assert (
            before + domain.BOOTSTRAP_TOKEN_TTL <= expires_at <= after + domain.BOOTSTRAP_TOKEN_TTL
        )
        assert domain.BOOTSTRAP_TOKEN_TTL == dt.timedelta(minutes=60), "Н-24: не более 60 минут"
        again = await client.post(f"/api/v1/admin/nodes/{NODE_ID}/bootstrap-token", headers=headers)
        assert again.status_code == 201 and again.json()["token"] != token["token"]
        # Шаг 5: агент с другого адреса, без сессии и CSRF, обменивает токен на identity.
        async with admin.stand.client("198.51.100.7") as agent:
            enrolled = await agent.post(
                "/agent/v1/enroll", json={**VALID_ENROLL, "bootstrap_token": token["token"]}
            )
        assert enrolled.status_code == 200, enrolled.text
        identity = enrolled.json()
        assert set(identity) == {"client_cert_pem", "ca_pem", "identity_token", "node_id"}
        assert identity["node_id"] == NODE_ID and len(identity["identity_token"]) >= 32
        assert (
            pem_der(identity["client_cert_pem"], "CERTIFICATE")
            and identity["ca_pem"] == STUB_CA_PEM
        )
        fingerprint = InternalCA.fingerprint(identity["client_cert_pem"])
        # Шаг 6: администратор сверяет отпечаток и версии по состоянию ноды.
        state = await client.get(f"/api/v1/admin/nodes/{NODE_ID}/state")
        assert state.status_code == 200, state.text
        presented = state.json()
        assert presented["node_id"] == NODE_ID
        assert presented["identity"]["revoked_at"] is None
        assert presented["identity"]["cert_fingerprint"] == fingerprint, "тот же сертификат"
        assert presented["identity"]["generation"] == 1
        assert (presented["agent_version"], presented["xray_version"]) == (
            domain.STUB_AGENT_VERSION,
            domain.STUB_XRAY_VERSION,
        ), "версии, предъявленные агентом (заглушка отдаёт объявленные значения)"
        assert presented["cursors"]["applied_users_seq"] == 0
        assert presented["heartbeat"]["last_at"] is None
        assert presented["bootstrap_token_used_at"] is not None, "токен погашен обменом"
        # Заглушка не хранит состояние, но и не подменяет ноду: статус состояния и карточки
        # одной и той же ноды совпадает.
        card = await client.get(f"/api/v1/admin/nodes/{NODE_ID}")
        assert card.status_code == 200 and card.json()["status"] == presented["status"]
        assert card.json()["last_heartbeat_at"] is not None
        # Шаг 7: подтверждение — provisioning.
        approved = await client.post(f"/api/v1/admin/nodes/{NODE_ID}/approve", headers=headers)
        assert approved.status_code == 200 and approved.json()["status"] == "provisioning"
        assert approved.json()["agent_version"] == domain.STUB_AGENT_VERSION
        listed = await client.get("/api/v1/admin/nodes")
        assert listed.status_code == 200 and [n["id"] for n in listed.json()] == [NODE_ID]
        # Ручные статусы §4.6 и их снятие (null → автоматика, в заглушке active).
        for manual in ("maintenance", "disabled", "suspended"):
            response = await client.post(
                f"/api/v1/admin/nodes/{NODE_ID}/status",
                json={"status": manual, "reason": "работы"},
                headers=headers,
            )
            assert response.status_code == 200 and response.json()["status"] == manual, manual
        cleared = await client.post(
            f"/api/v1/admin/nodes/{NODE_ID}/status",
            json={"status": None, "reason": "работы завершены"},
            headers=headers,
        )
        assert cleared.status_code == 200 and cleared.json()["status"] == "active"
        for bad in ({"status": "active"}, {"status": "pending"}, {}, {"status": "Maintenance"}):
            response = await client.post(
                f"/api/v1/admin/nodes/{NODE_ID}/status", json=bad, headers=headers
            )
            assert response.status_code == 422, (bad, response.text)
        # UC-12 шаг 1 / UC-01 A2: отзыв identity — revoked_at в состоянии.
        revoked = await client.post(
            f"/api/v1/admin/nodes/{NODE_ID}/revoke-identity", headers=headers
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["identity"]["revoked_at"] is not None
        assert revoked.json()["identity"]["cert_fingerprint"] == fingerprint
        # Изменение записи и вывод из эксплуатации.
        patched = await client.patch(
            f"/api/v1/admin/nodes/{NODE_ID}",
            json={"name": "Tokyo 1a", "fqdn": None},
            headers=headers,
        )
        assert patched.status_code == 200 and patched.json()["name"] == "Tokyo 1a"
        assert patched.json()["code"] == "JP-Tokyo-01", "непереданное поле не тронуто"
        gone = await client.delete(f"/api/v1/admin/nodes/{NODE_ID}", headers=headers)
        assert gone.status_code == 204 and gone.content == b""


async def test_operations_answer_about_the_node_from_the_path(pg_dsn: str, redis_url: str) -> None:
    """Заглушка отдаёт фиксированные значения, но идентификатор берёт из пути: карточка,
    состояние, подтверждение, ручной статус и отзыв identity другой ноды не выдают ноду
    ``STUB_NODE_ID`` (иначе панель 001.50 показала бы чужие данные)."""
    async with admin_stand(pg_dsn, redis_url) as admin:
        client, headers = admin.client, admin.headers
        for path, key in (
            (f"/api/v1/admin/nodes/{OTHER_NODE_ID}", "id"),
            (f"/api/v1/admin/nodes/{OTHER_NODE_ID}/state", "node_id"),
        ):
            response = await client.get(path)
            assert response.status_code == 200, response.text
            assert response.json()[key] == OTHER_NODE_ID, (path, response.json()[key])
        for path, key, body in (
            (f"/api/v1/admin/nodes/{OTHER_NODE_ID}/approve", "id", None),
            (f"/api/v1/admin/nodes/{OTHER_NODE_ID}/status", "id", {"status": "maintenance"}),
            (f"/api/v1/admin/nodes/{OTHER_NODE_ID}/revoke-identity", "node_id", None),
            (f"/api/v1/admin/nodes/{OTHER_NODE_ID}/bootstrap-token", "node_id", None),
        ):
            response = await client.post(path, json=body, headers=headers)
            assert response.status_code in (200, 201), (path, response.text)
            assert response.json()[key] == OTHER_NODE_ID, (path, response.json()[key])
        patched = await client.patch(
            f"/api/v1/admin/nodes/{OTHER_NODE_ID}", json={"name": "Osaka"}, headers=headers
        )
        assert patched.status_code == 200 and patched.json()["id"] == OTHER_NODE_ID


async def test_node_record_validation(pg_dsn: str, redis_url: str) -> None:
    """Поля §4.2.3: страна ISO alpha-2 в верхнем регистре, адрес IPv4, положительные лимиты,
    код без пробелов, обязательная тарифицируемая группа (R-18); PATCH: null для NOT NULL — 422."""
    async with admin_stand(pg_dsn, redis_url) as admin:
        client, headers = admin.client, admin.headers
        for bad in (
            {**VALID_NODE, "country": "jp"},
            {**VALID_NODE, "country": "JPN"},
            {**VALID_NODE, "public_ipv4": "300.1.1.1"},
            {**VALID_NODE, "public_ipv4": "2001:db8::1"},
            {**VALID_NODE, "public_ipv6": "203.0.113.10"},
            {**VALID_NODE, "bandwidth_mbps": 0},
            {**VALID_NODE, "max_conn_per_ip": -1},
            {**VALID_NODE, "code": "JP Tokyo 01"},
            {**VALID_NODE, "fqdn": "bad host"},
            {**VALID_NODE, "currency": "eur"},
            {**VALID_NODE, "monthly_cost": "-5"},
            {**VALID_NODE, "legal_profile": ["not", "an", "object"]},
            {k: v for k, v in VALID_NODE.items() if k != "billing_group_id"},
            {k: v for k, v in VALID_NODE.items() if k != "bandwidth_mbps"},
        ):
            response = await client.post("/api/v1/admin/nodes", json=bad, headers=headers)
            assert response.status_code == 422, (bad, response.text)
        for null_forbidden in (
            "code",
            "public_ipv4",
            "billing_group_id",
            "access_group_ids",
            "legal_profile",
        ):
            response = await client.patch(
                f"/api/v1/admin/nodes/{NODE_ID}", json={null_forbidden: None}, headers=headers
            )
            assert response.status_code == 422, (null_forbidden, response.text)
        optional_null = await client.patch(
            f"/api/v1/admin/nodes/{NODE_ID}",
            json={"public_ipv6": None, "monthly_cost": None},
            headers=headers,
        )
        assert optional_null.status_code == 200, optional_null.text
        assert (
            await client.patch("/api/v1/admin/nodes/not-a-uuid", json={}, headers=headers)
        ).status_code == 422


async def test_enroll_validates_body_and_needs_no_session(app_client: httpx.AsyncClient) -> None:
    """Enrollment без cookie и CSRF: валидное тело — 200 с сертификатом заглушки; ошибки тела —
    422 единого формата, а не 401/403; CSR обязан быть PEM-блоком CERTIFICATE REQUEST и не
    длиннее ``CSR_MAX_CHARS`` (тело enrollment-сервера nginx ограничено 64 КБ)."""
    ok = await app_client.post("/agent/v1/enroll", json=VALID_ENROLL)
    assert ok.status_code == 200, ok.text
    assert ok.json()["client_cert_pem"] == STUB_CLIENT_CERT_PEM
    cert_as_csr = STUB_CLIENT_CERT_PEM  # правильный PEM, но не запрос на сертификат
    # Валидный base64 длиннее предела, размер — литералом: страж мерит длину, а не испорченную
    # кодировку, и не растёт вместе с проверяемой константой (ревью 001.24, раунд 2).
    assert CSR_MAX_CHARS == 16 * 1024
    long_body = "QUJD" * 4096
    long_csr = (
        f"-----BEGIN CERTIFICATE REQUEST-----\n{long_body}\n-----END CERTIFICATE REQUEST-----\n"
    )
    assert len(long_csr) > CSR_MAX_CHARS
    for bad in (
        {},
        {**VALID_ENROLL, "bootstrap_token": "short"},
        # Алфавит токена — base64url выданного (001.28): вход сужается до отказа, а не после
        # него, потому что значение уедет в поиск по хешу (001.25).
        {**VALID_ENROLL, "bootstrap_token": "x" * 40 + "!"},
        {**VALID_ENROLL, "bootstrap_token": "x" * 20 + " " + "x" * 20},
        {**VALID_ENROLL, "csr_pem": "not a csr"},
        {**VALID_ENROLL, "csr_pem": cert_as_csr},
        {**VALID_ENROLL, "csr_pem": domain.STUB_CSR_PEM.replace("Y29u", "!!!!")},
        {**VALID_ENROLL, "csr_pem": "prefix " + domain.STUB_CSR_PEM},
        {
            **VALID_ENROLL,
            "csr_pem": "-----BEGIN CERTIFICATE REQUEST-----\n\n-----END CERTIFICATE REQUEST-----\n",
        },
        {**VALID_ENROLL, "csr_pem": long_csr},
        {**VALID_ENROLL, "agent_version": ""},
        {**VALID_ENROLL, "extra": 1},
        {k: v for k, v in VALID_ENROLL.items() if k != "xray_version"},
    ):
        response = await app_client.post("/agent/v1/enroll", json=bad)
        assert response.status_code == 422, (str(bad)[:80], response.text)
        assert response.json()["error"]["code"] == "validation_error", response.text
    reasons = (
        await app_client.post("/agent/v1/enroll", json={**VALID_ENROLL, "csr_pem": cert_as_csr})
    ).json()
    assert "CERTIFICATE REQUEST" in reasons["error"]["details"]["errors"][0]["msg"], reasons
    # Окончания строк CRLF допускает RFC 7468 §3 — агент на любой платформе не получает отказ.
    crlf = domain.STUB_CSR_PEM.replace("\n", "\r\n")
    assert (
        await app_client.post("/agent/v1/enroll", json={**VALID_ENROLL, "csr_pem": crlf})
    ).status_code == 200


async def test_csr_rejected_by_the_ca_is_a_request_error_not_a_server_error() -> None:
    """CSR, прошедший проверку рамки, но отвергнутый самим CA (разбор и подпись — 001.25), —
    422 ``invalid_csr`` без текста исключения наружу, а не 500."""

    class RejectingNodes:
        async def enroll(self, *args: object, **kwargs: object) -> object:
            raise ValueError("внутренняя подробность о ключе CA")

    app = create_app()
    app.dependency_overrides[get_node_service] = RejectingNodes
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://control-plane") as client:
        response = await client.post("/agent/v1/enroll", json=VALID_ENROLL)
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "invalid_csr", body
    assert "ключе CA" not in response.text, "текст исключения наружу не выносится"


@pytest.mark.parametrize("node_id", [NODE_ID, OTHER_NODE_ID])
async def test_state_and_card_agree_on_the_same_node(
    pg_dsn: str, redis_url: str, node_id: str
) -> None:
    """Карточка и состояние одной ноды не противоречат друг другу: статус, версии и
    ``resync_required`` совпадают (заглушка описывает одну ноду, а не две разные)."""
    async with admin_stand(pg_dsn, redis_url) as admin:
        card = (await admin.client.get(f"/api/v1/admin/nodes/{node_id}")).json()
        state = (await admin.client.get(f"/api/v1/admin/nodes/{node_id}/state")).json()
        assert card["id"] == state["node_id"] == node_id
        assert card["status"] == state["status"]
        assert card["resync_required"] == state["resync_required"]
        assert (card["agent_version"], card["xray_version"]) == (
            state["agent_version"],
            state["xray_version"],
        )
        assert uuid.UUID(card["id"])
