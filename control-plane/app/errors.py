"""Единый формат ошибок API (interfaces.md §5.1): ``{"error": {"code", "message", "details"}}``.

``ApiError`` поднимают обработчики и доменный слой; обработчики FastAPI переводят в тот же формат
ошибки маршрутизации (404, 405), валидации (422, ``details.errors`` — pydantic) и необработанные
исключения (500 без подробностей — они уходят в журнал, не клиенту).
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.db.pool import DatabaseUnavailable
from app.security.ratelimit import RETRY_AFTER_SECONDS, RateLimitUnavailable

log = logging.getLogger(__name__)

# Коды по статусам для ошибок, которые поднимает сам фреймворк (маршрутизация, методы).
STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    501: "not_implemented",
    503: "service_unavailable",
}


class ApiError(Exception):
    """Ошибка API с машинным кодом, HTTP-статусом и подробностями для клиента."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details: dict[str, Any] = details or {}
        self.headers: dict[str, str] = headers or {}  # например, Retry-After при 429/503

    def response(self) -> JSONResponse:
        return error_response(self.status, self.code, self.message, self.details, self.headers)


def error_response(
    status: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Тело ошибки в едином формате §5.1."""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or {}}},
        headers=headers,
    )


def not_implemented(operation: str) -> ApiError:
    """Заглушка STUB-задач: 501 с кодом ``not_implemented`` (задача 001.10)."""
    return ApiError(
        "not_implemented",
        f"операция «{operation}» ещё не реализована",
        status=501,
        details={"operation": operation},
    )


async def _api_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)  # noqa: S101 — регистрируется только для ApiError
    return exc.response()


async def _http_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    code = STATUS_CODES.get(exc.status_code, "http_error")
    message = exc.detail if isinstance(exc.detail, str) else code
    response = error_response(exc.status_code, code, message)
    if exc.headers:
        response.headers.update(exc.headers)
    return response


async def _validation_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    errors = [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in exc.errors()
    ]
    return error_response(422, "validation_error", "запрос не прошёл проверку", {"errors": errors})


async def _unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    """Fail-closed (§9.1): Redis или PostgreSQL недоступны → 503 с Retry-After, без подробностей
    клиенту."""
    log.error("503 %s %s: %s", request.method, request.url.path, exc)
    response = error_response(503, "service_unavailable", "сервис временно недоступен")
    response.headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
    return response


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("необработанная ошибка: %s %s", request.method, request.url.path)
    return error_response(500, "internal_error", "внутренняя ошибка сервера")


def install_error_handlers(app: FastAPI) -> None:
    """Подключить обработчики единого формата ко всем классам ошибок."""
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(RateLimitUnavailable, _unavailable_handler)
    app.add_exception_handler(DatabaseUnavailable, _unavailable_handler)
    app.add_exception_handler(asyncpg.PostgresConnectionError, _unavailable_handler)
    # Сессии и счётчики живут в Redis: его недоступность посреди запроса (после redis_required
    # или на маршруте без него) — тоже 503, а не 500 (ревью 001.15). Только отказы подключения и
    # таймауты: прочие RedisError (WRONGTYPE, ошибка Lua) — дефекты кода, им положен 500 с
    # трассировкой в журнале.
    app.add_exception_handler(RedisConnectionError, _unavailable_handler)
    app.add_exception_handler(RedisTimeoutError, _unavailable_handler)
    app.add_exception_handler(Exception, _unhandled_handler)
