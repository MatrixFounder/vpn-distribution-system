"""Общие фикстуры тестов Control Plane.

`pg_dsn`, `migrate_env` и `redis_url` читаются из окружения стенда разработки
(`deploy/compose/.env.example`, задача 001.02; `MIGRATE_DSN` — подключение `app_migrate` для
`app.cli migrate`). `app_client` — HTTP-клиент к приложению в процессе теста через транспорт
ASGI (задача 001.10): без сети и без lifespan, исключения приложения превращаются в ответ 500
единого формата, а не поднимаются в тест.
"""

import os
from collections.abc import AsyncIterator

import httpx
import pytest
from app.main import create_app


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    """DSN PostgreSQL стенда разработки под ролью `app_rw` (роли создаёт bootstrap 001.03)."""
    return os.environ.get("PG_DSN", "postgresql://app_rw:app@127.0.0.1:5432/control_plane")


@pytest.fixture(scope="session")
def migrate_env() -> dict[str, str]:
    """Окружение `python -m app.cli migrate`: MIGRATE_DSN по умолчанию — стенд под app_migrate."""
    env = dict(os.environ)
    env.setdefault("MIGRATE_DSN", "postgresql://app_migrate:app@127.0.0.1:5432/control_plane")
    return env


@pytest.fixture(scope="session")
def redis_url() -> str:
    """URL Redis стенда разработки."""
    return os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")


@pytest.fixture
async def app_client() -> AsyncIterator[httpx.AsyncClient]:
    """HTTP-клиент к приложению `create_app()` через транспорт ASGI."""
    transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://control-plane") as client:
        yield client
