"""Общие фикстуры тестов Control Plane.

`pg_dsn`, `migrate_env` и `redis_url` читаются из окружения стенда разработки
(`deploy/compose/.env.example`, задача 001.02; `MIGRATE_DSN` — подключение `app_migrate` для
`app.cli migrate`). `app_client` — HTTP-клиент к приложению в процессе теста; до задачи 001.10,
где появляется приложение и транспорт ASGI, фикстура пропускает тест, а не обращается в сеть.
"""

import os
from collections.abc import AsyncIterator

import httpx
import pytest


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
    """HTTP-клиент к приложению через транспорт ASGI (подключается в задаче 001.10)."""
    pytest.skip("app_client: транспорт ASGI и приложение появляются в задаче 001.10")
    yield httpx.AsyncClient()  # недостижимо; сохраняет тип генератора для mypy
