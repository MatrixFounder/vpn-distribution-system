"""Ввод ноды и её identity на живой базе (задача 001.25; UC-01, UC-12; R-02, R-44): запись ноды →
bootstrap-токен (Н-24, хеш, аннулирование прежнего) → обмен на лист внутреннего CA (адрес
источника, поколение, одноразовость, отказы UC-01 A1) → сверка признаков по состоянию (шаг 6) →
подтверждение (шаг 7) → запросы раздела ``/agent/v1`` по сертификату и токену → отзыв identity
(Н-31, UC-12 шаг 1, A1) → пересоздание новым токеном. Плюс контракт OpenAPI, валидация тела
enrollment, отказ CA как ошибка запроса и операции панели, которые не реализованы и отвечают 501
(изменение записи, ручной статус, вывод из эксплуатации).

Раздел ``/agent/v1`` здесь настоящий целиком: сертификат приходит заголовком ``X-Client-Cert`` в
той форме, в какой его передаёт nginx (``tests/_pki.py::escaped``), ``current_node`` ищет ноду в
базе. Какой нодой признан запрос, видно по отказу ``node_mismatch``: отчёт с чужим ``node_id``
отвечает идентификатором вызывающей ноды — тем, что взят из identity (§11.3).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from app.agent_api.enroll import CSR_MAX_CHARS, get_node_service
from app.main import create_app
from app.security.ca import CsrRejectedError, InternalCA, serial_hex
from app.security.sessions import SessionStore
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from tests._pki import STUB_CLIENT_CERT, STUB_CLIENT_CERT_PEM, escaped, make_csr
from tests._reports import VALID_REPORT

from ._admin import NODE_ID, VALID_NODE, admin_stand
from ._auth import AuthStand, auth_stand
from ._catalog import access_group, billing_group, catalog, unique_name

VALID_ENROLL: dict[str, Any] = {
    "bootstrap_token": "x" * 43,
    "csr_pem": make_csr(),
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
AGENT_IP = "198.51.100.7"  # адрес, с которого «нода» обменивает токен (документационный диапазон)
ADMIN_EMAIL = "t25-admin-%"  # уборка администраторов, заведённых этими тестами
BEHIND = {"config_version": "0", "users_seq": "0", "generation": "1"}


def _ref(schema: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    name = ref["$ref"].rsplit("/", 1)[1]
    return schema["components"]["schemas"][name]  # type: ignore[no-any-return]


def _fields(schema: dict[str, Any], name: str) -> set[str]:
    return set(schema["components"]["schemas"][name]["properties"])


def process_ca_pem() -> str:
    """Сертификат CA, который читает приложение теста (``CA_CERT_FILE`` — ``conftest.py``)."""
    return Path(os.environ["CA_CERT_FILE"]).read_text()


@dataclass
class Enrolled:
    """Что нода получила при обмене: сертификат, токен identity и ответ целиком."""

    node_id: str
    cert_pem: str
    identity_token: str
    body: dict[str, Any]

    @property
    def headers(self) -> dict[str, str]:
        """Запрос ноды в разделе ``/agent/v1`` так, как его видит приложение за nginx."""
        return {
            "X-Agent-Version": "0.1.0",
            "X-Client-Cert": escaped(self.cert_pem),
            "X-Node-Identity": self.identity_token,
        }

    @property
    def fingerprint(self) -> str:
        return InternalCA.fingerprint(self.cert_pem)


@dataclass
class Onboarding:
    """Стенд ввода ноды: настоящий администратор (строка ``admin_users`` — на неё ссылается
    ``bootstrap_tokens.created_by``) с сессией, группы каталога и подключение для сверки."""

    stand: AuthStand
    client: httpx.AsyncClient
    headers: dict[str, str]
    conn: asyncpg.Connection
    admin_id: uuid.UUID
    billing_group_id: uuid.UUID
    access_group_id: uuid.UUID

    async def create_node(self) -> dict[str, Any]:
        body = {
            **VALID_NODE,
            "code": unique_name("node"),
            "billing_group_id": str(self.billing_group_id),
            "access_group_ids": [str(self.access_group_id)],
        }
        created = await self.client.post("/api/v1/admin/nodes", json=body, headers=self.headers)
        assert created.status_code == 201, created.text
        return created.json()  # type: ignore[no-any-return]

    async def issue(self, node_id: str) -> dict[str, Any]:
        issued = await self.client.post(
            f"/api/v1/admin/nodes/{node_id}/bootstrap-token", headers=self.headers
        )
        assert issued.status_code == 201, issued.text
        assert issued.headers["cache-control"] == "no-store", "токен показывается один раз"
        return issued.json()  # type: ignore[no-any-return]

    async def enroll(
        self, token: str, csr_pem: str | None = None, ip: str = AGENT_IP
    ) -> httpx.Response:
        async with self.stand.client(ip) as agent:
            return await agent.post(
                "/agent/v1/enroll",
                json={**VALID_ENROLL, "bootstrap_token": token, "csr_pem": csr_pem or make_csr()},
            )

    async def enrolled(self, node_id: str) -> Enrolled:
        """Новый токен и обмен — нода в ``pending`` с действующей identity."""
        response = await self.enroll((await self.issue(node_id))["token"])
        assert response.status_code == 200, response.text
        body = response.json()
        return Enrolled(body["node_id"], body["client_cert_pem"], body["identity_token"], body)

    async def approve(self, node_id: str, fingerprint: str = "0" * 64) -> httpx.Response:
        """Шаг 7 — подтверждение той identity, чей отпечаток администратор сверил на шаге 6."""
        return await self.client.post(
            f"/api/v1/admin/nodes/{node_id}/approve",
            json={"cert_fingerprint": fingerprint},
            headers=self.headers,
        )

    async def revoke(self, node_id: str) -> httpx.Response:
        return await self.client.post(
            f"/api/v1/admin/nodes/{node_id}/revoke-identity", headers=self.headers
        )

    async def state(self, node_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/api/v1/admin/nodes/{node_id}/state")
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]

    async def as_agent(
        self, node: Enrolled | dict[str, str], path: str = "state"
    ) -> httpx.Response:
        """Запрос ноды: ``state`` — чтение состояния, ``whoami`` — отчёт с чужим ``node_id``,
        отказ на который называет ноду, которой признан запрос."""
        headers = node.headers if isinstance(node, Enrolled) else node
        async with self.stand.client(AGENT_IP) as agent:
            if path == "state":
                return await agent.get("/agent/v1/state", params=BEHIND, headers=headers)
            report = {**VALID_REPORT, "node_id": OTHER_NODE_ID}
            return await agent.post("/agent/v1/reports", json=report, headers=headers)


@asynccontextmanager
async def onboarding(pg_dsn: str, redis_url: str) -> AsyncIterator[Onboarding]:
    """Уборка — по префиксам (ноды и группы каталога — ``t19-``, администраторы — ``t25-admin-``):
    упавший посреди сценария тест не оставляет строк, о которые споткнётся следующий прогон.
    Администратор удаляется последним: на него ссылаются токены, а их снимает уборка каталога."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        try:
            async with catalog(pg_dsn) as conn:
                await conn.execute("delete from admin_users where email like $1", ADMIN_EMAIL)
                admin_id: uuid.UUID = await conn.fetchval(
                    "insert into admin_users (email, password_hash, role) "
                    "values ($1, 'не пароль', 'admin') returning id",
                    f"t25-admin-{uuid.uuid4().hex[:8]}@example.test",
                )
                store = SessionStore(stand.redis)
                session = await store.create("admin", str(admin_id), "203.0.113.200", "test", 600)
                try:
                    billing = await billing_group(conn, "n25")
                    access = await access_group(conn, "n25")
                    async with stand.client("203.0.113.200") as client:
                        client.cookies.set("sid", session.id)
                        yield Onboarding(
                            stand,
                            client,
                            {"X-CSRF-Token": session.csrf},
                            conn,
                            admin_id,
                            billing,
                            access,
                        )
                finally:
                    await store.revoke_all(str(admin_id))
        finally:
            async with stand.pool.acquire() as conn:
                await conn.execute("delete from admin_users where email like $1", ADMIN_EMAIL)


