"""Сквозные проверки ``/api/v1/me`` на заглушках (задача 001.15; UC-16, UC-14) на живом стенде:
восемь операций в OpenAPI со схемами, все под сессией пользователя (401 без неё), мутации под
CSRF, подтверждение ``confirm=true`` для перевыпуска и удаления, состав ``TrafficStats`` §4.12
с фиксированными числами, ссылки подписки на двух доменах, мастер первого подключения §17.6.

TC-E2E-01: ``GET /me/traffic`` → 200 и все поля схемы. TC-E2E-02: ``POST /me/subscription/reissue``
без ``confirm=true`` → 409 с предупреждением о разрыве устройств. Сессия — настоящая (001.14):
регистрация, подтверждение, вход через ``_auth.auth_stand``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import asyncpg
import httpx
import pytest
from app.accounting.stats import GIB, STUB_STATS
from app.domain.profile import STUB_EMAIL
from app.security.sessions import SessionStore
from redis.exceptions import ConnectionError as RedisConnectionError

from ._auth import PASSWORD, AuthStand, auth_stand, cookies_of
from ._me import ME_OPERATIONS, MUTATIONS, REQUEST_BODY

TRAFFIC_FIELDS = {
    "period",
    "raw_uplink",
    "raw_downlink",
    "raw_total",
    "billable",
    "limit",
    "remaining",
    "by_node",
    "by_country",
    "active_ips",
    "device_limit",
}
CLIENTS_4_2 = {"Shadowrocket", "v2rayNG", "Streisand", "Hiddify", "sing-box"}
DOMAINS = ("sub1.example.com", "sub2.example.com")


@dataclass
class Cabinet:
    """Вошедший пользователь: стенд, клиент с cookie ``sid``, адрес, идентификатор, маркер CSRF."""

    stand: AuthStand
    client: httpx.AsyncClient
    address: str
    user_id: uuid.UUID
    csrf: str

    @property
    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf}


@asynccontextmanager
async def logged_in(pg_dsn: str, redis_url: str) -> AsyncIterator[Cabinet]:
    async with auth_stand(pg_dsn, redis_url) as stand:
        address = stand.email("cabinet")
        user_id = await stand.register_verified(address)
        async with stand.client() as client:
            response = await client.post(
                "/api/v1/auth/login", json={"email": address, "password": PASSWORD}
            )
            assert response.status_code == 200, response.text
            # Cookie выданы с Secure: хранилище cookie клиента httpx не вернёт их над http://,
            # поэтому cookie сессии ставится клиенту вручную; серверу нужна только она — cookie
            # csrf сверяется с записью сессии, а не с cookie (§7.3).
            sid, _ = cookies_of(response)["sid"]
            csrf, _ = cookies_of(response)["csrf"]
            client.cookies.clear()
            client.cookies.set("sid", sid)
            yield Cabinet(stand, client, address, user_id, csrf)


def schema_of(schema: dict[str, Any], name: str) -> dict[str, Any]:
    schemas = schema["components"]["schemas"]
    assert name in schemas, f"схемы {name} нет в OpenAPI"
    model: dict[str, Any] = schemas[name]
    return model


# --- контракт маршрутов ---------------------------------------------------------------------------


async def test_eight_operations_in_openapi_with_schemas(
    app_client: httpx.AsyncClient, pg_dsn: str
) -> None:
    schema = (await app_client.get("/openapi.json")).json()
    operations = {
        (method.upper(), path)
        for path, methods in schema["paths"].items()
        if path.startswith("/api/v1/me")
        for method in methods
    }
    assert operations == set(ME_OPERATIONS), "ровно восемь операций раздела /me"
    traffic = schema_of(schema, "TrafficStats")
    assert set(traffic["properties"]) == TRAFFIC_FIELDS, "состав §4.12"
    node = schema_of(schema, "NodeTraffic")
    assert {
        "node_id",
        "name",
        "country",
        "raw_uplink",
        "raw_downlink",
        "multiplier",
        "billable",
    } <= set(node["properties"])
    assert set(schema_of(schema, "CountryTraffic")["properties"]) == {
        "country",
        "raw_uplink",
        "raw_downlink",
        "billable",
    }
    subscription = schema_of(schema, "SubscriptionOut")
    assert set(subscription["properties"]) >= {
        "state",
        "plan",
        "period",
        "limit",
        "remaining",
        "links",
    }
    conn = await asyncpg.connect(pg_dsn)
    try:  # перечисление состояний — из базы, не из копии в тесте
        db_states = await conn.fetchval("select enum_range(null::subscription_state)::text[]")
    finally:
        await conn.close()
    assert set(subscription["properties"]["state"]["enum"]) == set(db_states)
    assert set(schema_of(schema, "Profile")["properties"]) == {
        "id",
        "email",
        "language",
        "timezone",
        "announce_consent",
        "aup_version",
        "created_at",
    }
    assert set(schema_of(schema, "OnboardingOut")["properties"]) == {
        "platform",
        "clients",
        "subscription_links",
        "import_url",
        "qr_payload",
        "check",
    }
    for path, methods in schema["paths"].items():
        if path.startswith("/api/v1/me"):
            for method, operation in methods.items():
                if method.upper() != "DELETE":
                    assert "application/json" in operation["responses"]["200"]["content"], (
                        method,
                        path,
                    )
                if path not in REQUEST_BODY:
                    # Зависимость с pydantic-параметром по умолчанию стала бы телом запроса
                    # (ревью раунда 2, S-1): у операций без тела его нет.
                    assert "requestBody" not in operation, (method, path)
    assert "Settings" not in schema["components"]["schemas"], "настройки — не часть контракта"


@pytest.mark.parametrize(("method", "path"), ME_OPERATIONS)
async def test_operations_require_a_user_session(
    app_client: httpx.AsyncClient, method: str, path: str
) -> None:
    """Без cookie сессии и с чужим/несуществующим идентификатором — 401 ``unauthenticated``,
    без cookie в ответе; тело запроса и подтверждение не рассматриваются раньше сессии."""
    for sid in (None, "no-such-session-" + "x" * 30):
        app_client.cookies.clear()
        if sid is not None:
            app_client.cookies.set("sid", sid)
        response = await app_client.request(
            method, path, params={"confirm": "true"}, json=REQUEST_BODY.get(path)
        )
        assert response.status_code == 401, (method, path, response.text)
        assert response.json()["error"]["code"] == "unauthenticated"
        assert "set-cookie" not in response.headers


@pytest.mark.parametrize(("method", "path"), MUTATIONS)
async def test_every_mutation_requires_csrf(
    pg_dsn: str, redis_url: str, method: str, path: str
) -> None:
    """Каждая изменяющая операция кабинета под живой сессией без ``X-CSRF-Token`` → 403
    ``csrf_failed`` (§7.3) — параметризовано по списку мутаций, чтобы новая мутация попадала под
    стража по построению (ревью раунда 1, C-1)."""
    async with logged_in(pg_dsn, redis_url) as cabinet:
        response = await cabinet.client.request(
            method, path, params={"confirm": "true"}, json=REQUEST_BODY.get(path)
        )
        assert response.status_code == 403, (method, path, response.text)
        assert response.json()["error"]["code"] == "csrf_failed"


# --- UC-16 на заглушках ---------------------------------------------------------------------------


async def test_profile_read_and_update(pg_dsn: str, redis_url: str) -> None:
    async with logged_in(pg_dsn, redis_url) as cabinet:
        client, headers = cabinet.client, cabinet.headers
        profile = await client.get("/api/v1/me")
        assert profile.status_code == 200, profile.text
        body = profile.json()
        assert body["id"] == str(cabinet.user_id), "профиль отдаётся под идентификатором сессии"
        assert body["email"] == STUB_EMAIL and body["language"] == "en", "заглушка 001.15"
        updated = await client.patch(
            "/api/v1/me",
            json={"language": "ru", "timezone": "Europe/Amsterdam", "announce_consent": True},
            headers=headers,
        )
        assert updated.status_code == 200, updated.text
        assert (updated.json()["language"], updated.json()["timezone"]) == (
            "ru",
            "Europe/Amsterdam",
        )
        assert updated.json()["announce_consent"] is True
        for bad in (
            {"language": "de"},
            {"timezone": "Mars/Olympus"},
            {"timezone": "  "},
            {"is_admin": True},
        ):
            response = await client.patch("/api/v1/me", json=bad, headers=headers)
            assert response.status_code == 422, (bad, response.text)
            assert response.json()["error"]["code"] == "validation_error"


async def test_uc14_delete_requires_confirmation(pg_dsn: str, redis_url: str) -> None:
    async with logged_in(pg_dsn, redis_url) as cabinet:
        client, headers = cabinet.client, cabinet.headers
        refused = await client.delete("/api/v1/me", headers=headers)
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "confirmation_required"
        assert "confirm=true" in refused.json()["error"]["details"]["confirm"]
        deleted = await client.delete("/api/v1/me", params={"confirm": "true"}, headers=headers)
        assert deleted.status_code == 204 and deleted.content == b""
        assert await cabinet.stand.user(cabinet.address) is not None, (
            "заглушка 001.15 ничего не удаляет — логика UC-14 в 001.17"
        )


async def test_subscription_redeem_and_reissue(pg_dsn: str, redis_url: str) -> None:
    """TC-E2E-02: перевыпуск без ``confirm=true`` → 409 с предупреждением о разрыве устройств;
    с подтверждением — новая ссылка на обоих доменах."""
    async with logged_in(pg_dsn, redis_url) as cabinet:
        client, headers = cabinet.client, cabinet.headers
        current = await client.get("/api/v1/me/subscription")
        assert current.status_code == 200, current.text
        body = current.json()
        assert body["state"] == "active" and body["plan"]["name"]
        assert body["remaining"] == body["limit"] - body["used_billable"]
        links = body["links"]
        assert [link.split("/")[2] for link in links] == list(DOMAINS), "два домена §4.2"
        assert len({link.rsplit("/", 1)[1] for link in links}) == 1, "один токен на обоих доменах"
        assert all("/s/" in link and link.startswith("https://") for link in links)
        refused = await client.post("/api/v1/me/subscription/reissue", headers=headers)
        assert refused.status_code == 409, refused.text
        error = refused.json()["error"]
        assert error["code"] == "confirmation_required"
        assert "устройств" in error["details"]["warning"], "предупреждение о разрыве устройств"
        reissued = await client.post(
            "/api/v1/me/subscription/reissue", params={"confirm": "true"}, headers=headers
        )
        assert reissued.status_code == 200, reissued.text
        assert reissued.json()["links"] != links, "новый токен"
        assert [link.split("/")[2] for link in reissued.json()["links"]] == list(DOMAINS)
        redeemed = await client.post(
            "/api/v1/me/subscription/redeem", json={"code": "ABCD-EFGH-IJKL"}, headers=headers
        )
        assert redeemed.status_code == 200 and redeemed.json()["state"] == "active"
        for bad in ({"code": "short"}, {"code": "ABCD-EFGH-IJKL", "plan": "vip"}, {}):
            response = await client.post(
                "/api/v1/me/subscription/redeem", json=bad, headers=headers
            )
            assert response.status_code == 422, (bad, response.text)


async def test_traffic_stats_composition(pg_dsn: str, redis_url: str) -> None:
    """TC-E2E-01: состав §4.12 с фиксированными числами примера (коэффициент 2.0)."""
    async with logged_in(pg_dsn, redis_url) as cabinet:
        client = cabinet.client
        response = await client.get("/api/v1/me/traffic")
        assert response.status_code == 200, response.text
        stats = response.json()
        assert set(stats) == TRAFFIC_FIELDS
        assert stats["raw_total"] == stats["raw_uplink"] + stats["raw_downlink"] == 10 * GIB
        node = stats["by_node"][0]
        assert node["multiplier"] == "2.0" and node["billable"] == 2 * (
            node["raw_uplink"] + node["raw_downlink"]
        ), "пример §4.12: 10 ГБ × 2.0 = 20 ГБ"
        assert stats["billable"] == sum(n["billable"] for n in stats["by_node"]) == 20 * GIB
        assert stats["remaining"] == stats["limit"] - stats["billable"]
        assert stats["by_country"] == [
            {"country": "NL", "raw_uplink": 2 * GIB, "raw_downlink": 8 * GIB, "billable": 20 * GIB}
        ]
        assert (stats["active_ips"], stats["device_limit"]) == (1, 3)
        assert stats["period"]["id"] == str(STUB_STATS.period.id)
        by_period = await client.get("/api/v1/me/traffic", params={"period": stats["period"]["id"]})
        assert by_period.status_code == 200 and by_period.json() == stats
        bad = await client.get("/api/v1/me/traffic", params={"period": "current"})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation_error"


async def test_onboarding_wizard(pg_dsn: str, redis_url: str) -> None:
    async with logged_in(pg_dsn, redis_url) as cabinet:
        response = await cabinet.client.get(
            "/api/v1/me/onboarding",
            headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)"},
        )
        assert response.status_code == 200, response.text
        wizard = response.json()
        assert {c["name"] for c in wizard["clients"]} == CLIENTS_4_2, "клиенты §4.2"
        assert all(c["install_url"].startswith("https://") for c in wizard["clients"])
        links = wizard["subscription_links"]
        assert [link.split("/")[2] for link in links] == list(DOMAINS)
        assert wizard["qr_payload"] == links[0]
        scheme, _, query = wizard["import_url"].partition("://import-remote-profile?url=")
        assert scheme == "sing-box" and unquote(query) == links[0], "ссылка закодирована в query"
        assert "/" not in query, "процентное кодирование ссылки в параметре импорта"


async def test_redis_failure_mid_request_is_503_not_500(
    pg_dsn: str, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Вторая линия fail-closed (§9.1): Redis ответил на ping первой линии, но упал при чтении
    сессии — 503 с ``Retry-After`` от обработчика ошибок, а не 500 (ревью раунда 2, C-1)."""

    async def broken(self: SessionStore, sid: str) -> None:
        raise RedisConnectionError("Redis ушёл посреди запроса")

    async with logged_in(pg_dsn, redis_url) as cabinet:
        monkeypatch.setattr(SessionStore, "get", broken)
        for method, path in ME_OPERATIONS:
            response = await cabinet.client.request(
                method, path, params={"confirm": "true"}, json=REQUEST_BODY.get(path)
            )
            assert response.status_code == 503, (method, path, response.text)
            assert response.headers["retry-after"] == "5", (method, path)
            assert response.json()["error"]["code"] == "service_unavailable"


