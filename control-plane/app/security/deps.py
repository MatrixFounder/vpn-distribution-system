"""Зависимости FastAPI для обработчиков: текущий пользователь, текущий администратор, проверка
разрешения (R-35), fail-closed при недоступном Redis (§9.1). Задача 001.12 — интерфейс;
``redis_required`` действует с 001.12 (вход, восстановление, активация кодов и подписка отвечают
503 без Redis); ``current_user`` — с 001.15 (сессия пользователя по cookie ``sid`` §7.1,
хранилище 001.14); ``current_admin`` — с 001.18 (сессия вида ``admin`` по той же cookie;
подтверждённый второй фактор — 001.47).

Задача 001.46: роль, статус и язык панели ``current_admin`` читает из ``admin_users`` на каждом
запросе — понижение роли и блокировка действуют со следующего запроса без перевыпуска сессий
(решение заказчика 2026-11-04); ``require_permission(name)`` проверяет роль по реестру
``app.security.rbac.PERMISSIONS`` и пишет отказ в журнал (``audit.record``, ``denied``);
``declared_permission`` — та же проверка для имени, которое маршрут панели объявил в OpenAPI
(``x-permission``): объявленное и проверяемое не расходятся."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from app.db.pool import get_pool, transaction
from app.errors import ApiError
from app.redis import get_redis
from app.security import audit
from app.security.csrf import check_csrf
from app.security.ratelimit import RateLimiter
from app.security.rbac import PERMISSIONS
from app.security.sessions import SESSION_COOKIE, Session, SessionStore


async def redis_required() -> None:
    """Fail-closed (§9.1): без Redis эндпоинты с лимитом частоты не обслуживаются —
    ``RateLimitUnavailable`` переводится обработчиком ошибок в 503 с ``Retry-After``."""
    await RateLimiter(await get_redis()).ensure_available()


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """Аутентифицированный пользователь кабинета: идентификатор и его сессия."""

    id: uuid.UUID
    session: Session


def unauthenticated() -> ApiError:
    return ApiError("unauthenticated", "требуется вход", status=401)


async def current_user(request: Request) -> CurrentUser:
    """Пользователь по сессионной cookie ``sid`` (§7.1): сессии нет, она истекла, отозвана или
    принадлежит администратору — 401 ``unauthenticated``. Чтение продлевает TTL бездействия.
    Без Redis сессию проверить нельзя — роутеры под сессией подключают ``redis_required``
    (fail-closed §9.1); отказ Redis уже посреди запроса здесь не перехватывается — его переводит
    в 503 обработчик ошибок приложения (``errors.py``, вторая линия)."""
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        raise unauthenticated()
    session = await SessionStore(await get_redis()).get(sid)
    if session is None or session.kind != "user":
        raise unauthenticated()
    try:
        user_id = uuid.UUID(session.subject_id)
    except ValueError as exc:  # повреждённая запись — не сессия пользователя
        raise unauthenticated() from exc
    return CurrentUser(id=user_id, session=session)


@dataclass(frozen=True, slots=True)
class CurrentAdmin:
    """Аутентифицированный администратор: идентификатор ``admin_users.id``, его сессия, роль и
    язык панели — из ``admin_users`` на момент запроса."""

    id: uuid.UUID
    session: Session
    role: str
    language: str


async def current_admin(request: Request) -> CurrentAdmin:
    """Администратор по сессионной cookie ``sid``: сессия вида ``admin`` с UUID-субъектом, иначе
    401 ``unauthenticated`` (сессия пользователя панель не открывает). Роль, статус и язык —
    строка ``admin_users``: администратора нет или его статус не ``active`` — тоже 401, и
    отзываются все его сессии, а не только предъявленная: иначе каждый запрос по любой из них
    продлевал бы её (скользящий TTL), и после разблокировки украденная сессия снова открыла бы
    панель без пароля и TOTP. Подтверждённый второй фактор сессии — 001.47."""
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        raise unauthenticated()
    store = SessionStore(await get_redis())
    session = await store.get(sid)
    if session is None or session.kind != "admin":
        raise unauthenticated()
    try:
        admin_id = uuid.UUID(session.subject_id)
    except ValueError as exc:
        raise unauthenticated() from exc
    pool = await get_pool()
    row = await pool.fetchrow(
        "select role::text as role, status::text as status, language from admin_users "
        "where id = $1",
        admin_id,
    )
    if row is None or row["status"] != "active":
        await store.revoke_all(session.subject_id)
        raise unauthenticated()
    return CurrentAdmin(id=admin_id, session=session, role=row["role"], language=row["language"])


Admin = Annotated[CurrentAdmin, Depends(current_admin)]


def permission_denied(name: str | None) -> ApiError:
    """403 по разрешению: ``details.permission`` — имя, которого не хватило (панель различает
    отказ по правам и прочие 403, например ``csrf_failed``)."""
    return ApiError(
        "permission_denied",
        "недостаточно прав для операции",
        status=403,
        details={"permission": name},
    )


# Длины, до которых журнал усекает данные клиента: ``User-Agent`` — как запись сессии
# (``SessionStore.create``), путь запроса — по шаблону маршрута он не нужен целиком.
USER_AGENT_MAX = 512
PATH_MAX = 256


def audit_context(request: Request, admin: CurrentAdmin) -> audit.AuditContext:
    """Колонки актора журнала из запроса: адрес — клиента этого запроса (uvicorn берёт его из
    ``X-Forwarded-For``, который nginx перезаписывает, — как ``app.api.auth.client_ip``), а не
    адрес входа из сессии: украденная сессия, предъявленная из другой сети, должна быть видна в
    журнале. Сессия — несекретной меткой ``Session.handle``, не значением cookie."""
    return audit.AuditContext(
        actor_type="admin",
        actor_id=admin.id,
        actor_role=admin.role,
        session_id=admin.session.handle,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent", "")[:USER_AGENT_MAX],
    )


def _route_template(request: Request) -> str:
    """Шаблон пути совпавшего маршрута с префиксами разделов (``/api/v1/admin/plans/{plan_id}``).
    FastAPI 0.141 не разворачивает подключённые роутеры: в ``scope["route"]`` — маршрут
    подроутера с путём без префиксов, полный шаблон — в контексте маршрута
    (``scope["fastapi"]["effective_route_context"].path_format``). Поле внутреннее; без него —
    путь подроутера, а тест TC-E2E-02 закрепляет полный шаблон и заметит смену."""
    context = request.scope.get("fastapi", {}).get("effective_route_context")
    template = getattr(context, "path_format", None)
    if template:
        return str(template)
    return str(getattr(request.scope.get("route"), "path", ""))


async def _check(request: Request, admin: CurrentAdmin, name: str | None) -> None:
    """Роль администратора есть у разрешения ``name`` — иначе запись отказа в журнал отдельной
    короткой транзакцией и 403. Неизвестное имя и маршрут без объявления — отказ (fail-closed):
    операция, которой реестр не знает, никому не разрешена.

    У изменяющих запросов CSRF проверяется первым, по токену сессии ``admin`` (``check_csrf``):
    иначе поддельный запрос с чужой сессией без маркера оставлял бы в журнале отказ от имени
    жертвы (роаст 001.46). Проверка здесь, а не в одной из зависимостей, — её получают и роутер
    панели, и ``require_permission``."""
    check_csrf(request, admin.session)
    roles = PERMISSIONS.get(name) if name is not None else None
    if roles is not None and admin.role in roles:
        return
    # Сущность — метод и шаблон маршрута (ограничены набором маршрутов), фактический путь —
    # в ``new`` и усечённым: путь задаёт клиент, а журнал append-only (индекс по сущности не
    # должен ломаться на длинной строке, отказ — остаться записанным). Действие — имя
    # разрешения, как у остальных записей «раздел.действие».
    async with transaction(await get_pool()) as conn:
        await audit.record(
            conn,
            audit_context(request, admin),
            action=name if name is not None else "undeclared",
            entity=("operation", f"{request.method} {_route_template(request)}"),
            old=None,
            new={"path": request.url.path[:PATH_MAX]},
            result="denied",
        )
    raise permission_denied(name)


def require_permission(name: str) -> Callable[[Request, CurrentAdmin], Awaitable[CurrentAdmin]]:
    """Зависимость «операция требует разрешение ``name``». Имя проверяется по реестру при
    объявлении: опечатка в имени роняет импорт модуля, а не открывает операцию."""
    if name not in PERMISSIONS:
        raise ValueError(f"разрешение {name!r} не объявлено в app.security.rbac.PERMISSIONS")

    async def dependency(request: Request, admin: Admin) -> CurrentAdmin:
        await _check(request, admin, name)
        return admin

    return dependency


async def declared_permission(request: Request, admin: Admin) -> None:
    """Проверка разрешения, которое маршрут объявил в OpenAPI (``openapi_extra`` с ключом
    ``x-permission``, ``app.api.admin._common.permission``). Подключается к роутеру панели
    целиком: маршрут без объявления получает отказ, а не доступ любой роли."""
    route = request.scope.get("route")
    extra = getattr(route, "openapi_extra", None) or {}
    await _check(request, admin, extra.get("x-permission"))