async def db_now(conn: asyncpg.Connection) -> dt.datetime:
    moment: dt.datetime = await conn.fetchval("select clock_timestamp()")
    return moment


async def test_node_and_enroll_contracts_in_openapi(app_client: httpx.AsyncClient) -> None:
    """Маршруты в OpenAPI, ответ enroll несёт сертификат, CA и токен identity; 401 enroll
    называет три кода отказа по токену. Наборы полей всех схем сверяются равенством: выпавшее
    поле контракта не проходит молча."""
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
    refusals = enroll["responses"]["401"]["description"]
    for code in ("token_invalid", "token_used", "token_expired"):
        assert code in refusals, (code, "вторая сторона обмена различает отказы по коду")

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
        "bootstrap_token_annulled_at",
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
        "cert_serial",
        "issued_at",
        "expires_at",
        "revoked_at",
        "enrolled_from",
    }, "node_identities §4.2.3 и адрес источника (миграция 130)"
    assert _fields(schema, "BootstrapToken") == {"node_id", "token", "expires_at", "ca_pem"}
    approve = paths["/api/v1/admin/nodes/{node_id}/approve"]["post"]
    approval = _ref(schema, approve["requestBody"]["content"]["application/json"]["schema"])
    assert set(approval["properties"]) == {"cert_fingerprint"}, "подтверждается сверенная identity"
    assert (
        approval["required"] == ["cert_fingerprint"] and approval["additionalProperties"] is False
    )
    for path, method in (
        ("/api/v1/admin/nodes/{node_id}", "patch"),
        ("/api/v1/admin/nodes/{node_id}", "delete"),
        ("/api/v1/admin/nodes/{node_id}/status", "post"),
    ):
        assert "501" in paths[path][method]["responses"], (path, method, "не реализовано — 501")


async def test_uc01_onboarding_and_uc12_revocation_on_live_data(
    pg_dsn: str, redis_url: str
) -> None:
    """UC-01 шаги 1–7 и UC-12 шаг 1 через панель и раздел агента, со сверкой строк базы."""
    async with onboarding(pg_dsn, redis_url) as ob:
        # Шаг 1: запись ноды — pending, первый интервал тарифицируемой группы и адреса открыт с
        # момента создания, группы доступа записаны.
        node = await ob.create_node()
        node_id = node["id"]
        assert node["status"] == "pending" and node["agent_version"] is None
        assert node["access_group_ids"] == [str(ob.access_group_id)]
        assert node["legal_profile"] == {"jurisdiction": "JP"}
        created_at = dt.datetime.fromisoformat(node["created_at"])
        intervals = await ob.conn.fetch(
            "select billing_group_id, valid_from, valid_to from node_billing_assignments "
            "where node_id = $1",
            uuid.UUID(node_id),
        )
        assert [tuple(row) for row in intervals] == [(ob.billing_group_id, created_at, None)]
        addresses = await ob.conn.fetch(
            "select host(public_ipv4), valid_from, valid_to from node_ip_history "
            "where node_id = $1",
            uuid.UUID(node_id),
        )
        assert [tuple(row) for row in addresses] == [("203.0.113.10", created_at, None)]
        assert (await ob.client.get(f"/api/v1/admin/nodes/{node_id}")).json() == node

        # Шаги 2–3: токен на 60 минут от момента выдачи (Н-24); в базе — только хеш.
        before = await db_now(ob.conn)
        first = await ob.issue(node_id)
        after = await db_now(ob.conn)
        token = first["token"]
        assert first["node_id"] == node_id and len(token) == 43
        # Якорь доверия bootstrap-команды — сертификат CA процесса (security.md §7.1).
        assert first["ca_pem"] == process_ca_pem()
        expires_at = dt.datetime.fromisoformat(first["expires_at"])
        assert before <= expires_at - dt.timedelta(minutes=60) <= after, "срок — от выдачи"
        row = await ob.conn.fetchrow(
            "select token_hash, expires_at, used_at, annulled_at, created_by "
            "from bootstrap_tokens where node_id = $1",
            uuid.UUID(node_id),
        )
        assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
        assert (row["expires_at"], row["used_at"], row["annulled_at"], row["created_by"]) == (
            expires_at,
            None,
            None,
            ob.admin_id,
        )

        # A1: повторная выдача аннулирует прежний токен тем же моментом, в котором выдан новый, —
        # отметкой, а не укороченным сроком: срок жизни прежнего не тронут.
        second = await ob.issue(node_id)
        assert second["token"] != token
        old = await ob.conn.fetchrow(
            "select expires_at, annulled_at from bootstrap_tokens where token_hash = $1",
            hashlib.sha256(token.encode()).hexdigest(),
        )
        issued_second = dt.datetime.fromisoformat(second["expires_at"]) - dt.timedelta(minutes=60)
        assert (old["annulled_at"], old["expires_at"]) == (issued_second, expires_at)
        assert (await ob.state(node_id))["bootstrap_token_annulled_at"] is None, "последний — жив"
        stale = await ob.enroll(token)
        assert stale.status_code == 401 and stale.json()["error"]["code"] == "token_expired"

        # Шаг 5: обмен с другого адреса, без сессии — лист внутреннего CA, токен identity.
        csr = make_csr()
        enrolled_response = await ob.enroll(second["token"], csr)
        assert enrolled_response.status_code == 200, enrolled_response.text
        assert enrolled_response.headers["cache-control"] == "no-store", "токен identity — один раз"
        body = enrolled_response.json()
        assert set(body) == {"client_cert_pem", "ca_pem", "identity_token", "node_id"}
        assert body["node_id"] == node_id and len(body["identity_token"]) == 43
        ca_pem = process_ca_pem()
        assert body["ca_pem"] == ca_pem, "CA процесса — из CA_CERT_FILE"
        assert body["ca_pem"] == second["ca_pem"], "CA ответа — тот, что пришёл в команде"
        leaf = x509.load_pem_x509_certificate(body["client_cert_pem"].encode())
        leaf.verify_directly_issued_by(x509.load_pem_x509_certificate(ca_pem.encode()))
        public = x509.load_pem_x509_csr(csr.encode()).public_key()
        assert isinstance(public, ec.EllipticCurvePublicKey)
        assert leaf.public_key() == public, "лист — под ключ из CSR"
        assert leaf.subject.rfc4514_string() == f"CN={node_id}"
        me = Enrolled(node_id, body["client_cert_pem"], body["identity_token"], body)

        # Строка identity: отпечаток и сроки — от выданного листа, токен — хешем, адрес источника.
        identity = await ob.conn.fetchrow(
            "select cert_fingerprint, cert_serial, token_hash, generation, issued_at, expires_at, "
            "revoked_at, host(enrolled_from) as source from node_identities where node_id = $1",
            uuid.UUID(node_id),
        )
        assert identity["cert_fingerprint"] == me.fingerprint
        # Серийный номер — в форме $ssl_client_serial nginx (формат закреплён литералами openssl в
        # tests/unit/security/test_ca.py): по нему карта отказа 001.66 найдёт этот лист.
        assert identity["cert_serial"] == serial_hex(leaf.serial_number)
        assert identity["token_hash"] == hashlib.sha256(me.identity_token.encode()).hexdigest()
        assert (identity["generation"], identity["revoked_at"], identity["source"]) == (
            1,
            None,
            AGENT_IP,
        )
        assert (identity["issued_at"], identity["expires_at"]) == (
            leaf.not_valid_before_utc,
            leaf.not_valid_after_utc,
        )
        assert identity["expires_at"] - identity["issued_at"] == dt.timedelta(days=90)

        # Шаг 6: администратор сверяет адрес, отпечаток и версии по состоянию.
        presented = await ob.state(node_id)
        assert presented["status"] == "pending"
        assert presented["identity"]["cert_fingerprint"] == me.fingerprint
        assert presented["identity"]["cert_serial"] == identity["cert_serial"]
        assert presented["identity"]["enrolled_from"] == AGENT_IP
        assert (presented["agent_version"], presented["xray_version"]) == ("0.1.0", "26.9.1")
        assert presented["bootstrap_token_used_at"] is not None, "токен погашен обменом"

        # AC: в pending раздел отказывает 403 — identity опознана, но нода не подтверждена.
        pending = await ob.as_agent(me)
        assert pending.status_code == 403, pending.text
        assert pending.json()["error"]["code"] == "node_not_approved"

        # TC-E2E-01 (AC-13): токен одноразовый.
        reused = await ob.enroll(second["token"])
        assert reused.status_code == 401 and reused.json()["error"]["code"] == "token_used"

        # Шаг 7: подтверждение сверенной identity — provisioning; раздел отвечает, нода — из
        # identity.
        approved = await ob.approve(node_id, me.fingerprint)
        assert approved.status_code == 200 and approved.json()["status"] == "provisioning"
        assert (await ob.as_agent(me)).status_code == 200
        whoami = await ob.as_agent(me, "whoami")
        assert whoami.status_code == 403 and whoami.json()["error"]["code"] == "node_mismatch"
        assert whoami.json()["error"]["details"]["node_id"] == node_id, "node_id — из identity"

        # TC-E2E-03 (AC-14, Н-31): отзыв — синхронно: следующий же запрос ноды — 401, нода — в
        # disabled, identity отозвана моментом отзыва.
        revoked = await ob.revoke(node_id)
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["status"] == "disabled"
        revoked_at = revoked.json()["identity"]["revoked_at"]
        assert revoked_at is not None
        assert revoked.json()["identity"]["cert_fingerprint"] == me.fingerprint
        refused = await ob.as_agent(me)
        assert refused.status_code == 401 and refused.json()["error"]["code"] == "unauthenticated"
        again = await ob.revoke(node_id)
        assert again.json()["identity"]["revoked_at"] == revoked_at, "повтор ничего не сдвигает"
        assert again.json()["status_changed_at"] == revoked.json()["status_changed_at"]

        # UC-01 A2 / UC-12 шаг 6: запись остаётся, новый токен — новое поколение identity.
        reborn = await ob.enrolled(node_id)
        assert reborn.fingerprint != me.fingerprint
        generations = await ob.conn.fetch(
            "select generation, revoked_at is not null as revoked from node_identities "
            "where node_id = $1 order by generation",
            uuid.UUID(node_id),
        )
        assert [tuple(row) for row in generations] == [(1, True), (2, False)]
        assert (await ob.state(node_id))["status"] == "pending"
        assert (await ob.as_agent(me)).status_code == 401, "прежняя identity не ожила"
        assert (await ob.as_agent(reborn)).status_code == 403, "новая — опознана, не подтверждена"


