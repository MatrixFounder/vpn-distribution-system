"""Раздел ``/api/v1/auth`` (interfaces.md §5.1; UC-02 шаги 1–6, UC-15): регистрация, подтверждение
адреса, вход, выход, «выход везде», запрос и подтверждение восстановления пароля.

Задача 001.13 — маршруты, схемы и фиксированные ответы: ``UserService`` — заглушка 001.13,
логика — 001.14. Вход выставляет cookie сессии с атрибутами §7.1 (значение — случайное,
сессии за ним до 001.14 нет). Ответ ``reset-request`` не зависит от существования адреса (UC-15
A1). ``require_csrf`` и ``RateLimiter.check`` подключаются в 001.14 вместе с логикой: их заглушки
поднимают ``NotImplementedError``, а fail-closed уже действует через ``redis_required`` роутера.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.domain.users import UserService
from app.errors import ApiError
from app.security.sessions import USER_SESSION_TTL, clear_session_cookie, set_session_cookie

router = APIRouter(prefix="/auth", tags=["auth"])

# Границы полей (§9 постановки минимальной длины пароля не задаёт — решение 001.13, объявлено):
# адрес — форма «имя@домен» без пробелов и управляющих символов (NUL и переводы строки не должны
# доезжать до базы и конверта письма), без нормализации (§16.8 — 001.14); пароль 8…256 символов.
Email = Annotated[
    str,
    StringConstraints(
        min_length=3, max_length=254, pattern=r"^[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+$"
    ),
]
Password = Annotated[str, StringConstraints(min_length=8, max_length=256)]
Token = Annotated[str, StringConstraints(min_length=16, max_length=512)]


class StrictModel(BaseModel):
    """Тела запросов /auth: неизвестные поля отклоняются (422), а не проглатываются."""

    model_config = ConfigDict(extra="forbid")


class RegisterIn(StrictModel):
    email: Email
    password: Password
    aup_version: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    lang: Literal["en", "ru"] = "en"
    captcha_token: str | None = Field(default=None, max_length=4096)


class VerifyIn(StrictModel):
    token: Token


class LoginIn(StrictModel):
    email: Email
    password: Password
    captcha_token: str | None = Field(default=None, max_length=4096)


class ResetRequestIn(StrictModel):
    email: Email


class ResetConfirmIn(StrictModel):
    token: Token
    password: Password


class StatusOut(BaseModel):
    status: Literal["unconfirmed", "confirmed", "ok", "accepted", "password_changed"]


def get_user_service() -> UserService:
    """Зависимость: сервис пользователей (заглушка 001.13; 001.14 подключит пул)."""
    return UserService()


Users = Annotated[UserService, Depends(get_user_service)]


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=StatusOut,
    summary="Регистрация (UC-02, шаги 3–5)",
)
async def register(body: RegisterIn, users: Users) -> StatusOut:
    await users.register(body.email, body.password, body.aup_version, body.lang)
    return StatusOut(status="unconfirmed")


@router.post("/verify", response_model=StatusOut, summary="Подтверждение адреса (UC-02, шаг 6)")
async def verify(body: VerifyIn, users: Users) -> StatusOut:
    await users.verify_email(body.token)
    return StatusOut(status="confirmed")


@router.post("/login", response_model=StatusOut, summary="Вход пользователя")
async def login(body: LoginIn, users: Users, response: Response) -> StatusOut:
    user_id = await users.authenticate(body.email, body.password)
    if user_id is None:  # заглушка 001.13 не возвращает None; 001.14 — 401 invalid_credentials
        raise ApiError("invalid_credentials", "неверный адрес или пароль", status=401)
    set_session_cookie(response, secrets.token_urlsafe(32), USER_SESSION_TTL)
    return StatusOut(status="ok")


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Выход")
async def logout(response: Response) -> None:
    clear_session_cookie(response)


@router.post(
    "/logout-all", status_code=status.HTTP_204_NO_CONTENT, summary="Выход на всех устройствах"
)
async def logout_all(response: Response) -> None:
    clear_session_cookie(response)


@router.post(
    "/reset-request",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=StatusOut,
    summary="Запрос восстановления пароля (UC-15, шаги 1–3)",
)
async def reset_request(body: ResetRequestIn, users: Users) -> StatusOut:
    await users.request_reset(body.email)
    return StatusOut(status="accepted")  # одинаково для любого адреса (UC-15 A1)


@router.post(
    "/reset-confirm",
    response_model=StatusOut,
    summary="Подтверждение восстановления (UC-15, шаги 4–5)",
)
async def reset_confirm(body: ResetConfirmIn, users: Users) -> StatusOut:
    await users.confirm_reset(body.token, body.password)
    return StatusOut(status="password_changed")
