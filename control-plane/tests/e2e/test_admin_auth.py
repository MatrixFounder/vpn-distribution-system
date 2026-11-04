"""Вход администратора, проверка разрешений и журнал (задача 001.46; R-23, R-35, R-36, R-37;
UC-09, UC-13) на живом стенде: вход без второго фактора не завершён (TC-E2E-01), отказ по
разрешению — 403 и запись ``denied`` (TC-E2E-02), своя сессия — роль, разрешения и язык
(TC-E2E-03), разрешения принятых маршрутов и журнал смены коэффициента (TC-E2E-04).

Роль и статус администратора читаются из ``admin_users`` на каждом запросе (решение заказчика
2026-11-04): понижение роли, блокировка и удалённая строка действуют со следующего запроса.
``audit.record`` — заглушка без записи (строку ``audit_log`` пишет 001.48): вызовы
перехватываются подменой (``Recorder``), а то, что сама заглушка в базу не пишет, проверяет
отдельный тест без подмены. ``Recorder`` запоминает транзакцию вызова (``pg_current_xact_id``):
по ней сверяется, что журнал идёт в транзакции самой записи и что транзакция отказа
зафиксирована."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import asyncpg
import httpx
import pytest
from app.domain.groups import GroupService
from app.security import audit
from app.security.audit import AuditContext
from app.security.rbac import PERMISSIONS
from app.security.sessions import SessionStore

from ._admin import ADMIN_OPERATIONS, BATCH_ID, admin_stand, openapi_path
from ._auth import auth_stand
from ._catalog import RecordingHours, billing_group, catalog, node

CTX = AuditContext(
    actor_type="admin",
    actor_id=uuid.UUID("00000000-0000-7000-8000-00000000ad46"),
    actor_role="super_admin",
    session_id="t46-session",
    ip="203.0.113.46",
    user_agent="test",
)
OTHER_IP = "203.0.113.201"  # адрес клиента, отличный от адреса входа в сессии стенда
AGENT = "t46-agent/1.0 " + "x" * 600  # длиннее 512: журнал хранит начало строки


class Recorder:
    """Подмена ``audit.record``: запоминает вызов и транзакцию, в которой он шёл."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        conn: Any,
        ctx: AuditContext,
        action: str,
        entity: tuple[str, str],
        old: Any,
        new: Any,
        result: str,
    ) -> None:
        xact = wrote = None
        if conn is not None:
            xact = await conn.fetchval("select pg_current_xact_id()::text")
            # Записала ли транзакция вызова строку истории: версия строки с ``xmin`` этой
            # транзакции. Запись журнала в отдельной транзакции дала бы здесь ``False``.
            wrote = await conn.fetchval(
                "select exists(select 1 from billing_group_multipliers where xmin::text = $1) "
                "or exists(select 1 from node_billing_assignments where xmin::text = $1)",
                str(int(xact) % 2**32),
            )
        self.calls.append(
            {
                "xact": xact,
                "wrote_history": wrote,
                "ctx": ctx,
                "action": action,
                "entity": entity,
                "old": old,
                "new": new,
                "result": result,
            }
        )


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recording = Recorder()
    monkeypatch.setattr(audit, "record", recording)
    return recording


def handle(sid: str) -> str:
    """Несекретная метка сессии, выписанная независимо от ``Session.handle``."""
    return hashlib.sha256(sid.encode()).hexdigest()[:32]