async def test_tc_e2e_02_an_expired_token_is_refused_and_stays_unused(
    pg_dsn: str, redis_url: str
) -> None:
    """TC-E2E-02: токен с ``expires_at`` в прошлом — 401 ``token_expired``; отказ его не гасит и
    identity не выпускает. Срок проверяет база своими часами, в момент обмена."""
    async with onboarding(pg_dsn, redis_url) as ob:
        node_id = (await ob.create_node())["id"]
        token = (await ob.issue(node_id))["token"]
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        await ob.conn.execute(
            "update bootstrap_tokens set expires_at = now() - interval '1 second' "
            "where token_hash = $1",
            token_hash,
        )
        response = await ob.enroll(token)
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "token_expired"
        used_at = await ob.conn.fetchval(
            "select used_at from bootstrap_tokens where token_hash = $1", token_hash
        )
        assert used_at is None
        assert (await ob.state(node_id))["identity"] is None


async def test_an_unknown_token_is_refused_without_a_trace(pg_dsn: str, redis_url: str) -> None:
    """Токен, которого не выдавали, — 401 ``token_invalid``, и в базе от отказа ничего не
    остаётся: ни identity, ни погашенного токена. Подобрать 256 бит по коду отказа нельзя, поэтому
    коды различаются: оператору они говорят, что делать (UC-01 A1)."""
    traces = (
        "select (select count(*) from node_identities), "
        "(select count(*) from bootstrap_tokens where used_at is not null)"
    )
    async with onboarding(pg_dsn, redis_url) as ob:
        before = tuple(await ob.conn.fetchrow(traces))
        response = await ob.enroll("y" * 43)
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "token_invalid"
        assert tuple(await ob.conn.fetchrow(traces)) == before, "отказ не оставил следа"


async def wait_blocked_by(conn: asyncpg.Connection, holder_pid: int, count: int) -> None:
    """Дождаться, пока ``count`` подключений встанут в ожидание за подключением ``holder_pid`` —
    прямо или через очередь той же строки (второй ждущий строку ждёт блокировку кортежа первого):
    порядок гонки задаёт база, а не пауза цикла событий. Пауза с проверкой «задача ещё не
    завершилась» не доказывала, что встречная операция уже взяла свою блокировку: под задержкой
    стенда отзыв или выдача не успевали взять строку ноды, и обмен законно шёл первым — 200
    вместо 401 (посадки раунда 9: S130, после роаста — S213)."""
    blocked = 0
    for _ in range(400):
        blocked = await conn.fetchval(
            "with recursive waiting(pid) as ("
            " select pid from pg_stat_activity where $1 = any(pg_blocking_pids(pid))"
            " union"
            " select a.pid from pg_stat_activity a join waiting w"
            " on w.pid = any(pg_blocking_pids(a.pid))"
            ") select count(*) from waiting",
            holder_pid,
        )
        if blocked >= count:
            return
        await asyncio.sleep(0.025)
    raise AssertionError(f"ждали {count} подключений за {holder_pid}, встало {blocked}")


