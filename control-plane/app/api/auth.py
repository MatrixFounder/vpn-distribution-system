"""Раздел ``/api/v1/auth`` (interfaces.md §5.1; UC-02 шаги 1–6, UC-15): регистрация, подтверждение
адреса, вход, выход, «выход везде», запрос и подтверждение восстановления пароля.

Задача 001.14: настоящая логика поверх ``UserService`` (база, очередь писем), ``SessionStore``
(Redis), ``RateLimiter`` (пороги §5.12), ``require_csrf`` (§7.3). Вход выставляет cookie сессии
``sid`` (§7.1) и cookie ``csrf`` для заголовка ``X-CSRF-Token``. Ответы ``reset-request``
одинаковы для любого адреса — статус, тело, заголовки и путь кода (UC-15 A1). Fail-closed: без
Redis все маршруты раздела отвечают 503 (``redis_required`` роутера, §9.1).
"""

from __future__ import annotations

from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.db.pool import get_pool
from app.domain.users import CaptchaAnswer, UserService, normalize_email
from app.errors import ApiError
from app.redis import get_redis
from app.security import ratelimit
from app.security.csrf import clear_csrf_cookie, require_csrf, set_csrf_cookie
from app.security.sessions import (
    SESSION_COOKIE,
    USER_SESSION_TTL,
    SessionStore,
    clear_session_cookie,
    set_session_cookie,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Границы полей (§9 постановки минимальной длины пароля не задаёт — решение 001.13, объявлено):
# адрес — форма «имя@домен» без пробелов и управляющих символов (NUL и переводы строки не должны
# доезжать до базы и конверта письма), нормализация §16.8 — в домене; пароль 8…256 символов.
Email = Annotated[
    str,
    StringConstraints(
        min_length=3, max_length=254, pattern=r"^[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+$"
    ),
]
Password = Annotated[str, StringConstraints(min_length=8, max_length=256)]
Token = Annotated[str, StringConstraints(min_length=16, max_length=512)]


class StrictModel(BaseModel):
    """Тела запросов /auth: неизвестные поля отклоняются (422), а не проглатываются; краевые
    пробелы строк обрезаются до проверки (адрес с пробелами по краям — обычная опечатка)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


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


async def get_user_service() -> UserService:
    """Сервис пользователей поверх пула процесса (CAPTCHA-провайдер — ОВ-A3, пока отсутствует)."""
    return UserService(await get_pool())


async def get_session_store() -> SessionStore:
    return SessionStore(await get_redis())


async def get_rate_limiter() -> ratelimit.RateLimiter:
    return ratelimit.RateLimiter(await get_redis())


Users = Annotated[UserService, Depends(get_user_service)]
Sessions = Annotated[SessionStore, Depends(get_session_store)]
Limiter = Annotated[ratelimit.RateLimiter, Depends(get_rate_limiter)]
Pool = Annotated[asyncpg.Pool, Depends(get_pool)]


def client_ip(request: Request) -> str:
    """Адрес клиента: uvicorn подставляет X-Forwarded-For, который перезаписывает nginx (001.10)."""
    return request.client.host if request.client else "0.0.0.0"  # noqa: S104 — метка, не bind


def user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")[:512]


def captcha_required() -> ApiError:
    return ApiError(
        "captcha_required",
        "слишком много попыток: пройдите проверку",
        status=429,
        details={"captcha_required": True},
    )


async def limit(
    limiter: ratelimit.RateLimiter,
    key: str,
    threshold: ratelimit.Limit,
    *,
    captcha: CaptchaAnswer | None = None,
) -> None:
    """Учесть попытку; превышение порога — отказ 429 (UC-02 A5, UC-15 A3). Реакция §5.12:
    там, где положена CAPTCHA (``captcha`` — ответ запроса, провайдер спрашивается один раз на
    запрос), при настроенном провайдере верный ответ пропускает, иначе 429 ``captcha_required``;
    без провайдера (ОВ-A3) и там, где положен «отказ с задержкой», — 429 ``rate_limited`` с
    ``Retry-After`` до освобождения окна."""
    if await limiter.check(key, threshold.count, threshold.window_s):
        return
    if captcha is not None and captcha.available:
        if await captcha.verified():
            return
        raise captcha_required()
    retry_after = await limiter.retry_after(key, threshold.window_s)
    raise ApiError(
        "rate_limited",
        "слишком много запросов",
        status=429,
        headers={"Retry-After": str(max(1, retry_after))},
    )


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=StatusOut,
    summary="Регистрация (UC-02, шаги 3–5)",
)
async def register(body: RegisterIn, request: Request, users: Users, limiter: Limiter) -> StatusOut:
    ip = client_ip(request)
    captcha = users.captcha_answer(body.captcha_token, ip)  # один ответ на весь запрос
    await limit(limiter, ratelimit.register_ip(ip), ratelimit.REGISTER_IP, captcha=captcha)
    domain = normalize_email(body.email).rsplit("@", 1)[1]
    await limit(
        limiter, ratelimit.register_email_domain(domain), ratelimit.REGISTER_DOMAIN, captcha=captcha
    )
    await users.register(
        body.email,
        body.password,
        body.aup_version,
        body.lang,
        captcha=captcha,
        ip=ip,
        user_agent=user_agent(request),
    )
    return StatusOut(status="unconfirmed")


@router.post("/verify", response_model=StatusOut, summary="Подтверждение адреса (UC-02, шаг 6)")
async def verify(body: VerifyIn, request: Request, users: Users, limiter: Limiter) -> StatusOut:
    await limit(limiter, ratelimit.key("verify", "ip", client_ip(request)), ratelimit.TOKEN_IP)
    await users.verify_email(body.token)
    return StatusOut(status="confirmed")


@router.post("/login", response_model=StatusOut, summary="Вход пользователя (§7.1)")
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    users: Users,
    sessions: Sessions,
    limiter: Limiter,
) -> StatusOut:
    ip = client_ip(request)
    captcha = users.captcha_answer(body.captcha_token, ip)  # один ответ на весь запрос
    # §5.12: вход — раздельно по адресу источника и по учётной записи, реакция CAPTCHA для обоих
    # измерений: за общим адресом трансляции порог 5/мин переступает и добросовестный
    # пользователь — с провайдером он проходит проверку, без провайдера получает отказ с
    # Retry-After (ревью раунда 2, L-1). Оба порога спрашивают один и тот же ответ — провайдер
    # вызывается не более одного раза (ответы одноразовые; ревью раунда 3, S-1).
    await limit(limiter, ratelimit.login_ip(ip), ratelimit.LOGIN_IP, captcha=captcha)
    # §5.12: по учётной записи — CAPTCHA вместо блокировки. Счётчик хранит только отказы
    # (читается до проверки пароля, засчитывается после неё, обнуляется успешным входом):
    # чужие неверные попытки не запирают владельца. Над порогом при настроенном провайдере
    # CAPTCHA проверяется до пароля; без провайдера (ОВ-A3) порог не действует — верные учётные
    # данные проходят всегда, перебор сдерживает порог по адресу источника (объявлено в задаче).
    account_key = ratelimit.login_account(normalize_email(body.email))
    failures = await limiter.peek(account_key, ratelimit.LOGIN_ACCOUNT.window_s)
    over_threshold = failures >= ratelimit.LOGIN_ACCOUNT.count
    if over_threshold and captcha.available and not await captcha.verified():
        raise captcha_required()
    user_id = await users.authenticate(
        body.email, body.password, ip=ip, user_agent=user_agent(request)
    )
    if user_id is None:
        await limiter.check(
            account_key, ratelimit.LOGIN_ACCOUNT.count, ratelimit.LOGIN_ACCOUNT.window_s
        )  # засчитать отказ
        raise ApiError("invalid_credentials", "неверный адрес или пароль", status=401)
    await limiter.reset(account_key)
    session = await sessions.create("user", str(user_id), ip, user_agent(request), USER_SESSION_TTL)
    set_session_cookie(response, session.id, USER_SESSION_TTL)
    set_csrf_cookie(response, session.csrf, USER_SESSION_TTL)
    return StatusOut(status="ok")


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Выход",
    dependencies=[Depends(require_csrf)],
)
async def logout(request: Request, response: Response, sessions: Sessions) -> None:
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        await sessions.revoke(sid)
    clear_session_cookie(response)
    clear_csrf_cookie(response)


@router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Выход на всех устройствах",
    dependencies=[Depends(require_csrf)],
)
async def logout_all(request: Request, response: Response, sessions: Sessions) -> None:
    sid = request.cookies.get(SESSION_COOKIE)
    session = await sessions.get(sid) if sid else None
    if session is not None:
        await sessions.revoke_all(session.subject_id)
    clear_session_cookie(response)
    clear_csrf_cookie(response)


@router.post(
    "/reset-request",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=StatusOut,
    summary="Запрос восстановления пароля (UC-15, шаги 1–3)",
)
async def reset_request(
    body: ResetRequestIn, request: Request, users: Users, limiter: Limiter
) -> StatusOut:
    address = normalize_email(body.email)
    await limit(limiter, ratelimit.reset_email(address), ratelimit.RESET_EMAIL)  # UC-15 A3
    await users.request_reset(address)
    return StatusOut(status="accepted")  # одинаково для любого адреса (UC-15 A1)


@router.post(
    "/reset-confirm",
    response_model=StatusOut,
    summary="Подтверждение восстановления (UC-15, шаги 4–5)",
)
async def reset_confirm(
    body: ResetConfirmIn, request: Request, users: Users, sessions: Sessions, limiter: Limiter
) -> StatusOut:
    await limit(limiter, ratelimit.key("reset", "ip", client_ip(request)), ratelimit.TOKEN_IP)
    user_id = await users.confirm_reset_user(body.token, body.password)
    await sessions.revoke_all(str(user_id))  # UC-15 шаг 5: прежние сессии недействительны
    return StatusOut(status="password_changed")