async def test_tc_e2e_01_login_without_a_second_factor_is_not_finished(
    pg_dsn: str, redis_url: str
) -> None:
    """TC-E2E-01: пароль без второго фактора — ``202 totp_required`` и никакой сессии; второй
    фактор и резервный код — 501 до 001.47; тела проверяются раньше (422)."""
    async with auth_stand(pg_dsn, redis_url) as stand, stand.client() as client:
        body = {"email": "admin@example.test", "password": "долгий-пароль"}
        answer = await client.post("/api/v1/admin/auth/login", json=body)
        assert answer.status_code == 202, answer.text
        assert answer.json() == {"status": "totp_required"}
        assert "sid" not in answer.cookies, "сессия выдаётся только после второго фактора"
        for bad in ({"email": "admin@example.test"}, {**body, "otp": "123456"}):
            refused = await client.post("/api/v1/admin/auth/login", json=bad)
            assert refused.status_code == 422, (bad, refused.text)
        assert (
            await client.post("/api/v1/admin/auth/totp", json={"code": "12345"})
        ).status_code == 422, "код TOTP — шесть цифр"
        for path, payload, operation in (
            ("/api/v1/admin/auth/totp", {"code": "123456"}, "admin.auth.totp"),
            ("/api/v1/admin/auth/recovery", {"code": "abcd-efgh"}, "admin.auth.recovery"),
        ):
            stub = await client.post(path, json=payload)
            assert stub.status_code == 501, (path, stub.text)
            assert stub.json()["error"]["details"] == {"operation": operation}
        anonymous = await client.get("/api/v1/admin/auth/session")
        assert anonymous.status_code == 401, "своя сессия — только под сессией"


async def test_logout_needs_a_session_and_csrf(pg_dsn: str, redis_url: str) -> None:
    """Выход — под сессией и CSRF (как выход пользователя); сам выход — 501 до 001.47."""
    async with auth_stand(pg_dsn, redis_url) as stand, stand.client() as client:
        assert (await client.post("/api/v1/admin/auth/logout")).status_code == 401
    async with admin_stand(pg_dsn, redis_url, role="support") as admin:
        forged = await admin.client.post("/api/v1/admin/auth/logout")
        assert forged.status_code == 403, forged.text
        assert forged.json()["error"]["code"] == "csrf_failed"
        stub = await admin.client.post("/api/v1/admin/auth/logout", headers=admin.headers)
        assert stub.status_code == 501, stub.text


async def test_tc_e2e_02_a_role_without_the_permission_is_refused_and_logged(
    pg_dsn: str, redis_url: str, recorder: Recorder
) -> None:
    """TC-E2E-02: роль ``support`` вызывает ``DELETE /admin/plans/{id}`` — 403
    ``permission_denied`` с именем разрешения; ``audit.record`` вызвана с ``denied`` в своей
    транзакции, и та зафиксирована. Контекст — из этого запроса: адрес клиента (не адрес входа
    из сессии), его ``User-Agent`` (до 512 символов), несекретная метка сессии вместо значения
    cookie. Сущность — метод и шаблон маршрута; фактический путь — в ``new``, усечённый до 256
    символов."""
    async with admin_stand(pg_dsn, redis_url, role="support") as admin:
        sid = admin.client.cookies["sid"]
        async with admin.stand.client(OTHER_IP) as client:
            client.cookies.set("sid", sid)
            plan_id = uuid.uuid4()
            refused = await client.delete(
                f"/api/v1/admin/plans/{plan_id}",
                headers={**admin.headers, "User-Agent": AGENT},
            )
            assert refused.status_code == 403, refused.text
            assert refused.json()["error"]["code"] == "permission_denied"
            assert refused.json()["error"]["details"] == {"permission": "plans.write"}
            (call,) = recorder.calls
            assert call["result"] == "denied" and call["action"] == "plans.write"
            assert call["entity"] == ("operation", "DELETE /api/v1/admin/plans/{plan_id}")
            assert (call["old"], call["new"]) == (None, {"path": f"/api/v1/admin/plans/{plan_id}"})
            ctx = call["ctx"]
            assert (ctx.actor_type, ctx.actor_id, ctx.actor_role) == (
                "admin",
                admin.admin_id,
                "support",
            )
            assert (ctx.ip, ctx.user_agent) == (OTHER_IP, AGENT[:512])
            assert ctx.session_id == handle(sid) and sid not in ctx.session_id
            assert ctx.impersonated_user_id is None
            status = await admin.stand.pool.fetchval(
                "select pg_xact_status($1::text::xid8)", call["xact"]
            )
            assert status == "committed", "отказ записан зафиксированной транзакцией"

            long_id = "x" * 600
            await client.delete(f"/api/v1/admin/plans/{long_id}", headers=admin.headers)
            long_call = recorder.calls[-1]
            assert long_call["entity"] == ("operation", "DELETE /api/v1/admin/plans/{plan_id}")
            assert len(long_call["new"]["path"]) == 256
        reading = await admin.client.get("/api/v1/admin/plans")
        assert reading.status_code == 403, (
            "у support в заглушечной матрице нет ни одного разрешения"
        )
        assert reading.json()["error"]["details"] == {"permission": "plans.read"}


