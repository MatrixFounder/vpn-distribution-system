"""Внешний API ``/api/v1`` (interfaces.md §5.1): подроутеры ``auth`` (``api/auth.py``, 001.13),
``me`` (``api/me.py``, 001.15), ``admin`` (``api/admin/``, 001.18).

Задача 001.10 регистрирует заглушки 501 (``not_implemented``); STUB-задачи заменяют их
фиксированными ответами, LOGIC-задачи — реализацией. Новые роутеры подключаются здесь.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.admin import router as admin
from app.api.auth import router as auth
from app.api.me import router as me
from app.security.deps import redis_required

router = APIRouter(prefix="/api/v1")

# Разделы /auth (лимиты частоты §5.12), /me и /admin (сессии §7.1) живут в Redis: без него
# отвечают 503 (fail-closed, §9.1), проверка — до валидации тела и до поиска сессии.
router.include_router(auth, dependencies=[Depends(redis_required)])
router.include_router(me, dependencies=[Depends(redis_required)])
router.include_router(admin, dependencies=[Depends(redis_required)])