async def test_one_token_exchanged_twice_at_once_gives_one_identity(
    pg_dsn: str, redis_url: str
) -> None:
    """Два одновременных обмена одним токеном: ровно один лист и одна строка identity, второй
    ответ — ``token_used``. Одновременность не отдана расписанию цикла событий: второе подключение
    держит строку ноды, пока стартуют оба обмена. При верном порядке оба ждут её, и второй,
    получив, видит погашение первого. Одноразовость здесь держат два механизма — блокировка ноды
    и условное погашение (``used_at is null``, раунд 2): без блокировки второй обмен всё равно
    получил бы ``token_used`` на погашении (его страж —
    ``test_a_token_consumed_behind_the_exchanges_back_is_not_consumed_twice``)."""
    async with onboarding(pg_dsn, redis_url) as ob:
        node_id = uuid.UUID((await ob.create_node())["id"])
        token = (await ob.issue(str(node_id)))["token"]
        holder = await asyncpg.connect(pg_dsn)
        try:
            transaction = holder.transaction()
            await transaction.start()
            await holder.execute("select 1 from nodes where id = $1 for no key update", node_id)
            exchanges = [asyncio.create_task(ob.enroll(token)) for _ in range(2)]
            await wait_blocked_by(ob.conn, await holder.fetchval("select pg_backend_pid()"), 2)
            assert not any(exchange.done() for exchange in exchanges), "оба ждут строку ноды"
            await transaction.commit()
            results = await asyncio.wait_for(asyncio.gather(*exchanges), timeout=10)
        finally:
            await holder.close()
        assert sorted(r.status_code for r in results) == [200, 401], [r.text for r in results]
        loser = next(r for r in results if r.status_code == 401)
        assert loser.json()["error"]["code"] == "token_used"
        count = await ob.conn.fetchval(
            "select count(*) from node_identities where node_id = $1", node_id
        )
        assert count == 1


@pytest.mark.parametrize(
    ("mark", "code"),
    [("used_at", "token_used"), ("annulled_at", "token_expired")],
)
async def test_a_token_consumed_behind_the_exchanges_back_is_not_consumed_twice(
    pg_dsn: str, redis_url: str, mark: str, code: str
) -> None:
    """Одноразовость держит и сама запись погашения, а не только блокировка ноды (роаст 001.25,
    раунды 2–3): писатель, обошедший блокировку ноды (будущий путь или ошибка), гасит или
    аннулирует токен, пока обмен уже прочёл его годным и ждёт своей записи. Сыграно на живой базе:
    вспомогательная транзакция держит строку токена, обмен проходит проверки и встаёт на своём
    условном погашении; вспомогательная ставит отметку и фиксируется — обмен после ожидания видит
    ``UPDATE 0`` и отвечает кодом по тому, что сделал встречный: ``token_used`` или
    ``token_expired`` (раунд 3: без условия ``annulled_at is null`` погашение аннулированного
    упиралось в CHECK миграции 131 — 500), identity не появляется. Без условия ``used_at is null``
    обмен перезаписал бы погашение и выпустил вторую identity по одному токену."""
    async with onboarding(pg_dsn, redis_url) as ob:
        node_id = uuid.UUID((await ob.create_node())["id"])
        token = (await ob.issue(str(node_id)))["token"]
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        other = await asyncpg.connect(pg_dsn)
        try:
            transaction = other.transaction()
            await transaction.start()
            await other.execute(
                "select 1 from bootstrap_tokens where token_hash = $1 for update", token_hash
            )
            exchange = asyncio.create_task(ob.enroll(token))
            await wait_blocked_by(ob.conn, await other.fetchval("select pg_backend_pid()"), 1)
            assert not exchange.done(), "обмен прошёл проверки и ждёт строку токена"
            await other.execute(
                f"update bootstrap_tokens set {mark} = clock_timestamp() where token_hash = $1",  # noqa: S608 — имя колонки из параметризации
                token_hash,
            )
            await transaction.commit()
            response = await asyncio.wait_for(exchange, timeout=10)
        finally:
            await other.close()
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == code
        identities = await ob.conn.fetchval(
            "select count(*) from node_identities where node_id = $1", node_id
        )
        assert identities == 0, "по одному токену — ни одной лишней identity"


async def test_the_state_shows_the_last_issued_token_even_after_a_clock_step(
    pg_dsn: str, redis_url: str
) -> None:
    """Состояние на шаге 6 показывает последний выданный токен — по номеру выдачи
    (``issue_seq``, последовательность базы), а не по идентификатору: uuidv7 растёт с часами
    базы, и после их шага назад прежний токен оказался бы «последним» — и пока новый жив (роаст
    001.25, раунд 3), и после его обмена, когда живого токена нет (раунд 4). Шаг сыгран прежним
    токеном с идентификатором «из будущего», выданным раньше нового: новая выдача аннулирует его
    отметкой, как настоящий."""
    async with onboarding(pg_dsn, redis_url) as ob:
        node_id = (await ob.create_node())["id"]
        # Прежний токен выдан «при забежавших вперёд часах»: и идентификатор, и срок у него —
        # позже, чем у нового, так что ни порядок по uuidv7, ни порядок по сроку не совпадут с
        # порядком выдачи (роаст 001.25, раунд 5: при сроке раньше нового тест пропускал порядок
        # по expires_at).
        await ob.conn.execute(
            "insert into bootstrap_tokens (id, node_id, token_hash, expires_at, created_by) "
            "values ($1, $2, $3, now() + interval '2 hours', $4)",
            uuid.UUID("ffffffff-ffff-7fff-bfff-ffffffffffff"),
            uuid.UUID(node_id),
            hashlib.sha256(b"issued before the clock step").hexdigest(),
            ob.admin_id,
        )
        issued = await ob.issue(node_id)
        state = await ob.state(node_id)
        assert state["bootstrap_token_annulled_at"] is None, "показан живой токен, а не прежний"
        assert state["bootstrap_token_expires_at"] == issued["expires_at"]
        assert (await ob.enroll(issued["token"])).status_code == 200
        state = await ob.state(node_id)
        assert state["bootstrap_token_used_at"] is not None, "показан обменянный токен"
        assert state["bootstrap_token_annulled_at"] is None, "а не прежний, аннулированный"