async def test_csrf_is_checked_before_the_permission(
    pg_dsn: str, redis_url: str, recorder: Recorder
) -> None:
    """Каждая изменяющая операция панели (``POST``, ``PATCH``, ``DELETE``) без маркера CSRF
    отклоняется как ``csrf_failed`` до проверки разрешения: поддельный запрос не оставляет в
    журнале отказа от имени жертвы."""
    mutating = [op for op in ADMIN_OPERATIONS if op[0] != "GET"]
    assert {method for method, _, _ in mutating} == {"POST", "PATCH", "DELETE"}
    async with admin_stand(pg_dsn, redis_url, role="support") as admin:
        for method, path, body in mutating:
            forged = await admin.client.request(method, path, json=body)
            assert forged.status_code == 403, (method, path, forged.text)
            assert forged.json()["error"]["code"] == "csrf_failed", (method, path)
        assert recorder.calls == [], "поддельный запрос в журнал не попадает"


async def test_every_panel_operation_checks_its_declared_permission(
    pg_dsn: str, redis_url: str, app_client: httpx.AsyncClient
) -> None:
    """Проверка разрешения действует на каждой операции панели, а не на одном разделе: у
    ``support`` в заглушечной матрице нет ни одного разрешения, и каждая операция
    ``ADMIN_OPERATIONS`` отвечает 403 ``permission_denied`` с объявленным ею именем."""
    paths = (await app_client.get("/openapi.json")).json()["paths"]
    async with admin_stand(pg_dsn, redis_url, role="support") as admin:
        for method, path, body in ADMIN_OPERATIONS:
            declared = paths[openapi_path(path)][method.lower()]["x-permission"]
            answer = await admin.client.request(method, path, json=body, headers=admin.headers)
            assert answer.status_code == 403, (method, path, answer.text)
            error = answer.json()["error"]
            assert (error["code"], error["details"]) == (
                "permission_denied",
                {"permission": declared},
            ), (method, path)


async def test_tc_e2e_03_the_session_shows_role_permissions_and_language(
    pg_dsn: str, redis_url: str
) -> None:
    """TC-E2E-03: ``GET /admin/auth/session`` — роль и язык панели из ``admin_users`` (стенд
    пишет ``ru``, умолчание колонки — ``en``) и разрешения роли по реестру: у ``support`` в
    заглушке их нет, у ``super_admin`` — все."""
    async with admin_stand(pg_dsn, redis_url, role="support") as admin:
        answer = await admin.client.get("/api/v1/admin/auth/session")
        assert answer.status_code == 200, answer.text
        assert answer.json() == {"role": "support", "permissions": [], "language": "ru"}
    async with admin_stand(pg_dsn, redis_url) as admin:
        answer = await admin.client.get("/api/v1/admin/auth/session")
        assert answer.json() == {
            "role": "super_admin",
            "permissions": sorted(PERMISSIONS),
            "language": "ru",
        }


async def test_tc_e2e_04_accepted_routes_declare_the_dedicated_permissions(
    app_client: httpx.AsyncClient,
) -> None:
    """TC-E2E-04, первая половина: экспорт партии кодов — ``codes.export``, отзыв identity —
    ``nodes.revoke_identity``, ручной статус ноды — ``nodes.manual_status`` (В-39)."""
    paths = (await app_client.get("/openapi.json")).json()["paths"]
    assert paths["/api/v1/admin/codes/batch/{batch_id}/export"]["get"]["x-permission"] == (
        "codes.export"
    )
    assert paths["/api/v1/admin/nodes/{node_id}/revoke-identity"]["post"]["x-permission"] == (
        "nodes.revoke_identity"
    )
    assert paths["/api/v1/admin/nodes/{node_id}/status"]["post"]["x-permission"] == (
        "nodes.manual_status"
    )


