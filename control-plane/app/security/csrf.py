"""CSRF (security.md §7.3): cookie ``SameSite=Lax`` плюс заголовок ``X-CSRF-Token`` для
изменяющих методов. Токен выдаётся вместе с сессией (``Session.csrf``) и отдаётся клиенту в
cookie ``csrf`` без ``HttpOnly`` (двойная отправка: скрипт кабинета читает cookie и кладёт значение
в заголовок; чужой сайт cookie прочитать не может). Проверка (001.14): при наличии сессионной
cookie изменяющий запрос обязан нести заголовок, равный токену сессии; без сессии проверять
нечего — такие маршруты защищены отсутствием побочных эффектов для чужой учётной записи."""

from __future__ import annotations

import hmac

from fastapi import Request, Response

from app.errors import ApiError
from app.redis import get_redis
from app.security.sessions import SESSION_COOKIE, SessionStore

CSRF_HEADER = "X-CSRF-Token"
CSRF_COOKIE = "csrf"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def set_csrf_cookie(response: Response, token: str, ttl: int) -> None:
    """Cookie с токеном для скрипта кабинета: без HttpOnly (читается JS), Secure, SameSite=Lax."""
    response.set_cookie(
        CSRF_COOKIE, token, max_age=ttl, path="/", httponly=False, secure=True, samesite="lax"
    )


def clear_csrf_cookie(response: Response) -> None:
    response.delete_cookie(CSRF_COOKIE, path="/", httponly=False, secure=True, samesite="lax")


async def require_csrf(request: Request) -> None:
    """Зависимость FastAPI для изменяющих методов: заголовок ``X-CSRF-Token`` равен токену
    сессии из cookie ``sid``; безопасные методы и запросы без сессии проходят."""
    if request.method in SAFE_METHODS:
        return
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return
    session = await SessionStore(await get_redis()).get(sid)
    if session is None:
        return  # сессии нет — защищать нечего; аутентификация решит сама
    header = request.headers.get(CSRF_HEADER, "")
    if not header or not hmac.compare_digest(header, session.csrf):
        raise ApiError("csrf_failed", "нет или неверен заголовок X-CSRF-Token", status=403)
