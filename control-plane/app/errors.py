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


# FastAPI отвечает этим текстом, когда тело не разобрать до проверки формы (негодный UTF-8,
# целое длиннее 4 300 цифр — ``ValueError`` разбора, не ``JSONDecodeError``) и когда заливка
# оборвана (``ClientDisconnect`` при чтении тела попадает под его общий ``except Exception``):
# клиенту — единый формат и русский текст. Глубина вложенности сюда не приводит: разбор JSON
# Python 3.14 проходит 32 000 уровней без ``RecursionError``, а глубже 64-КиБ тело не бывает.
# Строка — из fastapi 0.141.1 (``requirements.lock``): обновление зависимости меняет контракт,
# страж — ``test_a_body_that_cannot_be_parsed_is_a_unified_400`` (``tests/e2e``).
FASTAPI_BODY_PARSE_DETAIL = "There was an error parsing the body"
BODY_PARSE_MESSAGE = "тело запроса не разобрано"


async def _http_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    code = STATUS_CODES.get(exc.status_code, "http_error")
    message = exc.detail if isinstance(exc.detail, str) else code
    if exc.status_code == 400 and message == FASTAPI_BODY_PARSE_DETAIL:
        message = BODY_PARSE_MESSAGE
    response = error_response(exc.status_code, code, message)
    if exc.headers:
        response.headers.update(exc.headers)
    return response


# Сколько ошибок валидации попадает в ответ 422. pydantic собирает по ошибке на каждый негодный
# элемент и на каждое лишнее поле, а тело отчёта о трафике (001.33) — до тысяч строк: без
# предела негодное тело в мегабайты порождало бы ответ в мегабайты и второй проход по нему в том
# же цикле событий. Остаток сообщается числом, ``input`` и ``ctx`` не копируются никогда, а
# элемент ``loc`` — имя лишнего поля, то есть текст клиента — обрезается до
# ``VALIDATION_LOC_MAX_CHARS``: пятьдесят ключей по десять тысяч символов (полмегабайта под
# пределом прокси) иначе вернулись бы эхом. Потолок ограничивает ответ, а не работу: список
# ошибок pydantic строит целиком до среза, поэтому число ошибок держит проверка формы тела по
# байтам до разбора (``app.body_shape``), а не этот срез.
VALIDATION_ERRORS_MAX = 50
VALIDATION_LOC_MAX_CHARS = 64
# Сообщение pydantic — текст сервера, но валидатор, который вставит в ``ValueError`` само
# значение, вернул бы его эхом; предел — обязательство §5.1 interfaces.md («сообщения ``msg`` —
# до 200»), той же природы, что и у ``loc``.
VALIDATION_MSG_MAX_CHARS = 200


def validation_failure(msg: str, type_: str, loc: tuple[object, ...] = ("body",)) -> ApiError:
    """422 единого формата с одной ошибкой — той же формы, что даёт ``_validation_handler``
    (проверки до разбора тела: ``agent_api.body.BoundedBodyRoute``)."""
    return ApiError(
        "validation_error",
        "запрос не прошёл проверку",
        status=422,
        details={"errors": [{"loc": list(loc), "msg": msg, "type": type_}]},
    )


def _loc(parts: object) -> list[object]:
    return [
        part[:VALIDATION_LOC_MAX_CHARS] if isinstance(part, str) else part
        for part in (parts if isinstance(parts, list | tuple) else ())
    ]


async def _validation_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    found = exc.errors()
    errors = [
        {
            "loc": _loc(e.get("loc", ())),
            "msg": str(e.get("msg", ""))[:VALIDATION_MSG_MAX_CHARS],
            "type": e.get("type", ""),
        }
        for e in found[:VALIDATION_ERRORS_MAX]
    ]
    details: dict[str, Any] = {"errors": errors}
    if len(found) > VALIDATION_ERRORS_MAX:
        details["truncated"] = len(found) - VALIDATION_ERRORS_MAX
    return error_response(422, "validation_error", "запрос не прошёл проверку", details)


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
