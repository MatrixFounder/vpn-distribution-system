"""Маршрут Node API, сверяющий тело по байтам с пределами формы своей модели до того, как FastAPI
разберёт JSON (образец — «Custom Request and APIRoute class» FastAPI; правило и пределы —
``app.body_shape``). Все роутеры раздела объявляются с ``route_class=BoundedBodyRoute``: у
операции без тела (GET состояния) проверки нет, у операции с телом пределы выведены из её
модели — heartbeat из пяти полей отвергает шестую запятую до разбора, отчёт получает пределы
частей. Сбой чтения тела (обрыв заливки, ошибка транспорта) — 400 единого формата с тем же
текстом, что даёт FastAPI на остальных операциях; отказы приложения (``ApiError``,
``HTTPException``) проходят как есть, всё прочее пишется в журнал — сбой сервера не должен
выглядеть виной клиента без следа.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.requests import ClientDisconnect

from app.body_shape import ShapeLimits, shape_limits
from app.errors import BODY_PARSE_MESSAGE, ApiError, validation_failure

log = logging.getLogger(__name__)


class BoundedBodyRoute(APIRoute):
    """Маршрут с проверкой формы тела по байтам до разбора JSON; отказ — 422 в едином формате с
    одной ошибкой ``too_wide``. Пределы — из модели тела маршрута (``shape_limits``)."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        # ``APIRoute.__init__`` строит обработчик до конца собственной инициализации, поэтому
        # пределы берутся здесь, из уже разобранного поля тела; операция без тела — без проверки.
        if self.body_field is None:
            return handler
        model = self.body_field.field_info.annotation
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise TypeError(f"тело маршрута {self.path} — не модель pydantic: {model!r}")
        # Модель без верхней границы формы — ошибка объявления маршрута, не тихий пропуск.
        shape: ShapeLimits = shape_limits(model)

        async def bounded(request: Request) -> Response:
            try:
                body = await request.body()
            except ApiError, HTTPException:
                raise
            except ClientDisconnect as exc:
                raise ApiError("bad_request", BODY_PARSE_MESSAGE, status=400) from exc
            except Exception as exc:  # noqa: BLE001 — то же правило, что у FastAPI, но со следом
                log.warning("сбой чтения тела %s: %s", request.url.path, exc, exc_info=exc)
                raise ApiError("bad_request", BODY_PARSE_MESSAGE, status=400) from exc
            problem = shape.problem(body, "этой операции")
            if problem is not None:
                raise validation_failure(problem, "too_wide")
            return await handler(request)

        return bounded
