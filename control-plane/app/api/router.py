"""Внешний API ``/api/v1`` (interfaces.md §5.1): подроутеры ``auth``, ``me``, ``admin``.

Задача 001.10 регистрирует заглушки 501 (``not_implemented``); STUB-задачи 001.13+ заменяют их
фиксированными ответами, LOGIC-задачи — реализацией. Новые роутеры подключаются здесь.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.errors import not_implemented

router = APIRouter(prefix="/api/v1")

auth = APIRouter(prefix="/auth", tags=["auth"])
me = APIRouter(prefix="/me", tags=["me"])
admin = APIRouter(prefix="/admin", tags=["admin"])


@auth.post("/login", status_code=200, summary="Вход пользователя (001.13)")
async def login() -> dict[str, str]:
    raise not_implemented("auth.login")


@me.get("", summary="Профиль пользователя (001.16)")
async def profile() -> dict[str, str]:
    raise not_implemented("me.profile")


@admin.get("/dashboard", summary="Сводка администратора (001.44)")
async def dashboard() -> dict[str, str]:
    raise not_implemented("admin.dashboard")


router.include_router(auth)
router.include_router(me)
router.include_router(admin)
