"""Зависимости FastAPI для обработчиков: текущий пользователь, текущий администратор, проверка
разрешения (R-35), fail-closed при недоступном Redis (§9.1). Задача 001.12 — интерфейс;
``redis_required`` действует с 001.12 (вход, восстановление, активация кодов и подписка отвечают
503 без Redis); ``current_user`` — с 001.15 (сессия пользователя по cookie ``sid`` §7.1,
хранилище 001.14); администратор и разрешения — ``NotImplementedError`` до 001.46/001.47."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

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


async def current_admin(request: Request) -> Any:
    """Администратор по сессии с подтверждённым вторым фактором — 001.47."""
    raise NotImplementedError("current_admin — задача 001.47")


def require_permission(name: str) -> Callable[[Request], Awaitable[None]]:
    """Зависимость «операция требует разрешение ``name``» (матрица роль × операция — 001.46)."""

    async def dependency(request: Request) -> None:
        raise NotImplementedError(f"require_permission({name!r}) — задача 001.46")

    return dependency