async def test_tc_e2e_04_history_writers_log_old_and_new_in_their_transaction(
    pg_dsn: str, recorder: Recorder
) -> None:
    """TC-E2E-04, вторая половина: ``set_group_multiplier`` и ``set_billing_group`` вызывают
    ``audit.record`` с прежним и новым значением (§4.16, UC-09 шаг 7, R-23) в транзакции, которая
    записала строку истории (в ней есть версия строки с её ``xmin``), а не в отдельной: изменение и
    его след атомарны. Первое назначение несёт переопределение — прежнее значение второй записи
    берётся из закрытой строки, а не подставляется. Назначение пишет и действующий коэффициент
    ноды: переопределение, иначе коэффициент группы в момент смены, иначе 1.0 (у ``other``
    коэффициента нет). Коэффициент группы меняется, пока нода в ней (3.0 → 4.0): прежнее значение
    последней записи — 4.0 на момент смены, а не 3.0 на начало назначения."""
    async with catalog(pg_dsn) as conn:
        group = await billing_group(conn, "t46")
        other = await billing_group(conn, "t46")
        node_id = await node(conn, group, 46)
        pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)
        assert pool is not None
        try:
            service = GroupService(pool, RecordingHours())
            await service.set_group_multiplier(group, 2000, ctx=CTX)
            await service.set_group_multiplier(group, 3000, ctx=CTX)
            await service.set_billing_group(node_id, group, 1200, ctx=CTX)
            await service.set_billing_group(node_id, other, 1500, ctx=CTX)
            await service.set_billing_group(node_id, group, ctx=CTX)
            await service.set_group_multiplier(group, 4000, ctx=CTX)
            await service.set_billing_group(node_id, other, ctx=CTX)
        finally:
            await pool.close()
    assert [(c["action"], c["entity"], c["old"], c["new"]) for c in recorder.calls] == [
        ("billing_groups.multiplier", ("billing_group", str(group)), None, {"multiplier": "2.0"}),
        (
            "billing_groups.multiplier",
            ("billing_group", str(group)),
            {"multiplier": "2.0"},
            {"multiplier": "3.0"},
        ),
        (
            "nodes.billing_group",
            ("node", str(node_id)),
            None,
            {"billing_group_id": str(group), "multiplier_override": "1.2", "multiplier": "1.2"},
        ),
        (
            "nodes.billing_group",
            ("node", str(node_id)),
            {"billing_group_id": str(group), "multiplier_override": "1.2", "multiplier": "1.2"},
            {"billing_group_id": str(other), "multiplier_override": "1.5", "multiplier": "1.5"},
        ),
        (
            "nodes.billing_group",
            ("node", str(node_id)),
            {"billing_group_id": str(other), "multiplier_override": "1.5", "multiplier": "1.5"},
            {"billing_group_id": str(group), "multiplier_override": None, "multiplier": "3.0"},
        ),
        (
            "billing_groups.multiplier",
            ("billing_group", str(group)),
            {"multiplier": "3.0"},
            {"multiplier": "4.0"},
        ),
        (
            "nodes.billing_group",
            ("node", str(node_id)),
            {"billing_group_id": str(group), "multiplier_override": None, "multiplier": "4.0"},
            {"billing_group_id": str(other), "multiplier_override": None, "multiplier": "1.0"},
        ),
    ]
    assert [c["wrote_history"] for c in recorder.calls] == [True] * 7, (
        "журнал — в транзакции, записавшей строку истории"
    )
    assert all(c["ctx"] is CTX and c["result"] == "success" for c in recorder.calls)


async def test_routes_hand_the_session_context_to_the_journal(
    pg_dsn: str, redis_url: str, recorder: Recorder
) -> None:
    """Маршрут смены коэффициента и экспорт партии кодов передают в журнал контекст своей
    сессии: актор, роль, метка сессии и адрес — из запроса, а не подставленные."""
    async with catalog(pg_dsn) as conn, admin_stand(pg_dsn, redis_url) as admin:
        group = await billing_group(conn, "t46")
        changed = await admin.client.post(
            f"/api/v1/admin/groups/billing/{group}/multiplier",
            json={"multiplier": "2.0"},
            headers=admin.headers,
        )
        assert changed.status_code == 201, changed.text
        exported = await admin.client.get(f"/api/v1/admin/codes/batch/{BATCH_ID}/export")
        assert exported.status_code == 200, exported.text
        sid = admin.client.cookies["sid"]
    assert [(c["action"], c["entity"]) for c in recorder.calls] == [
        ("billing_groups.multiplier", ("billing_group", str(group))),
        ("codes.export", ("code_batch", BATCH_ID)),
    ]
    assert recorder.calls[1]["new"] == {"count": 3}
    for call in recorder.calls:
        ctx = call["ctx"]
        assert (ctx.actor_id, ctx.actor_role, ctx.session_id, ctx.ip) == (
            admin.admin_id,
            "super_admin",
            handle(sid),
            "203.0.113.200",
        )


