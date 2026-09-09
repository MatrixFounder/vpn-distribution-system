"""Зависимости FastAPI для обработчиков: текущий пользователь, текущий администратор, проверка
разрешения (R-35), fail-closed при недоступном Redis (§9.1). Задача 001.12 — интерфейс: субъекты и
разрешения поднимают ``NotImplementedError`` до 001.14/001.46; ``redis_required`` действует уже
сейчас — заглушки входа, восстановления, активации кодов и подписки отвечают 503, когда Redis
недоступен (TC-E2E-01)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request

from app.redis import get_redis
from app.security.ratelimit import RateLimiter


async def redis_required() -> None:
    """Fail-closed (§9.1): без Redis эндпоинты с лимитом частоты не обслуживаются —
    ``RateLimitUnavailable`` переводится обработчиком ошибок в 503 с ``Retry-After``."""
    await RateLimiter(await get_redis()).ensure_available()


async def current_user(request: Request) -> Any:
    """Пользователь по сессионной cookie — 001.14."""
    raise NotImplementedError("current_user — задача 001.14")


async def current_admin(request: Request) -> Any:
    """Администратор по сессии с подтверждённым вторым фактором — 001.47."""
    raise NotImplementedError("current_admin — задача 001.47")


def require_permission(name: str) -> Callable[[Request], Awaitable[None]]:
    """Зависимость «операция требует разрешение ``name``» (матрица роль × операция — 001.46)."""

    async def dependency(request: Request) -> None:
        raise NotImplementedError(f"require_permission({name!r}) — задача 001.46")

    return dependency
