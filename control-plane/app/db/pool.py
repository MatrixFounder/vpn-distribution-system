"""Пул подключений asyncpg под ролью ``app_rw`` (data-model.md §4.6) и транзакции.

Пул создаётся лениво при первом ``get_pool()`` — импорт и ``create_app()`` к базе не обращаются;
``close_pool()`` вызывается при остановке приложения (lifespan в ``app.main``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from app.config import Settings

_pool: asyncpg.Pool | None = None
_lock = asyncio.Lock()


async def get_pool(settings: Settings | None = None) -> asyncpg.Pool:
    """Единственный пул процесса; ``settings`` читаются из окружения, если не переданы."""
    global _pool
    if _pool is not None:
        return _pool
    async with _lock:
        if _pool is None:
            settings = settings or Settings.load()
            _pool = await asyncpg.create_pool(
                settings.pg_dsn_with_password, min_size=1, max_size=10, command_timeout=30
            )
    return _pool


async def close_pool() -> None:
    """Закрыть пул (остановка процесса); повторный вызов безопасен."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def transaction(pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    """Подключение из пула в транзакции: commit на выходе, rollback при исключении."""
    async with pool.acquire() as conn, conn.transaction():
        yield conn
