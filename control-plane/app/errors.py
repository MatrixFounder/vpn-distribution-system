"""Единый формат ошибок API (interfaces.md §5.1): ``{"error": {"code", "message", "details"}}``.

``ApiError`` поднимают обработчики и доменный слой; обработчики FastAPI переводят в тот же формат
ошибки маршрутизации (404, 405), валидации (422, ``details.errors`` — pydantic) и необработанные
исключения (500 без подробностей — они уходят в журнал, не клиенту).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

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
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details: dict[str, Any] = details or {}

    def response(self) -> JSONResponse:
        return error_response(self.status, self.code, self.message, self.details)


def error_response(
    status: int, code: str, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    """Тело ошибки в едином формате §5.1."""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or {}}},
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


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("необработанная ошибка: %s %s", request.method, request.url.path)
    return error_response(500, "internal_error", "внутренняя ошибка сервера")


def install_error_handlers(app: FastAPI) -> None:
    """Подключить обработчики единого формата ко всем классам ошибок."""
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(Exception, _unhandled_handler)
