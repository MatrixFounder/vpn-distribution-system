"""CSRF (security.md §7.3): cookie ``SameSite=Lax`` плюс заголовок ``X-CSRF-Token`` для
изменяющих методов. Задача 001.12 — интерфейс зависимости; проверка токена — 001.14."""

from __future__ import annotations

from fastapi import Request

CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def require_csrf(request: Request) -> None:
    """Зависимость FastAPI для изменяющих методов: сверка ``X-CSRF-Token`` с сессией — 001.14.
    Безопасные методы проходят без проверки."""
    if request.method in SAFE_METHODS:
        return
    raise NotImplementedError("require_csrf — задача 001.14")
