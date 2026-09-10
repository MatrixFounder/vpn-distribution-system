"""Сквозные проверки панели: тарифы, группы, коды (задача 001.18; UC-09) на заглушках, живой
стенд: каждая операция `/admin` из ``ADMIN_OPERATIONS`` (и никакая другая) есть в OpenAPI с одним
разрешением на операцию (R-35, ``x-permission``), все под сессией администратора (401 без неё,
сессия пользователя не подходит), мутации под CSRF, fail-closed без Redis (в
``test_security_fail_closed``), TC-E2E-01 CRUD тарифа 201/200/200/204, TC-E2E-02 экспорт партии в
``text/csv`` с заголовком ``code,expires_at,plan``; коды в ответах проходят контрольную сумму
(§16.8). Операции нод (001.24) — в том же перечне, их сценарий — ``test_nodes``."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.domain import codes
from app.security.sessions import SessionStore

from ._admin import (
    ADMIN_OPERATIONS,
    GROUP_ID,
    MUTATIONS,
    VALID_PLAN,
    admin_stand,
    openapi_path,
)
from ._auth import PASSWORD, auth_stand, cookies_of


async def test_admin_operations_in_openapi_with_one_permission_each(
    app_client: httpx.AsyncClient,
) -> None:
    schema = (await app_client.get("/openapi.json")).json()
    operations = {
        (method.upper(), path): operation
        for path, methods in schema["paths"].items()
        if path.startswith("/api/v1/admin")
        for method, operation in methods.items()
    }
    expected = {(m, openapi_path(p)) for m, p, _ in ADMIN_OPERATIONS}
    assert len(expected) == len(ADMIN_OPERATIONS) == 28, "перечень без дубликатов: 18 + 10 нод"
    assert set(operations) == expected, "операции раздела /admin — ровно ADMIN_OPERATIONS"
    for (method, path), operation in operations.items():
        declared = operation.get("x-permission")
        assert declared, f"{(method, path)}: операция без разрешения (R-35)"
        section, action = declared.split(".")
        # Разрешение по смыслу: раздел — сегмент пути, действие — read для GET и write для
        # мутаций; мутация с read уехала бы в матрицу 001.46 правом на чтение (ревью, S-1).
        assert section == path.split("/")[4], (method, path, declared)
        assert action == ("read" if method == "GET" else "write"), (method, path, declared)
        if method in ("GET", "DELETE"):
            assert "requestBody" not in operation, (method, path)
        assert operation["tags"] == ["admin"], (method, path, operation["tags"])
    schemas = schema["components"]["schemas"]
    for name in (
        "PlanIn",
        "PlanPatch",
        "Plan",
        "AccessGroupIn",
        "BillingGroupIn",
        "MultiplierIn",
        "Multiplier",
        "CodeSpec",
        "Code",
        "BatchIn",
        "BatchOut",
        "Redemption",
    ):
        assert name in schemas, name
    assert set(schemas["PlanIn"]["properties"]) == {
        "name",
        "price_amount",
        "price_currency",
        "duration_days",
        "traffic_limit_bytes",
        "device_limit",
        "access_group_ids",
        "profiles",
    }, "поля §4.2.2 plans"
    assert set(schemas["CodeSpec"]["properties"]) == {
        "kind",
        "plan_id",
        "expires_at",
        "max_uses",
        "max_uses_per_user",
        "traffic_bonus_bytes",
        "duration_bonus_days",
    }, "поля §4.2.4 codes"
    export = operations[("GET", "/api/v1/admin/codes/batch/{batch_id}/export")]
    assert set(export["responses"]["200"]["content"]) == {"text/csv"}, "только CSV"
    assert "Settings" not in schemas


@pytest.mark.parametrize(("method", "path", "body"), ADMIN_OPERATIONS)
async def test_operations_require_an_admin_session(
    pg_dsn: str, redis_url: str, method: str, path: str, body: dict[str, Any] | None
) -> None:
    """Без сессии, с несуществующей и с сессией пользователя — 401 ``unauthenticated``."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        address = stand.email("notadmin")
        await stand.register_verified(address)
        async with stand.client() as client:
            login = await client.post(
                "/api/v1/auth/login", json={"email": address, "password": PASSWORD}
            )
            user_sid, _ = cookies_of(login)["sid"]
            user_csrf, _ = cookies_of(login)["csrf"]
            for sid in (None, "no-such-session-" + "x" * 30, user_sid):
                client.cookies.clear()
                if sid is not None:
                    client.cookies.set("sid", sid)
                response = await client.request(
                    method, path, json=body, headers={"X-CSRF-Token": user_csrf}
                )
                assert response.status_code == 401, (method, path, sid, response.text)
                assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize(("method", "path", "body"), MUTATIONS)