@pytest.mark.parametrize("subject", ["token", "identity"])
async def test_expiry_is_judged_after_the_node_lock(
    pg_dsn: str, redis_url: str, subject: str
) -> None:
    """Срок сверяется с моментом операции после блокировки ноды, а не с началом транзакции:
    операция, начатая до истечения и дождавшаяся ноды после, видит истёкшее истёкшим (роаст
    001.25, раунд 4 — структурный страж порядка не видел предиката срока). Вторая транзакция
    держит строку ноды; срок токена (или identity) истекает через секунду; обмен (или
    подтверждение) начинает транзакцию и встаёт на блокировке; через две секунды вторая
    фиксируется — обмен получает ``token_expired``, подтверждение — 409 без действующей
    identity, и ни identity, ни ``provisioning`` не появляются."""
    async with onboarding(pg_dsn, redis_url) as ob:
        node_id = (await ob.create_node())["id"]
        if subject == "token":
            token = (await ob.issue(node_id))["token"]
            await ob.conn.execute(
                "update bootstrap_tokens set expires_at = clock_timestamp() + interval '1 second' "
                "where token_hash = $1",
                hashlib.sha256(token.encode()).hexdigest(),
            )
        else:
            enrolled = await ob.enrolled(node_id)
            await ob.conn.execute(
                "update node_identities set expires_at = clock_timestamp() + interval '1 second' "
                "where node_id = $1 and revoked_at is null",
                uuid.UUID(node_id),
            )
        holder = await asyncpg.connect(pg_dsn)
        try:
            transaction = holder.transaction()
            await transaction.start()
            await holder.execute("select 1 from nodes where id = $1 for update", uuid.UUID(node_id))
            operation = asyncio.create_task(
                ob.enroll(token)
                if subject == "token"
                else ob.approve(node_id, enrolled.fingerprint)
            )
            await wait_blocked_by(ob.conn, await holder.fetchval("select pg_backend_pid()"), 1)
            assert not operation.done(), "операция ждёт строку ноды"
            await asyncio.sleep(2.0)  # срок истекает, пока операция ждёт
            await transaction.commit()
            response = await asyncio.wait_for(operation, timeout=10)
        finally:
            await holder.close()
        if subject == "token":
            assert response.status_code == 401, response.text
            assert response.json()["error"]["code"] == "token_expired"
            count = await ob.conn.fetchval(
                "select count(*) from node_identities where node_id = $1", uuid.UUID(node_id)
            )
            assert count == 0, "identity по истёкшему токену не выпущена"
        else:
            assert response.status_code == 409, response.text
            assert response.json()["error"] == {
                "code": "conflict",
                "message": "у ноды нет действующей identity: токен не обменян, "
                "identity истекла или отозвана",
                "details": {},
            }
            assert (await ob.state(node_id))["status"] == "pending"


async def test_a_token_reissued_during_an_exchange_waits_instead_of_deadlocking(
    pg_dsn: str, redis_url: str
) -> None:
    """Порядок блокировок: выдача и отзыв берут строку ноды, затем её токены; обмен — так же.
    Встречную выдачу играет второе подключение: держит строку ноды и аннулирует токен, как
    ``issue_bootstrap_token`` (отметка ``annulled_at``). Обмен ждёт ноду и видит аннулирование —
    401 ``token_expired``. Обмен, взявший строку токена раньше ноды, держал бы её, пока ждёт ноду,
    а выдача ждала бы токен — взаимная блокировка, и база прервала бы одну из транзакций."""
    async with onboarding(pg_dsn, redis_url) as ob:
        node_id = uuid.UUID((await ob.create_node())["id"])
        token = (await ob.issue(str(node_id)))["token"]
        issuer = await asyncpg.connect(pg_dsn)
        try:
            transaction = issuer.transaction()
            await transaction.start()
            await issuer.execute("select 1 from nodes where id = $1 for no key update", node_id)
            exchange = asyncio.create_task(ob.enroll(token))
            await wait_blocked_by(ob.conn, await issuer.fetchval("select pg_backend_pid()"), 1)
            assert not exchange.done(), "обмен ждёт строку ноды"
            await issuer.execute(
                "update bootstrap_tokens set annulled_at = clock_timestamp() "
                "where node_id = $1 and used_at is null and annulled_at is null",
                node_id,
            )
            await transaction.commit()
            response = await asyncio.wait_for(exchange, timeout=10)
        finally:
            await issuer.close()
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "token_expired"


@pytest.mark.parametrize("annulment", ["revoke", "reissue"])
async def test_an_annulment_that_began_after_the_exchange_still_holds(
    pg_dsn: str, redis_url: str, annulment: str
) -> None:
    """Настоящие отзыв и выдача против обмена, **начавшегося раньше** них (роаст 001.25, раунд 1):
    обмен открыл транзакцию и ждёт таблицу токенов (её держит третье подключение), отзыв
    (повторная выдача) открывается позже, берёт строку ноды первым и ждёт ту же таблицу. После
    освобождения аннулирование фиксируется раньше, чем обмен получает ноду, — и обмен обязан его
    увидеть: 401 ``token_expired``, ни одной действующей identity, а у отзыва нода остаётся
    ``disabled``. Сравнение срока с ``now()`` транзакции обмена — моментом её начала, более ранним,
    чем аннулирование, — выпустило бы identity после отзыва и вернуло бы ноду в ``pending``."""
    async with onboarding(pg_dsn, redis_url) as ob:
        node_id = (await ob.create_node())["id"]
        token = (await ob.issue(node_id))["token"]
        gate = await asyncpg.connect(pg_dsn)
        try:
            transaction = gate.transaction()
            await transaction.start()
            await gate.execute("lock table bootstrap_tokens in access exclusive mode")
            gate_pid = await gate.fetchval("select pg_backend_pid()")
            exchange = asyncio.create_task(ob.enroll(token))
            await wait_blocked_by(ob.conn, gate_pid, 1)
            assert not exchange.done(), "обмен начал транзакцию и ждёт таблицу токенов"
            annul = asyncio.create_task(
                ob.revoke(node_id)
                if annulment == "revoke"
                else ob.client.post(
                    f"/api/v1/admin/nodes/{node_id}/bootstrap-token", headers=ob.headers
                )
            )
            await wait_blocked_by(ob.conn, gate_pid, 2)  # взял строку ноды и ждёт таблицу
            assert not annul.done(), "аннулирующий взял ноду и ждёт таблицу токенов"
            await transaction.commit()
            annulled = await asyncio.wait_for(annul, timeout=10)
            response = await asyncio.wait_for(exchange, timeout=10)
        finally:
            await gate.close()
        assert annulled.status_code in (200, 201), annulled.text
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "token_expired"
        live = await ob.conn.fetchval(
            "select count(*) from node_identities where node_id = $1 and revoked_at is null",
            uuid.UUID(node_id),
        )
        assert live == 0, "identity после аннулирования не выпущена"
        expected = "disabled" if annulment == "revoke" else "pending"
        assert (await ob.state(node_id))["status"] == expected


async def test_an_annulled_token_stays_refused_when_the_clock_steps_back(
    pg_dsn: str, redis_url: str
) -> None:
    """Аннулирование — отметка, а не укороченный срок (роаст 001.25, раунд 2): часы базы могут
    шагнуть назад (NTP, возобновление VM), и обмен, чей момент окажется раньше момента отзыва, при
    сравнении срока с часами счёл бы токен годным — identity после отзыва. Шаг назад здесь сыгран
    тем, что отметка поставлена «в будущем» относительно часов обмена, а срок жизни продлён на
    сутки: обмен обязан отказать по отметке."""
    async with onboarding(pg_dsn, redis_url) as ob:
        node_id = (await ob.create_node())["id"]
        token = (await ob.issue(node_id))["token"]
        assert (await ob.revoke(node_id)).status_code == 200
        await ob.conn.execute(
            "update bootstrap_tokens set annulled_at = clock_timestamp() + interval '5 seconds', "
            "expires_at = expires_at + interval '1 day' where token_hash = $1",
            hashlib.sha256(token.encode()).hexdigest(),
        )
        response = await ob.enroll(token)
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "token_expired"
        assert (await ob.state(node_id))["status"] == "disabled", "отзыв не откатился"