async def test_the_journal_stub_writes_nothing(pg_dsn: str, redis_url: str) -> None:
    """Критерий приёмки «заглушки не пишут в базу» — без подмены: настоящая заглушка
    ``audit.record`` на отказе по разрешению и на смене коэффициента не добавляет строк
    ``audit_log`` (запись — 001.48)."""
    async with catalog(pg_dsn) as conn:
        group = await billing_group(conn, "t46")
        before = await conn.fetchval("select count(*) from audit_log")
        async with admin_stand(pg_dsn, redis_url, role="support") as support:
            denied = await support.client.delete(
                f"/api/v1/admin/plans/{uuid.uuid4()}", headers=support.headers
            )
            assert denied.status_code == 403, denied.text
        async with admin_stand(pg_dsn, redis_url) as admin:
            changed = await admin.client.post(
                f"/api/v1/admin/groups/billing/{group}/multiplier",
                json={"multiplier": "2.0"},
                headers=admin.headers,
            )
            assert changed.status_code == 201, changed.text
        assert await conn.fetchval("select count(*) from audit_log") == before


async def test_the_role_and_status_are_read_on_every_request(pg_dsn: str, redis_url: str) -> None:
    """Решение заказчика 2026-11-04: роль и статус — из ``admin_users`` на каждом запросе.
    Понижение роли — 403 со следующего запроса той же сессии; блокировка и удалённая строка —
    401, и отзываются все сессии администратора, а не только предъявленная: разблокировка их не
    оживляет."""
    async with admin_stand(pg_dsn, redis_url) as admin:
        pool, store = admin.stand.pool, SessionStore(admin.stand.redis)
        second = await store.create("admin", str(admin.admin_id), "203.0.113.202", "test", 600)
        assert (await admin.client.get("/api/v1/admin/plans")).status_code == 200
        await pool.execute("update admin_users set role = 'support' where id = $1", admin.admin_id)
        demoted = await admin.client.get("/api/v1/admin/plans")
        assert demoted.status_code == 403, demoted.text
        assert demoted.json()["error"]["code"] == "permission_denied"
        await pool.execute(
            "update admin_users set role = 'super_admin', status = 'blocked' where id = $1",
            admin.admin_id,
        )
        sid = admin.client.cookies["sid"]
        blocked = await admin.client.get("/api/v1/admin/plans")
        assert blocked.status_code == 401, blocked.text
        assert blocked.json()["error"]["code"] == "unauthenticated"
        assert await store.get(sid) is None, "сессия заблокированного отозвана"
        assert await store.get(second.id) is None, "и вторая его сессия, не предъявленная"
        await pool.execute("update admin_users set status = 'active' where id = $1", admin.admin_id)
        assert (await admin.client.get("/api/v1/admin/plans")).status_code == 401, (
            "разблокировка прежнюю сессию не оживляет"
        )
        fresh = await store.create("admin", str(admin.admin_id), "203.0.113.200", "test", 600)
        spare = await store.create("admin", str(admin.admin_id), "203.0.113.202", "test", 600)
        admin.client.cookies.set("sid", fresh.id)
        assert (await admin.client.get("/api/v1/admin/plans")).status_code == 200
        await pool.execute("delete from admin_users where id = $1", admin.admin_id)
        gone = await admin.client.get("/api/v1/admin/auth/session")
        assert gone.status_code == 401, "сессия без строки администратора не открывает панель"
        assert await store.get(fresh.id) is None
        assert await store.get(spare.id) is None, "отозваны все сессии удалённого"