async def test_every_mutation_requires_csrf(
    pg_dsn: str, redis_url: str, method: str, path: str, body: dict[str, Any] | None
) -> None:
    async with admin_stand(pg_dsn, redis_url) as admin:
        response = await admin.client.request(method, path, json=body)
        assert response.status_code == 403, (method, path, response.text)
        assert response.json()["error"]["code"] == "csrf_failed"


async def test_uc09_plan_crud_on_stubs(pg_dsn: str, redis_url: str) -> None:
    """TC-E2E-01: создание 201, чтение 200, изменение 200, архивирование 204; валидация полей
    §4.10 (срок > 0, валюта ISO 4217, профили из перечисления, лимиты, цена, имя); PATCH:
    ``null`` сбрасывает необязательное поле (Unlimited), а для NOT NULL полей — 422, непереданное
    поле не меняется. Неизвестные поля принимаются молча (объявлено в задаче)."""
    async with admin_stand(pg_dsn, redis_url) as admin:
        client, headers = admin.client, admin.headers
        created = await client.post("/api/v1/admin/plans", json=VALID_PLAN, headers=headers)
        assert created.status_code == 201, created.text
        plan = created.json()
        assert plan["name"] == "Basic" and plan["status"] == "active"
        assert plan["price_currency"] == "EUR" and plan["profiles"] == ["vless_raw_vision"]
        assert plan["traffic_limit_bytes"] == 100 * 1024**3
        listed = await client.get("/api/v1/admin/plans")
        assert listed.status_code == 200 and isinstance(listed.json(), list)
        assert {p["id"] for p in listed.json()}, "хотя бы один тариф в ответе"
        updated = await client.patch(
            f"/api/v1/admin/plans/{plan['id']}",
            json={"name": "Basic+", "traffic_limit_bytes": None},
            headers=headers,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["id"] == plan["id"] and updated.json()["name"] == "Basic+"
        assert updated.json()["traffic_limit_bytes"] is None, "null сбрасывает лимит в Unlimited"
        assert updated.json()["device_limit"] == 3, "непереданное поле не тронуто"
        for null_forbidden in ("name", "duration_days", "profiles", "access_group_ids"):
            response = await client.patch(
                f"/api/v1/admin/plans/{plan['id']}", json={null_forbidden: None}, headers=headers
            )
            assert response.status_code == 422, (null_forbidden, response.text)
        archived = await client.delete(f"/api/v1/admin/plans/{plan['id']}", headers=headers)
        assert archived.status_code == 204 and archived.content == b""
        for bad in (
            {**VALID_PLAN, "duration_days": 0},
            {**VALID_PLAN, "price_currency": "eur"},
            {**VALID_PLAN, "profiles": ["wireguard"]},
            {**VALID_PLAN, "profiles": []},
            {**VALID_PLAN, "device_limit": 0},
            {**VALID_PLAN, "price_amount": "-1"},
            {**VALID_PLAN, "name": ""},
        ):
            response = await client.post("/api/v1/admin/plans", json=bad, headers=headers)
            assert response.status_code == 422, (bad, response.text)
        assert (
            await client.patch("/api/v1/admin/plans/not-a-uuid", json={}, headers=headers)
        ).status_code == 422


async def test_uc09_groups_and_multiplier_step(pg_dsn: str, redis_url: str) -> None:
    """UC-09 шаги 1–2: группы доступа и тарифицируемые группы; A2 — коэффициент 0.0…10.0 с
    шагом 0.1, иначе 422."""
    async with admin_stand(pg_dsn, redis_url) as admin:
        client, headers = admin.client, admin.headers
        access = await client.post(
            "/api/v1/admin/groups/access",
            json={"name": "Asia", "description": "APAC"},
            headers=headers,
        )
        assert access.status_code == 201 and access.json()["name"] == "Asia"
        assert (await client.get("/api/v1/admin/groups/access")).status_code == 200
        billing = await client.post(
            "/api/v1/admin/groups/billing", json={"name": "premium"}, headers=headers
        )
        assert billing.status_code == 201 and billing.json()["current_multiplier"] is None
        group_id = billing.json()["id"]
        for ok in ("0", "0.1", "2.0", "10", "9.9"):
            response = await client.post(
                f"/api/v1/admin/groups/billing/{group_id}/multiplier",
                json={"multiplier": ok},
                headers=headers,
            )
            assert response.status_code == 201, (ok, response.text)
            assert response.json()["billing_group_id"] == group_id
            assert response.json()["valid_to"] is None
        for bad in ("2.05", "10.1", "-0.1", "abc", "0.15"):
            response = await client.post(
                f"/api/v1/admin/groups/billing/{group_id}/multiplier",
                json={"multiplier": bad},
                headers=headers,
            )
            assert response.status_code == 422, (bad, response.text)
        renamed = await client.patch(
            f"/api/v1/admin/groups/billing/{group_id}", json={"name": "premium+"}, headers=headers
        )
        assert renamed.status_code == 200 and renamed.json()["name"] == "premium+"
        assert (
            await client.delete(f"/api/v1/admin/groups/access/{GROUP_ID}", headers=headers)
        ).status_code == 204
        assert (
            await client.delete(f"/api/v1/admin/groups/billing/{group_id}", headers=headers)
        ).status_code == 204


async def test_uc09_codes_batch_export_and_redemptions(pg_dsn: str, redis_url: str) -> None:
    """UC-09 шаги 4–5, TC-E2E-02: один код (проходит контрольную сумму, показан один раз),
    партия, экспорт ``text/csv`` с заголовком ``code,expires_at,plan`` и кодами с верной
    суммой, использования кода; неверная спецификация → 422."""
    async with admin_stand(pg_dsn, redis_url) as admin:
        client, headers = admin.client, admin.headers
        one = await client.post(
            "/api/v1/admin/codes",
            json={"kind": "redeem", "max_uses": 5, "duration_bonus_days": 7},
            headers=headers,
        )
        assert one.status_code == 201, one.text
        assert codes.checksum_ok(one.json()["code"]) and one.json()["max_uses"] == 5
        assert one.json()["uses_count"] == 0 and one.json()["kind"] == "redeem"
        batch = await client.post(
            "/api/v1/admin/codes/batch",
            json={"count": 3, "spec": {"kind": "promo", "traffic_bonus_bytes": 1024}},
            headers=headers,
        )
        assert batch.status_code == 201 and batch.json()["count"] == 3
        export = await client.get(f"/api/v1/admin/codes/batch/{batch.json()['batch_id']}/export")
        assert export.status_code == 200, export.text
        assert export.headers["content-type"].startswith("text/csv")
        assert export.headers["cache-control"] == "no-store", "коды в открытом виде не кэшируются"
        assert export.headers["content-disposition"].startswith("attachment; filename="), (
            "экспорт — вложение"
        )
        assert export.text.endswith("\r\n") and "\r\n" in export.text, "RFC 4180: CRLF"
        lines = export.text.strip().splitlines()
        assert lines[0] == "code,expires_at,plan"
        assert len(lines) >= 2, "экспорт содержит коды партии"
        for line in lines[1:]:
            code, expires_at, plan = line.split(",")
            assert codes.checksum_ok(code), code
            assert uuid.UUID(plan) and expires_at
        redemptions = await client.get(f"/api/v1/admin/codes/{one.json()['id']}/redemptions")
        assert redemptions.status_code == 200
        assert {"code_id", "user_id", "period_id", "redeemed_at"} <= set(redemptions.json()[0])
        for bad in (
            {"kind": "gift"},
            {"max_uses": 0},
            {"traffic_bonus_bytes": -1},
            {"count": 0, "spec": {}},
        ):
            path = "/api/v1/admin/codes/batch" if "count" in bad else "/api/v1/admin/codes"
            response = await client.post(path, json=bad, headers=headers)
            assert response.status_code == 422, (bad, response.text)


async def test_redeem_rejects_bad_checksum_without_database(pg_dsn: str, redis_url: str) -> None:
    """§16.8 / UC-02 A3: неверная контрольная сумма отклоняется сервисом до обращения к базе —
    пул не трогается (заглушка получает намеренно негодный «пул»)."""
    from app.domain.codes import CodeService
    from app.errors import ApiError

    service = CodeService(pool=None)
    with pytest.raises(ApiError) as denied:
        await service.redeem(uuid.uuid4(), codes.EXAMPLE_INVALID)
    assert (denied.value.status, denied.value.code) == (400, "invalid_code")
    redemption = await service.redeem(uuid.uuid4(), codes.EXAMPLE_VALID.lower())
    assert redemption.code_id, "верный код доходит до заглушки"


async def test_user_logout_all_keeps_admin_session(pg_dsn: str, redis_url: str) -> None:
    """«Выход везде» настоящего пользователя (свой субъект в том же хранилище) не трогает
    сессию администратора; сессия пользователя того же UUID-пространства панель не открывает."""
    async with admin_stand(pg_dsn, redis_url) as admin:
        stand = admin.stand
        address = stand.email("bystander")
        await stand.register_verified(address)
        async with stand.client() as user_client:
            login = await user_client.post(
                "/api/v1/auth/login", json={"email": address, "password": PASSWORD}
            )
            assert login.status_code == 200
            sid, _ = cookies_of(login)["sid"]
            csrf, _ = cookies_of(login)["csrf"]
            user_client.cookies.clear()
            user_client.cookies.set("sid", sid)
            everywhere = await user_client.post(
                "/api/v1/auth/logout-all", headers={"X-CSRF-Token": csrf}
            )
            assert everywhere.status_code == 204
        assert (await admin.client.get("/api/v1/admin/plans")).status_code == 200, (
            "сессия администратора пережила «выход везде» пользователя"
        )
        store = SessionStore(stand.redis)
        assert await store.get(sid) is None
