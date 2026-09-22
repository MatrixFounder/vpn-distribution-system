"""Сквозные проверки панели: тарифы, группы, коды (задачи 001.18 и 001.19; UC-09) на живом
стенде: каждая операция `/admin` из ``ADMIN_OPERATIONS`` (и никакая другая) есть в OpenAPI с одним
разрешением на операцию (R-35, ``x-permission``), все под сессией администратора (401 без неё,
сессия пользователя не подходит), мутации под CSRF, fail-closed без Redis (в
``test_security_fail_closed``), CRUD тарифа 201/200/200/204, экспорт партии в ``text/csv`` с
заголовком ``code,expires_at,plan``; коды в ответах проходят контрольную сумму (§16.8).

001.19 добавила проверки настоящей логики: тариф и группы переживают запрос (читаются из базы
другим подключением), **TC-E2E-01** — вторая тарифицируемая группа ноды отклонена 409 и
``nodes.billing_group_id`` не изменился (UC-09 A1), **TC-E2E-02** — два изменения коэффициента
подряд дают два интервала без пересечения (§4.9), и критерий приёмки «добавление ноды в группу
доступа не изменяет тарифы». Коды (001.20) остаются на заглушках. Операции нод (001.24) — в том
же перечне, их сценарий — ``test_nodes``."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import httpx
import pytest
from app.domain import codes
from app.domain.groups import Assignment, GroupService
from app.errors import ApiError
from app.security.sessions import SessionStore

from ._admin import (
    ADMIN_OPERATIONS,
    MUTATIONS,
    VALID_PLAN,
    admin_stand,
    openapi_path,
)
from ._auth import PASSWORD, auth_stand, cookies_of
from ._catalog import RecordingHours, access_group, billing_group, catalog, node, unique_name


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


async def test_uc09_plan_crud_persists(pg_dsn: str, redis_url: str) -> None:
    """CRUD тарифа через панель с проверкой, что записано в базу, а не только отвечено:
    создание 201, список 200, изменение 200, архивирование 204; валидация полей §4.10 (срок > 0,
    валюта ISO 4217, профили из перечисления, лимиты, цена, имя); PATCH: ``null`` сбрасывает
    необязательное поле (Unlimited), для NOT NULL полей — 422, непереданное поле не меняется.
    Состав тарифа (группы доступа, профили) заменяется целиком переданным списком."""
    async with catalog(pg_dsn) as conn, admin_stand(pg_dsn, redis_url) as admin:
        client, headers = admin.client, admin.headers
        group_id = await access_group(conn, "eu")
        other_group_id = await access_group(conn, "asia")
        body = {**VALID_PLAN, "name": unique_name("basic"), "access_group_ids": [str(group_id)]}

        created = await client.post("/api/v1/admin/plans", json=body, headers=headers)
        assert created.status_code == 201, created.text
        plan = created.json()
        assert plan["name"] == body["name"] and plan["status"] == "active"
        assert plan["price_currency"] == "EUR" and plan["profiles"] == ["vless_raw_vision"]
        assert plan["traffic_limit_bytes"] == 100 * 1024**3 and plan["device_limit"] == 2
        assert plan["access_group_ids"] == [str(group_id)]
        # Запрос кончился — тариф остался: читаем другим подключением, а не из ответа.
        stored = await conn.fetchrow(
            "select name, status, device_limit from plans where id = $1", uuid.UUID(plan["id"])
        )
        assert stored is not None and stored["name"] == body["name"]
        assert stored["status"] == "active" and stored["device_limit"] == 2

        assert (
            await client.post("/api/v1/admin/plans", json=body, headers=headers)
        ).status_code == 409, "имя тарифа уникально"
        unknown = await client.post(
            "/api/v1/admin/plans",
            json={**body, "name": unique_name("ghost"), "access_group_ids": [str(uuid.uuid4())]},
            headers=headers,
        )
        assert unknown.status_code == 422, unknown.text

        listed = await client.get("/api/v1/admin/plans")
        assert listed.status_code == 200
        assert plan["id"] in {p["id"] for p in listed.json()}

        updated = await client.patch(
            f"/api/v1/admin/plans/{plan['id']}",
            json={
                "name": unique_name("basic-plus"),
                "traffic_limit_bytes": None,
                "access_group_ids": [str(other_group_id)],
            },
            headers=headers,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["id"] == plan["id"]
        assert updated.json()["traffic_limit_bytes"] is None, "null сбрасывает лимит в Unlimited"
        assert updated.json()["device_limit"] == 2, "непереданное поле не тронуто"
        assert updated.json()["access_group_ids"] == [str(other_group_id)], "состав заменён"
        assert await conn.fetchval(
            "select array_agg(access_group_id) from plan_access_groups where plan_id = $1",
            uuid.UUID(plan["id"]),
        ) == [other_group_id]

        for null_forbidden in ("name", "duration_days", "profiles", "access_group_ids"):
            response = await client.patch(
                f"/api/v1/admin/plans/{plan['id']}", json={null_forbidden: None}, headers=headers
            )
            assert response.status_code == 422, (null_forbidden, response.text)

        archived = await client.delete(f"/api/v1/admin/plans/{plan['id']}", headers=headers)
        assert archived.status_code == 204 and archived.content == b""
        assert (
            await conn.fetchval("select count(*) from plans where id = $1", uuid.UUID(plan["id"]))
            == 0
        ), "тариф без подписок удаляется"
        assert (
            await client.delete(f"/api/v1/admin/plans/{plan['id']}", headers=headers)
        ).status_code == 404

        for bad in (
            {**body, "duration_days": 0},
            {**body, "price_currency": "eur"},
            {**body, "profiles": ["wireguard"]},
            {**body, "profiles": []},
            {**body, "device_limit": 0},
            {**body, "price_amount": "-1"},
            {**body, "name": ""},
        ):
            response = await client.post("/api/v1/admin/plans", json=bad, headers=headers)
            assert response.status_code == 422, (bad, response.text)
        assert (
            await client.patch("/api/v1/admin/plans/not-a-uuid", json={}, headers=headers)
        ).status_code == 422


async def test_uc09_a4_plan_with_a_period_is_archived_not_deleted(
    pg_dsn: str, redis_url: str
) -> None:
    """UC-09 A4: удаление тарифа, названного периодом подписки, отклонено — тариф переводится в
    ``archived``, а период продолжает на него ссылаться. Ответ тот же 204: панель просит убрать
    тариф из выдачи, и он убран."""
    async with catalog(pg_dsn) as conn, admin_stand(pg_dsn, redis_url) as admin:
        plan_id = await conn.fetchval(
            "insert into plans (name, duration_days) values ($1, 30) returning id",
            unique_name("used"),
        )
        user_id = await conn.fetchval(
            "insert into users (email, password_hash, aup_version, aup_accepted_at) "
            "values ($1, 'x', '2026-09', now()) returning id",
            f"{unique_name('holder')}@test-auth.local",
        )
        await conn.execute(
            "insert into subscription_periods (user_id, plan_id, period_start, period_end, source) "
            "values ($1, $2, now(), now() + interval '30 days', 'admin')",
            user_id,
            plan_id,
        )
        response = await admin.client.delete(
            f"/api/v1/admin/plans/{plan_id}", headers=admin.headers
        )
        assert response.status_code == 204, response.text
        assert await conn.fetchval("select status from plans where id = $1", plan_id) == "archived"
        assert (
            await conn.fetchval(
                "select count(*) from subscription_periods where plan_id = $1", plan_id
            )
            == 1
        ), "период продолжает ссылаться на тариф"
        await conn.execute("delete from subscription_periods where plan_id = $1", plan_id)
        await conn.execute("delete from users where id = $1", user_id)


async def test_uc09_groups_and_multiplier_step(pg_dsn: str, redis_url: str) -> None:
    """UC-09 шаги 1–2: группы доступа и тарифицируемые группы записываются в базу; A2 —
    коэффициент 0.0…10.0 с шагом 0.1, иначе 422. Новая тарифицируемая группа — без действующего
    коэффициента: он датирован и заводится отдельным интервалом (§4.9)."""
    async with catalog(pg_dsn) as conn, admin_stand(pg_dsn, redis_url) as admin:
        client, headers = admin.client, admin.headers
        access_name = unique_name("asia")
        access = await client.post(
            "/api/v1/admin/groups/access",
            json={"name": access_name, "description": "APAC"},
            headers=headers,
        )
        assert access.status_code == 201 and access.json()["name"] == access_name
        access_id = uuid.UUID(access.json()["id"])
        assert await conn.fetchval("select name from access_groups where id = $1", access_id) == (
            access_name
        )
        assert (
            await client.post(
                "/api/v1/admin/groups/access", json={"name": access_name}, headers=headers
            )
        ).status_code == 409, "имя группы доступа уникально"

        billing_name = unique_name("premium")
        billing = await client.post(
            "/api/v1/admin/groups/billing", json={"name": billing_name}, headers=headers
        )
        assert billing.status_code == 201 and billing.json()["current_multiplier"] is None
        group_id = billing.json()["id"]
        # Нода в группе обязательна: без неё смена коэффициента не доходит до закрытия часа, и
        # маршрут, собравший службу без службы учёта, остался бы незамеченным (UC-09 A3).
        await node(conn, uuid.UUID(group_id), 65)
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
        listed = await client.get("/api/v1/admin/groups/billing")
        shown = {g["id"]: g["current_multiplier"] for g in listed.json()}
        assert shown[group_id] == "9.9", "показывается действующий интервал, а не первый"

        renamed = await client.patch(
            f"/api/v1/admin/groups/billing/{group_id}",
            json={"name": unique_name("premium-plus")},
            headers=headers,
        )
        assert renamed.status_code == 200 and renamed.json()["current_multiplier"] == "9.9"
        assert (
            await client.delete(f"/api/v1/admin/groups/access/{access_id}", headers=headers)
        ).status_code == 204
        assert (
            await client.delete(f"/api/v1/admin/groups/access/{access_id}", headers=headers)
        ).status_code == 404


async def test_uc09_a1_second_billing_group_of_a_node_is_rejected(pg_dsn: str) -> None:
    """TC-E2E-01 (UC-09 A1, R-18): двум одновременным назначениям разных тарифицируемых групп
    одной ноде отвечают 201 и 409 — ровно одну группу держит ограничение целостности, а не
    проверка «прочитать, потом записать». Проигравшая транзакция откатывается целиком, поэтому
    ``nodes.billing_group_id`` равен группе победителя, а не остаётся между ними."""
    async with catalog(pg_dsn) as conn:
        home = await billing_group(conn, "home")
        first = await billing_group(conn, "first")
        second = await billing_group(conn, "second")
        node_id = await node(conn, home, 61)
        await conn.execute(
            "insert into node_billing_assignments (node_id, billing_group_id, valid_from) "
            "values ($1, $2, now())",
            node_id,
            home,
        )
        pool = await asyncpg.create_pool(pg_dsn, min_size=2, max_size=4)
        assert pool is not None
        try:
            hours = RecordingHours()
            service = GroupService(pool, hours)
            outcomes = await asyncio.gather(
                service.set_billing_group(node_id, first),
                service.set_billing_group(node_id, second),
                return_exceptions=True,
            )
        finally:
            await pool.close()
        errors = [o for o in outcomes if isinstance(o, ApiError)]
        winners = [o for o in outcomes if isinstance(o, Assignment)]
        assert len(winners) == 1 and len(errors) == 1, outcomes
        assert errors[0].status == 409, errors[0].message
        assert await conn.fetchval("select billing_group_id from nodes where id = $1", node_id) == (
            winners[0].billing_group_id
        )
        open_intervals = await conn.fetchval(
            "select count(*) from node_billing_assignments where node_id = $1 and valid_to is null",
            node_id,
        )
        assert open_intervals == 1, "действующее назначение одно (R-18)"


async def test_uc09_a3_multiplier_history_has_no_overlap(pg_dsn: str) -> None:
    """TC-E2E-02 (§4.9, B-1): два изменения коэффициента подряд дают два интервала — прежний
    закрыт моментом, которым открыт новый, ``EXCLUDE`` не нарушен, история не переписана.
    UC-09 A3: час учёта закрыт у ноды группы, и закрытие шло на подключении той же транзакции;
    нода с собственным переопределением групповой сменой не затронута."""
    async with catalog(pg_dsn) as conn:
        group = await billing_group(conn, "tier")
        plain = await node(conn, group, 62)
        overridden = await node(conn, group, 63)
        await conn.execute("update nodes set multiplier_milli = 300 where id = $1", overridden)
        pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)
        assert pool is not None
        try:
            hours = RecordingHours()
            service = GroupService(pool, hours)
            first = await service.set_group_multiplier(group, 2000)
            second = await service.set_group_multiplier(group, 3000)
        finally:
            await pool.close()

        rows = await conn.fetch(
            "select multiplier_milli, valid_from, valid_to from billing_group_multipliers "
            "where billing_group_id = $1 order by valid_from",
            group,
        )
        assert [r["multiplier_milli"] for r in rows] == [2000, 3000]
        assert rows[0]["valid_to"] == rows[1]["valid_from"], "интервалы стыкуются без зазора"
        assert rows[1]["valid_to"] is None, "последний интервал действует"
        assert first.valid_to is None and second.multiplier == Decimal("3.0")
        assert hours.nodes == [plain, plain], "час закрыт только у ноды без переопределения"


async def test_adding_a_node_to_an_access_group_does_not_change_plans(pg_dsn: str) -> None:
    """Критерий приёмки UC-09: добавление ноды в группу доступа делает её доступной тарифам с
    этой группой и не меняет сами тарифы. Проверяется составом тарифа до и после: строки
    ``plan_access_groups`` и поля тарифа те же, а связь ноды с группой появилась."""
    async with catalog(pg_dsn) as conn:
        group = await access_group(conn, "shared")
        home = await billing_group(conn, "home")
        node_id = await node(conn, home, 64)
        plan_id = await conn.fetchval(
            "insert into plans (name, duration_days) values ($1, 30) returning id",
            unique_name("fixed"),
        )
        await conn.execute(
            "insert into plan_access_groups (plan_id, access_group_id) values ($1, $2)",
            plan_id,
            group,
        )
        before = await conn.fetchrow(
            "select name, duration_days, status, updated_at from plans where id = $1", plan_id
        )
        composition_before = await conn.fetch(
            "select access_group_id from plan_access_groups where plan_id = $1", plan_id
        )

        await conn.execute(
            "insert into node_access_groups (node_id, access_group_id) values ($1, $2)",
            node_id,
            group,
        )

        after = await conn.fetchrow(
            "select name, duration_days, status, updated_at from plans where id = $1", plan_id
        )
        composition_after = await conn.fetch(
            "select access_group_id from plan_access_groups where plan_id = $1", plan_id
        )
        assert dict(after) == dict(before), "тариф не изменился"
        assert [dict(r) for r in composition_after] == [dict(r) for r in composition_before]
        assert (
            await conn.fetchval(
                "select count(*) from node_access_groups "
                "where node_id = $1 and access_group_id = $2",
                node_id,
                group,
            )
            == 1
        ), "нода в группе доступа"


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
