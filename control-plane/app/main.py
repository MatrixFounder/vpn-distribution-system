"""Приложение C-01: сборка FastAPI (``create_app``), маршрутизаторы ``/api/v1``, ``/agent/v1``,
``/s``, единый формат ошибок, ``/healthz`` и ``/metrics``, OpenAPI (R-50).

``create_app()`` не обращается к базе и Redis: подключения создаются лениво при первом запросе
(``app.db.pool``, ``app.redis``) и закрываются в lifespan. Точка входа uvicorn —
``app.main:create_app --factory`` (``docker-entrypoint.sh``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from app import __version__
from app.agent_api.router import router as agent_router
from app.api.router import router as api_router
from app.db.pool import close_pool
from app.errors import install_error_handlers
from app.redis import close_redis
from app.subscription.router import router as subscription_router

API_VERSION = "v1"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Старт без обращений наружу; при остановке — закрыть пул и клиент Redis."""
    try:
        yield
    finally:
        await close_pool()
        await close_redis()


def create_app() -> FastAPI:
    """Собрать приложение: роутеры, обработчики ошибок, служебные маршруты, OpenAPI."""
    app = FastAPI(
        title="Control Plane",
        version=__version__,
        description=f"API версии {API_VERSION}: /api/{API_VERSION}, /agent/{API_VERSION}, /s",
        lifespan=lifespan,
        docs_url=None,  # интерактивной документации нет; схема — /openapi.json (R-50)
        redoc_url=None,
        # Без редиректов по завершающему слэшу: редирект строится из схемы запроса, и за прокси
        # без доверенных заголовков уносил бы токен /s/{token} (Н-25) в http://; лишний слэш —
        # 404 в едином формате.
        redirect_slashes=False,
    )
    install_error_handlers(app)
    app.include_router(api_router)
    app.include_router(agent_router)
    app.include_router(subscription_router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Живость процесса (healthcheck compose, §10.2); готовность базы и Redis — 001.68."""
        return {"status": "ok", "version": __version__}

    @app.get("/metrics", include_in_schema=False, response_class=PlainTextResponse)
    async def metrics() -> str:
        """Экспозиция Prometheus (§5.4); до задачи 001.68 — один показатель живости."""
        return (
            "# HELP control_plane_up Процесс Control Plane запущен.\n"
            "# TYPE control_plane_up gauge\n"
            "control_plane_up 1\n"
        )

    return app
