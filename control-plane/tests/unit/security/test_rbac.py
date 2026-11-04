"""Реестр разрешений и проверка по объявленному разрешению (задача 001.46; R-35): полный перечень
имён, заглушечная матрица «всё у ``super_admin``» и отказ по умолчанию — маршрут панели без
объявления или с именем вне реестра не открывается ни одной роли."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
import pytest
from app.api.admin._common import permission
from app.errors import ApiError, install_error_handlers
from app.security import audit, deps
from app.security.audit import AuditContext
from app.security.deps import CurrentAdmin, current_admin, declared_permission, require_permission
from app.security.rbac import PERMISSIONS, ROLES, permissions_of
from app.security.sessions import Session
from app.security.totp import Totp
from fastapi import APIRouter, Depends, FastAPI


def test_the_registry_is_the_full_list_of_permissions() -> None:
    """Перечень — литералом: выпавшее имя оставило бы маршрут 001.31, 001.48, 001.49, 001.85 или
    001.89 без разрешения в реестре, и проверка закрыла бы его для всех ролей."""
    assert set(PERMISSIONS) == {
        "dashboard.read",
        "plans.read",
        "plans.write",
        "groups.read",
        "groups.write",
        "codes.read",
        "codes.write",
        "codes.export",
        "nodes.read",
        "nodes.write",
        "nodes.revoke_identity",
        "nodes.manual_status",
        "nodes.update_agent",
        "nodes.restart_xray",
        "nodes.commands",
        "users.read",
        "users.write",
        "users.view_subscription_url",
        "users.impersonate",
        "approvals.approve",
        "settings.read",
        "settings.write",
        "audit.read",
        "admins.manage",
        "admins.self",
    }
    assert ROLES == ("super_admin", "admin", "operator", "support"), "перечисление admin_role"


def test_the_stub_matrix_gives_everything_to_super_admin_only() -> None:
    """Заглушка 001.46: все разрешения у ``super_admin``; матрица ролей security.md — 001.48."""
    assert all(roles == {"super_admin"} for roles in PERMISSIONS.values())
    assert permissions_of("super_admin") == sorted(PERMISSIONS)
    for role in ("admin", "operator", "support", "auditor"):
        assert permissions_of(role) == [], role


def _app(role: str, monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, list[dict[str, Any]]]:
    """Роутер с проверкой объявленного разрешения; администратор подменён, журнал отказа идёт
    в подставную транзакцию (база не нужна) и перехватывается: второй элемент — вызовы
    ``audit.record``."""

    @asynccontextmanager
    async def no_transaction(pool: Any) -> AsyncIterator[None]:
        yield None

    async def no_pool() -> None:
        return None

    calls: list[dict[str, Any]] = []

    async def recording(conn: Any, ctx: AuditContext, **kw: Any) -> None:
        calls.append(kw)

    monkeypatch.setattr(deps, "transaction", no_transaction)
    monkeypatch.setattr(deps, "get_pool", no_pool)
    monkeypatch.setattr(audit, "record", recording)
    router = APIRouter(dependencies=[Depends(declared_permission)])

    @router.get("/declared", openapi_extra={"x-permission": "plans.read"})
    async def declared() -> dict[str, str]:
        return {"ok": "declared"}

    @router.post("/declared", openapi_extra={"x-permission": "plans.write"})
    async def declared_write() -> dict[str, str]:
        return {"ok": "declared"}

    @router.get("/undeclared")
    async def undeclared() -> dict[str, str]:
        return {"ok": "undeclared"}

    @router.get("/unknown", openapi_extra={"x-permission": "plans.reed"})
    async def unknown() -> dict[str, str]:
        return {"ok": "unknown"}

    outside = APIRouter()

    @outside.get("/required", dependencies=[Depends(require_permission("plans.read"))])
    async def required() -> dict[str, str]:
        return {"ok": "required"}

    @outside.post("/required", dependencies=[Depends(require_permission("plans.write"))])
    async def required_write() -> dict[str, str]:
        return {"ok": "required"}

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    app.include_router(outside)

    async def as_admin() -> CurrentAdmin:
        return CurrentAdmin(
            id=uuid.uuid4(), session=cast(Session, _Session()), role=role, language="en"
        )

    app.dependency_overrides[current_admin] = as_admin
    return app, calls


class _Session:
    id = "sid"
    ip = "203.0.113.1"
    handle = "handle"
    csrf = "csrf-token"


@pytest.mark.parametrize(
    ("role", "path", "status", "permission"),
    [
        ("super_admin", "/declared", 200, None),
        ("support", "/declared", 403, "plans.read"),
        ("super_admin", "/undeclared", 403, None),
        ("super_admin", "/unknown", 403, "plans.reed"),
        ("super_admin", "/required", 200, None),
        ("support", "/required", 403, "plans.read"),
    ],
)
async def test_the_declared_permission_is_enforced_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, role: str, path: str, status: int, permission: str | None
) -> None:
    """Отказ пишется в журнал с ``denied``; действие — имя разрешения, у маршрута без
    объявления — ``undeclared``. Пропущенный запрос журнала отказа не пишет."""
    app, calls = _app(role, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        answer = await client.get(path)
    assert answer.status_code == status, answer.text
    if status == 403:
        assert answer.json()["error"]["code"] == "permission_denied"
        assert answer.json()["error"]["details"] == {"permission": permission}
        assert [(c["action"], c["result"]) for c in calls] == [
            (permission or "undeclared", "denied")
        ]
    else:
        assert calls == []


@pytest.mark.parametrize("path", ["/declared", "/required"])
async def test_csrf_comes_before_the_permission_on_both_checks(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Изменяющий запрос без маркера CSRF или с чужим — ``csrf_failed`` до разрешения и без
    записи в журнал — и на роутере панели, и у ``require_permission``, у роли без разрешения и у
    роли с ним (CSRF защищает прежде всего того, кому операция разрешена). С маркером сессии
    проверка доходит до разрешения: ``support`` получает отказ, ``super_admin`` — доступ."""
    app, calls = _app("support", monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        for headers in ({}, {"X-CSRF-Token": "other-token"}):
            forged = await client.post(path, headers=headers)
            assert forged.status_code == 403, forged.text
            assert forged.json()["error"]["code"] == "csrf_failed"
        assert calls == [], "поддельный запрос в журнал не попадает"
        honest = await client.post(path, headers={"X-CSRF-Token": "csrf-token"})
        assert honest.json()["error"]["details"] == {"permission": "plans.write"}
        assert [c["action"] for c in calls] == ["plans.write"]
    app, calls = _app("super_admin", monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        for headers in ({}, {"X-CSRF-Token": "other-token"}):
            forged = await client.post(path, headers=headers)
            assert forged.status_code == 403, forged.text
            assert forged.json()["error"]["code"] == "csrf_failed"
        allowed = await client.post(path, headers={"X-CSRF-Token": "csrf-token"})
        assert allowed.status_code == 200, allowed.text
        assert calls == [], "разрешённому запросу журнал отказа не пишется"


def test_an_unknown_name_fails_the_declaration() -> None:
    """``permission(...)`` в ``openapi_extra`` и ``require_permission(...)`` отвергают имя вне
    реестра при объявлении: опечатка роняет импорт модуля маршрутов, а не закрывает операцию."""
    assert permission("plans.read") == {"x-permission": "plans.read"}
    for declare in (permission, require_permission):
        with pytest.raises(ValueError, match="plans.reed"):
            declare("plans.reed")


def test_a_system_actor_needs_no_session() -> None:
    """У системного актора (планировщик, команда хоста) нет ни актора, ни сессии, ни адреса —
    контекст собирается из одного вида актора."""
    ctx = AuditContext(actor_type="system")
    assert (ctx.actor_id, ctx.actor_role, ctx.session_id, ctx.ip, ctx.user_agent) == (
        None,
        None,
        None,
        None,
        None,
    )


def test_the_session_handle_does_not_carry_the_cookie() -> None:
    """Метка сессии для журнала — SHA-256 идентификатора (32 hex): по ней сессию не предъявить."""
    sid = "s3cr3t-cookie-value-0123456789abcdefghij"
    session = Session(
        id=sid,
        kind="admin",
        subject_id=str(uuid.uuid4()),
        ip="203.0.113.1",
        ua="ua",
        created_at=dt.datetime(2026, 11, 4, tzinfo=dt.UTC),
        ttl=600,
    )
    assert session.handle == hashlib.sha256(sid.encode()).hexdigest()[:32]
    assert sid not in session.handle and len(session.handle) == 32


class _NoPool:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"обращение к базе: {name}")


@pytest.mark.parametrize(
    ("operation", "args"),
    [("provision", ()), ("verify", ("123456",)), ("recovery_codes", ())],
)
async def test_totp_stubs_answer_501_without_the_database(
    operation: str, args: tuple[Any, ...]
) -> None:
    """Заглушки TOTP (001.47 — логика) отвечают 501 с именем операции и в базу не ходят."""
    with pytest.raises(ApiError) as refused:
        await getattr(Totp(_NoPool()), operation)(uuid.uuid4(), *args)
    assert (refused.value.status, refused.value.details) == (
        501,
        {"operation": f"admin.totp.{operation}"},
    )