async def test_the_token_is_checked_before_the_ca_sees_the_csr(pg_dsn: str, redis_url: str) -> None:
    """Проверки токена идут раньше работы CA: неизвестный, использованный, истёкший и
    аннулированный токен с CSR, который CA отверг бы (RSA), — 401 с кодом токена, а не 422
    ``invalid_csr``. Иначе держатель негодного токена заставлял бы CA разбирать его CSR, а ответ
    выдавал бы, что до CA дошло. Аннулированный — отдельно (роаст 001.25, раунд 4): условное
    погашение отказывает ему и само, поэтому пропуск отметки в предварительной проверке виден
    только здесь — по порядку относительно CA."""
    async with onboarding(pg_dsn, redis_url) as ob:
        node_id = (await ob.create_node())["id"]
        rsa = make_csr(rsa_bits=2048)
        unknown = await ob.enroll("y" * 43, rsa)
        assert unknown.status_code == 401 and unknown.json()["error"]["code"] == "token_invalid"
        token = (await ob.issue(node_id))["token"]
        assert (await ob.enroll(token)).status_code == 200
        used = await ob.enroll(token, rsa)
        assert used.status_code == 401 and used.json()["error"]["code"] == "token_used"
        stale = (await ob.issue(node_id))["token"]
        await ob.conn.execute(
            "update bootstrap_tokens set expires_at = now() - interval '1 second' "
            "where token_hash = $1",
            hashlib.sha256(stale.encode()).hexdigest(),
        )
        expired = await ob.enroll(stale, rsa)
        assert expired.status_code == 401 and expired.json()["error"]["code"] == "token_expired"
        annulled = (await ob.issue(node_id))["token"]
        await ob.issue(node_id)  # новая выдача аннулирует прежний токен отметкой
        refused = await ob.enroll(annulled, rsa)
        assert refused.status_code == 401 and refused.json()["error"]["code"] == "token_expired"


async def test_a_csr_the_ca_rejects_does_not_burn_the_token(pg_dsn: str, redis_url: str) -> None:
    """CSR с ключом не P-256 отвергает CA — 422 ``invalid_csr``; погашение откатывается вместе
    с транзакцией обмена, и тот же токен с годным CSR проходит. Иначе агент с ошибкой генерации
    ключа сжигал бы токен и шёл к администратору за новым."""
    async with onboarding(pg_dsn, redis_url) as ob:
        node_id = (await ob.create_node())["id"]
        token = (await ob.issue(node_id))["token"]
        rejected = await ob.enroll(token, make_csr(rsa_bits=2048))
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["error"]["code"] == "invalid_csr"
        assert (await ob.enroll(token, make_csr(ec.SECP384R1()))).status_code == 422
        assert (await ob.state(node_id))["bootstrap_token_used_at"] is None
        assert (await ob.enroll(token)).status_code == 200


async def test_revocation_annuls_a_token_not_yet_exchanged(pg_dsn: str, redis_url: str) -> None:
    """Отзыв identity аннулирует и невыменянный токен (UC-12): токен, утёкший вместе с VPS,
    иначе выпустил бы identity уже после отзыва. Аннулирование — отметка моментом отзыва; срок
    жизни токена не тронут, и состояние показывает, что токен аннулирован, а не истёк."""
    async with onboarding(pg_dsn, redis_url) as ob:
        node_id = (await ob.create_node())["id"]
        issued = await ob.issue(node_id)
        revoked = await ob.revoke(node_id)
        assert revoked.status_code == 200
        state = revoked.json()
        assert state["bootstrap_token_annulled_at"] == state["status_changed_at"], "момент отзыва"
        assert state["bootstrap_token_expires_at"] == issued["expires_at"], "срок не тронут"
        response = await ob.enroll(issued["token"])
        assert response.status_code == 401 and response.json()["error"]["code"] == "token_expired"


async def test_a_new_exchange_retires_the_identity_it_replaces(pg_dsn: str, redis_url: str) -> None:
    """§4.5 «Пересоздание»: новый токен и новый обмен при живой identity отзывают прежнюю тем же
    моментом, в котором выдана новая, и возвращают ноду в pending — старый VPS больше не
    представится этой нодой. (Ротация с окном перекрытия Н-28 — отдельный путь 001.31.)"""
    async with onboarding(pg_dsn, redis_url) as ob:
        node_id = (await ob.create_node())["id"]
        old = await ob.enrolled(node_id)
        assert (await ob.approve(node_id, old.fingerprint)).status_code == 200
        new = await ob.enrolled(node_id)
        rows = await ob.conn.fetch(
            "select generation, issued_at, revoked_at from node_identities where node_id = $1 "
            "order by generation",
            uuid.UUID(node_id),
        )
        exchanged_at = await ob.conn.fetchval(
            "select used_at from bootstrap_tokens where node_id = $1 order by id desc limit 1",
            uuid.UUID(node_id),
        )
        assert [row["generation"] for row in rows] == [1, 2]
        assert rows[1]["revoked_at"] is None
        assert rows[0]["revoked_at"] == exchanged_at, "прежняя отозвана моментом нового обмена"
        assert rows[1]["issued_at"] == exchanged_at.replace(microsecond=0), (
            "новая выпущена тем же моментом (сертификат хранит секунды)"
        )
        assert (await ob.as_agent(old)).status_code == 401
        assert (await ob.as_agent(new)).status_code == 403, "новая identity ждёт подтверждения"
        assert (await ob.state(node_id))["status"] == "pending"


def _refusal(response: httpx.Response) -> tuple[int, str, str]:
    """Статус, код и смысловая часть сообщения отказа (до двоеточия)."""
    error = response.json()["error"]
    return response.status_code, error["code"], error["message"].split(":")[0]


async def test_approval_needs_pending_and_a_live_identity(pg_dsn: str, redis_url: str) -> None:
    """Шаг 7 подтверждает то, что сверено на шаге 6: без действующей identity сверять нечего —
    409; из любого статуса, кроме pending, — 409; выведенная нода — 409; неизвестная — 404; тело
    без отпечатка или с негодным — 422 модели."""
    async with onboarding(pg_dsn, redis_url) as ob:
        node_id = (await ob.create_node())["id"]
        # Код и текст — равенством: отказ «нечего подтверждать» отличается от
        # ``identity_changed`` (подтверждение не той identity), и оператору они говорят разное.
        no_identity = (409, "conflict", "у ноды нет действующей identity")
        bare = await ob.approve(node_id)
        assert _refusal(bare) == no_identity, bare.text
        expired = await ob.enrolled(node_id)
        await ob.conn.execute(
            "update node_identities set expires_at = now() - interval '1 second' "
            "where node_id = $1",
            uuid.UUID(node_id),
        )
        stale = await ob.approve(node_id, expired.fingerprint)
        assert _refusal(stale) == no_identity, ("истёкшая identity — не действующая", stale.text)
        live = await ob.enrolled(node_id)
        for body in (
            {},
            {"cert_fingerprint": "AB" * 32},
            {"cert_fingerprint": live.fingerprint[:63]},
        ):
            response = await ob.client.post(
                f"/api/v1/admin/nodes/{node_id}/approve", json=body, headers=ob.headers
            )
            assert response.status_code == 422, (body, response.text)
        assert (await ob.approve(node_id, live.fingerprint)).status_code == 200
        twice = await ob.approve(node_id, live.fingerprint)
        assert twice.status_code == 409 and "provisioning" in twice.json()["error"]["message"]
        assert (await ob.approve(OTHER_NODE_ID)).status_code == 404
        gone_id = (await ob.create_node())["id"]
        gone = await ob.enrolled(gone_id)
        await ob.conn.execute(
            "update nodes set decommissioned_at = now() where id = $1", uuid.UUID(gone_id)
        )
        decommissioned = await ob.approve(gone_id, gone.fingerprint)
        assert decommissioned.status_code == 409 and "выведена" in decommissioned.text


