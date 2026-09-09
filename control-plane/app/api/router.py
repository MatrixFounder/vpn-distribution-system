"""Внешний API ``/api/v1`` (interfaces.md §5.1): подроутеры ``auth`` (``api/auth.py``, 001.13),
``me`` (``api/me.py``, 001.15), ``admin``.

Задача 001.10 регистрирует заглушки 501 (``not_implemented``); STUB-задачи заменяют их
фиксированными ответами, LOGIC-задачи — реализацией. Новые роутеры подключаются здесь.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.auth import router as auth
from app.api.me import router as me
from app.errors import not_implemented
from app.security.deps import redis_required

router = APIRouter(prefix="/api/v1")

admin = APIRouter(prefix="/admin", tags=["admin"])


@admin.get("/dashboard", summary="Сводка администратора (001.44)")
async def dashboard() -> dict[str, str]:
    raise not_implemented("admin.dashboard")


# Разделы /auth (лимиты частоты §5.12) и /me (сессии §7.1) живут в Redis: без него отвечают 503
# (fail-closed, §9.1), проверка — до валидации тела и до поиска сессии.
router.include_router(auth, dependencies=[Depends(redis_required)])
router.include_router(me, dependencies=[Depends(redis_required)])
router.include_router(admin)
