"""Раздел ``/api/v1/admin/auth`` — вход администратора (§7.1, R-36; interfaces.md §5.1; UC-09,
UC-13): пароль, второй фактор TOTP или резервный код, выход, сведения о своей сессии.

Задача 001.46: маршруты и формы. Вход отвечает ``202 totp_required`` без проверки пароля и без
сессии; второй фактор, резервный код и выход — 501 ``not_implemented``; настоящий двухшаговый
вход, выдача сессии вида ``admin`` и пороги частоты — 001.47. ``GET …/session`` настоящий:
роль и язык панели — из ``admin_users`` (``current_admin``), разрешения — по реестру
``app.security.rbac`` (по списку панель скрывает недоступные действия, 001.64).

Разрешения раздел не объявляет и роутер панели с ``declared_permission`` не использует: вход идёт
до сессии, а свою сессию читает и закрывает администратор любой роли. Без Redis — 503
(``redis_required`` при подключении в ``app.api.router``).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, StringConstraints

from app.errors import not_implemented
from app.security.csrf import require_csrf
from app.security.deps import Admin
from app.security.rbac import permissions_of

router = APIRouter(prefix="/admin/auth", tags=["admin"])

# Адрес и пароль — те же границы, что у входа пользователя (``app.api.auth``); код TOTP — шесть
# цифр (RFC 6238, умолчание приложений-аутентификаторов). Формат резервного кода задаёт 001.47.
Email = Annotated[
    str,
    StringConstraints(
        min_length=3, max_length=254, pattern=r"^[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+$"
    ),
]
Password = Annotated[str, StringConstraints(min_length=8, max_length=256)]
TotpCode = Annotated[str, StringConstraints(pattern=r"^[0-9]{6}$")]
RecoveryCode = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AdminLoginIn(StrictModel):
    email: Email
    password: Password


class TotpIn(StrictModel):
    code: TotpCode


class RecoveryIn(StrictModel):
    code: RecoveryCode


class LoginStep(BaseModel):
    """Шаг входа: пароль принят, нужен второй фактор (сессия ещё не выдана)."""

    status: Literal["totp_required"]


class AdminSession(BaseModel):
    """Своя сессия администратора: роль, разрешения роли и язык панели (``admin_users``)."""

    role: str
    permissions: list[str]
    language: str


@router.post(
    "/login",
    response_model=LoginStep,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Вход администратора: пароль (шаг 1 из 2)",
)
async def login(body: AdminLoginIn) -> LoginStep:
    """Заглушка 001.46: пароль не проверяется, сессия не выдаётся — вход не завершён без второго
    фактора (TC-E2E-01). Проверка пароля, статуса и выдача промежуточного шага — 001.47."""
    return LoginStep(status="totp_required")


@router.post(
    "/totp",
    summary="Вход администратора: код TOTP (шаг 2 из 2; не реализовано — 501)",
    responses={501: {"description": "не реализовано — `not_implemented` (001.47)"}},
)
async def totp(body: TotpIn) -> None:
    raise not_implemented("admin.auth.totp")


@router.post(
    "/recovery",
    summary="Вход администратора: резервный код вместо TOTP (не реализовано — 501)",
    responses={501: {"description": "не реализовано — `not_implemented` (001.47)"}},
)
async def recovery(body: RecoveryIn) -> None:
    raise not_implemented("admin.auth.recovery")


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Выход администратора (не реализовано — 501)",
    dependencies=[Depends(require_csrf)],
    responses={501: {"description": "не реализовано — `not_implemented` (001.47)"}},
)
async def logout(admin: Admin) -> None:
    raise not_implemented("admin.auth.logout")


@router.get("/session", response_model=AdminSession, summary="Своя сессия администратора")
async def session(admin: Admin) -> AdminSession:
    """Роль и язык — строка ``admin_users`` на момент запроса, разрешения — по реестру: смена
    роли видна панели со следующего запроса."""
    return AdminSession(
        role=admin.role, permissions=permissions_of(admin.role), language=admin.language
    )