async def test_approval_confirms_the_identity_that_was_checked(pg_dsn: str, redis_url: str) -> None:
    """Подтверждается та identity, что сверена на шаге 6, а не та, что действует к моменту
    подтверждения (роаст 001.25, раунд 1): администратор видит поколение 1, другой администратор
    тем временем выдаёт новый токен, и его обменивают — действует поколение 2 с другим адресом.
    Подтверждение с отпечатком поколения 1 — 409 ``identity_changed``, нода остаётся ``pending``;
    с отпечатком поколения 2, сверенного заново, — 200."""
    async with onboarding(pg_dsn, redis_url) as ob:
        node_id = (await ob.create_node())["id"]
        checked = await ob.enrolled(node_id)
        assert (await ob.state(node_id))["identity"]["cert_fingerprint"] == checked.fingerprint
        response = await ob.enroll((await ob.issue(node_id))["token"], ip="192.0.2.66")
        assert response.status_code == 200, response.text
        replaced = response.json()["client_cert_pem"]
        refused = await ob.approve(node_id, checked.fingerprint)
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "identity_changed"
        assert (await ob.state(node_id))["status"] == "pending"
        rechecked = await ob.state(node_id)
        assert rechecked["identity"]["enrolled_from"] == "192.0.2.66"
        assert rechecked["identity"]["cert_fingerprint"] == InternalCA.fingerprint(replaced)
        approved = await ob.approve(node_id, InternalCA.fingerprint(replaced))
        assert approved.status_code == 200 and approved.json()["status"] == "provisioning"


async def test_the_agent_is_known_only_by_its_certificate_and_its_token(
    pg_dsn: str, redis_url: str
) -> None:
    """Identity — пара «сертификат + токен»: чужой токен к своему сертификату, свой токен к
    чужому сертификату, сертификат без строки identity, истёкшая identity и выведенная нода —
    один и тот же 401 (§5.2). Своя пара — нода из identity, а не из тела запроса."""
    async with onboarding(pg_dsn, redis_url) as ob:
        first_id = (await ob.create_node())["id"]
        second_id = (await ob.create_node())["id"]
        first, second = await ob.enrolled(first_id), await ob.enrolled(second_id)
        for node in (first, second):
            assert (await ob.approve(node.node_id, node.fingerprint)).status_code == 200
        assert (await ob.as_agent(first, "whoami")).json()["error"]["details"] == {
            "node_id": first_id
        }
        assert (await ob.as_agent(second, "whoami")).json()["error"]["details"] == {
            "node_id": second_id
        }
        for mixed in (
            {**first.headers, "X-Node-Identity": second.identity_token},
            {**second.headers, "X-Node-Identity": first.identity_token},
            {**first.headers, "X-Node-Identity": "z" * 43},
            {**first.headers, "X-Client-Cert": STUB_CLIENT_CERT},
        ):
            response = await ob.as_agent(mixed)
            assert response.status_code == 401, response.text
            assert response.json()["error"]["code"] == "unauthenticated"
        assert InternalCA.fingerprint(STUB_CLIENT_CERT_PEM) not in {
            first.fingerprint,
            second.fingerprint,
        }
        await ob.conn.execute(
            "update node_identities set expires_at = now() - interval '1 second' "
            "where node_id = $1",
            uuid.UUID(first_id),
        )
        assert (await ob.as_agent(first)).status_code == 401, "истёкшая identity"
        await ob.conn.execute(
            "update nodes set decommissioned_at = now() where id = $1", uuid.UUID(second_id)
        )
        assert (await ob.as_agent(second)).status_code == 401, "выведенная нода"


async def test_tokens_are_issued_only_for_a_node_in_service(pg_dsn: str, redis_url: str) -> None:
    """Нет ноды — 404; нода выведена из эксплуатации — 409 на выдачу, а выданный до вывода токен —
    401 ``token_invalid`` на обмен."""
    async with onboarding(pg_dsn, redis_url) as ob:
        missing = await ob.client.post(
            f"/api/v1/admin/nodes/{OTHER_NODE_ID}/bootstrap-token", headers=ob.headers
        )
        assert missing.status_code == 404, missing.text
        node_id = (await ob.create_node())["id"]
        token = (await ob.issue(node_id))["token"]
        await ob.conn.execute(
            "update nodes set decommissioned_at = now() where id = $1", uuid.UUID(node_id)
        )
        refused = await ob.client.post(
            f"/api/v1/admin/nodes/{node_id}/bootstrap-token", headers=ob.headers
        )
        assert refused.status_code == 409, refused.text
        late = await ob.enroll(token)
        assert late.status_code == 401 and late.json()["error"]["code"] == "token_invalid"


async def test_a_node_record_refers_to_what_exists(pg_dsn: str, redis_url: str) -> None:
    """Код ноды уникален — 409; неизвестная тарифицируемая группа или группа доступа — 422 с
    кодом причины, а не 500 внешнего ключа; неудачная запись не оставляет строк."""
    async with onboarding(pg_dsn, redis_url) as ob:
        code = (await ob.create_node())["code"]
        base = {
            **VALID_NODE,
            "billing_group_id": str(ob.billing_group_id),
            "access_group_ids": [str(ob.access_group_id)],
        }
        cases = (
            ({**base, "code": code}, 409, "conflict"),
            (
                {**base, "code": unique_name("node"), "billing_group_id": OTHER_NODE_ID},
                422,
                "unknown_billing_group",
            ),
            (
                {**base, "code": unique_name("node"), "access_group_ids": [OTHER_NODE_ID]},
                422,
                "unknown_access_group",
            ),
        )
        for body, status, reason in cases:
            response = await ob.client.post("/api/v1/admin/nodes", json=body, headers=ob.headers)
            assert response.status_code == status, (body["code"], response.text)
            assert response.json()["error"]["code"] == reason, response.text
        leftover = await ob.conn.fetchval(
            "select count(*) from nodes where code like 't19-node-%' and code <> $1", code
        )
        assert leftover == 0, "откат записи — целиком"


async def test_real_operations_answer_404_for_an_unknown_node(pg_dsn: str, redis_url: str) -> None:
    """Карточка, состояние, подтверждение, отзыв и выдача токена — настоящие: про ноду, которой
    нет, они отвечают 404, а не фиксированной карточкой."""
    async with admin_stand(pg_dsn, redis_url) as admin:
        client, headers = admin.client, admin.headers
        for path in (
            f"/api/v1/admin/nodes/{OTHER_NODE_ID}",
            f"/api/v1/admin/nodes/{OTHER_NODE_ID}/state",
        ):
            assert (await client.get(path)).status_code == 404, path
        for path, body in (
            (f"/api/v1/admin/nodes/{OTHER_NODE_ID}/approve", {"cert_fingerprint": "0" * 64}),
            (f"/api/v1/admin/nodes/{OTHER_NODE_ID}/revoke-identity", None),
            (f"/api/v1/admin/nodes/{OTHER_NODE_ID}/bootstrap-token", None),
        ):
            response = await client.post(path, json=body, headers=headers)
            assert response.status_code == 404, (path, response.text)
            assert response.json()["error"]["code"] == "not_found"


