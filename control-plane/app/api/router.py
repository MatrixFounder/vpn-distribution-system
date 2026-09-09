"""Внешний API ``/api/v1`` (interfaces.md §5.1): подроутеры ``auth`` (``api/auth.py``, 001.13),
``me``, ``admin``.

Задача 001.10 регистрирует заглушки 501 (``not_implemented``); STUB-задачи заменяют их
фиксированными ответами, LOGIC-задачи — реализацией. Новые роутеры подключаются здесь.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.auth import router as auth
from app.errors import not_implemented
from app.security.deps import redis_required

router = APIRouter(prefix="/api/v1")

me = APIRouter(prefix="/me", tags=["me"])
admin = APIRouter(prefix="/admin", tags=["admin"])


@me.get("", summary="Профиль пользователя (001.16)")
async def profile() -> dict[str, str]:
    raise not_implemented("me.profile")


@admin.get("/dashboard", summary="Сводка администратора (001.44)")
async def dashboard() -> dict[str, str]:
    raise not_implemented("admin.dashboard")


# Раздел /auth — операции с лимитом частоты (§5.12): без Redis отвечают 503 (fail-closed, §9.1).
router.include_router(auth, dependencies=[Depends(redis_required)])
router.include_router(me)
router.include_router(admin)