async def test_missing_subscription_domains_is_a_loud_configuration_error(
    pg_dsn: str, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без ``SUBSCRIPTION_DOMAINS`` кабинет не отдаёт 200 с пустыми ссылками (§4.2 требует две):
    ошибка конфигурации — 500 единого формата в журнал (ревью раунда 1, L-1)."""
    async with logged_in(pg_dsn, redis_url) as cabinet:
        monkeypatch.setenv("SUBSCRIPTION_DOMAINS", "")
        for path in ("/api/v1/me/subscription", "/api/v1/me/onboarding"):
            response = await cabinet.client.get(path)
            assert response.status_code == 500, (path, response.text)
            assert response.json()["error"]["code"] == "internal_error"


async def test_admin_session_is_not_a_cabinet_user(pg_dsn: str, redis_url: str) -> None:
    """Сессия администратора (§7.1, 001.47) не открывает кабинет: ``current_user`` принимает
    только сессии вида ``user``."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        store = SessionStore(stand.redis)
        subject = str(uuid.uuid4())  # admin_users.id — uuid, как и users.id: отличает только вид
        session = await store.create("admin", subject, "203.0.113.9", "test", 600)
        try:
            async with stand.client() as client:
                client.cookies.set("sid", session.id)
                response = await client.get("/api/v1/me")
                assert response.status_code == 401, response.text
                assert response.json()["error"]["code"] == "unauthenticated"
        finally:
            await store.revoke_all(subject)