async def test_the_operations_not_implemented_say_so(pg_dsn: str, redis_url: str) -> None:
    """Изменение записи, ручной статус и вывод из эксплуатации не реализованы и отвечают 501
    ``not_implemented`` — и про настоящую ноду, и про несуществующую (роаст 001.25, раунд 1: при
    настоящих чтении и подтверждении «успех» без записи говорил бы администратору, что нода
    отключена, пока она обслуживает). Тело по-прежнему проверяется моделью раньше: ручной статус —
    только из трёх ручных (``active``, ``pending``, пустое тело, регистр — 422)."""
    async with onboarding(pg_dsn, redis_url) as ob:
        real = (await ob.create_node())["id"]
        for node_id in (real, OTHER_NODE_ID):
            path = f"/api/v1/admin/nodes/{node_id}"
            for response in (
                await ob.client.post(
                    f"{path}/status", json={"status": "disabled"}, headers=ob.headers
                ),
                await ob.client.post(f"{path}/status", json={"status": None}, headers=ob.headers),
                await ob.client.patch(path, json={"name": "Tokyo 1a"}, headers=ob.headers),
                await ob.client.delete(path, headers=ob.headers),
            ):
                assert response.status_code == 501, (node_id, response.request.url, response.text)
                assert response.json()["error"]["code"] == "not_implemented"
            for bad in ({"status": "active"}, {"status": "pending"}, {}, {"status": "Maintenance"}):
                response = await ob.client.post(f"{path}/status", json=bad, headers=ob.headers)
                assert response.status_code == 422, (bad, response.text)
        card = (await ob.client.get(f"/api/v1/admin/nodes/{real}")).json()
        assert (card["status"], card["name"], card["decommissioned_at"]) == (
            "pending",
            "Tokyo 1",
            None,
        ), "ничего не записано"


async def test_node_record_validation(pg_dsn: str, redis_url: str) -> None:
    """Поля §4.2.3: страна ISO alpha-2 в верхнем регистре, адрес IPv4, положительные лимиты,
    код без пробелов, обязательная тарифицируемая группа (R-18); PATCH: null для NOT NULL — 422
    до вызова (сама операция не реализована — 501)."""
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
        assert optional_null.status_code == 501, "null необязательного поля модель пропускает"
        assert (
            await client.patch("/api/v1/admin/nodes/not-a-uuid", json={}, headers=headers)
        ).status_code == 422


async def test_enroll_validates_body_and_needs_no_session(app_client: httpx.AsyncClient) -> None:
    """Enrollment без cookie и CSRF: годное тело доходит до проверки токена (неизвестный —
    401 ``token_invalid``, а не 401/403 сессии); ошибки тела — 422 единого формата до базы; CSR
    обязан быть PEM-блоком CERTIFICATE REQUEST и не длиннее ``CSR_MAX_CHARS`` (тело
    enrollment-сервера nginx ограничено 64 КБ)."""
    ok = await app_client.post("/agent/v1/enroll", json=VALID_ENROLL)
    assert ok.status_code == 401, ok.text
    assert ok.json()["error"]["code"] == "token_invalid"
    cert_as_csr = STUB_CLIENT_CERT_PEM  # правильный PEM, но не запрос на сертификат
    # Валидный base64 длиннее предела, размер — литералом: страж мерит длину, а не испорченную
    # кодировку, и не растёт вместе с проверяемой константой (ревью 001.24, раунд 2).
    assert CSR_MAX_CHARS == 16 * 1024
    long_body = "QUJD" * 4096
    long_csr = (
        f"-----BEGIN CERTIFICATE REQUEST-----\n{long_body}\n-----END CERTIFICATE REQUEST-----\n"
    )
    assert len(long_csr) > CSR_MAX_CHARS
    csr = VALID_ENROLL["csr_pem"]
    begin, first, *rest = csr.splitlines(keepends=True)
    broken = "".join([begin, "!!!" + first[3:], *rest])  # тело не base64
    assert broken != csr
    for bad in (
        {},
        {**VALID_ENROLL, "bootstrap_token": "short"},
        # Алфавит токена — base64url выданного: вход сужается до отказа, а не после него,
        # потому что значение уходит в поиск по хешу.
        {**VALID_ENROLL, "bootstrap_token": "x" * 40 + "!"},
        {**VALID_ENROLL, "bootstrap_token": "x" * 20 + " " + "x" * 20},
        {**VALID_ENROLL, "csr_pem": "not a csr"},
        {**VALID_ENROLL, "csr_pem": cert_as_csr},
        {**VALID_ENROLL, "csr_pem": broken},
        {**VALID_ENROLL, "csr_pem": "prefix " + csr},
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
    # Окончания строк CRLF допускает RFC 7468 §3 — тело проходит проверку и доходит до токена.
    crlf = csr.replace("\n", "\r\n")
    crlf_answer = await app_client.post("/agent/v1/enroll", json={**VALID_ENROLL, "csr_pem": crlf})
    assert crlf_answer.status_code == 401, crlf_answer.text


async def test_only_a_ca_refusal_is_the_nodes_fault() -> None:
    """CSR, прошедший проверку рамки, но отвергнутый самим CA, — 422 ``invalid_csr`` без текста
    исключения наружу. Прочая ``ValueError`` из обмена — ошибка программы, а не вина ноды: 500,
    а не 422 (раньше маршрут перехватывал любую ``ValueError``, в том числе ``ValidationError``
    pydantic при сборке ответа)."""

    def failing(exc: Exception) -> type:
        class Failing:
            async def enroll(self, *args: object, **kwargs: object) -> object:
                raise exc

        return Failing

    app = create_app()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://control-plane") as client:
        app.dependency_overrides[get_node_service] = failing(
            CsrRejectedError("внутренняя подробность о ключе CA")
        )
        response = await client.post("/agent/v1/enroll", json=VALID_ENROLL)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "invalid_csr"
        assert "ключе CA" not in response.text, "текст исключения наружу не выносится"
        app.dependency_overrides[get_node_service] = failing(ValueError("сбой сборки ответа"))
        response = await client.post("/agent/v1/enroll", json=VALID_ENROLL)
        assert response.status_code == 500, response.text


async def test_state_and_card_agree_on_the_same_node(pg_dsn: str, redis_url: str) -> None:
    """Карточка и состояние одной ноды не противоречат друг другу: статус, версии и
    ``resync_required`` совпадают — и до обмена токена, и после подтверждения."""
    async with onboarding(pg_dsn, redis_url) as ob:
        node_id = (await ob.create_node())["id"]
        fingerprint = ""
        for step in ("создана", "обменяла токен", "подтверждена"):
            if step == "обменяла токен":
                fingerprint = (await ob.enrolled(node_id)).fingerprint
            if step == "подтверждена":
                assert (await ob.approve(node_id, fingerprint)).status_code == 200
            card = (await ob.client.get(f"/api/v1/admin/nodes/{node_id}")).json()
            state = await ob.state(node_id)
            assert card["id"] == state["node_id"] == node_id, step
            assert (card["status"], card["status_changed_at"]) == (
                state["status"],
                state["status_changed_at"],
            ), step
            assert card["resync_required"] == state["resync_required"], step
            assert (card["agent_version"], card["xray_version"]) == (
                state["agent_version"],
                state["xray_version"],
            ), step
