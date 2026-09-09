"""Зависимости FastAPI для обработчиков: текущий пользователь, текущий администратор, проверка
разрешения (R-35), fail-closed при недоступном Redis (§9.1). Задача 001.12 — интерфейс;
``redis_required`` действует с 001.12 (вход, восстановление, активация кодов и подписка отвечают
503 без Redis); ``current_user`` — с 001.15 (сессия пользователя по cookie ``sid`` §7.1,
хранилище 001.14); ``current_admin`` — с 001.18 в минимальном виде (сессия вида ``admin`` по
той же cookie; подтверждённый второй фактор и роль — 001.47); ``require_permission`` —
``NotImplementedError`` до 001.46, до него операции панели объявляют разрешение в OpenAPI
(``x-permission``) и защищены только сессией администратора."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Request

from app.errors import ApiError
from app.redis import get_redis
from app.security.ratelimit import RateLimiter
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
    """Аутентифицированный администратор: идентификатор ``admin_users.id`` и его сессия."""

    id: uuid.UUID
    session: Session


async def current_admin(request: Request) -> CurrentAdmin:
    """Администратор по сессионной cookie ``sid``: сессия вида ``admin`` с UUID-субъектом, иначе
    401 ``unauthenticated`` (сессия пользователя панель не открывает). Минимальный вид 001.18:
    сессии администраторов выдаёт 001.47 (пароль + TOTP), там же — проверка подтверждённого
    второго фактора; роль и матрица разрешений — 001.46."""
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        raise unauthenticated()
    session = await SessionStore(await get_redis()).get(sid)
    if session is None or session.kind != "admin":
        raise unauthenticated()
    try:
        admin_id = uuid.UUID(session.subject_id)
    except ValueError as exc:
        raise unauthenticated() from exc
    return CurrentAdmin(id=admin_id, session=session)


def require_permission(name: str) -> Callable[[Request], Awaitable[None]]:
    """Зависимость «операция требует разрешение ``name``» (матрица роль × операция — 001.46)."""

    async def dependency(request: Request) -> None:
        raise NotImplementedError(f"require_permission({name!r}) — задача 001.46")

    return dependency
