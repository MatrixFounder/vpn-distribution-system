"""Пул подключений asyncpg под ролью ``app_rw`` (data-model.md §4.6) и транзакции.

Пул создаётся лениво при первом ``get_pool()`` — импорт и ``create_app()`` к базе не обращаются;
``close_pool()`` вызывается при остановке приложения (lifespan в ``app.main``). Пул привязан к
циклу событий: в тестах (цикл на каждый тест) пул закрытого цикла обрывается ``terminate()`` и
создаётся заново (001.12); под uvicorn/исполнителем цикл один.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from app.config import Settings

_pool: asyncpg.Pool | None = None
_loop: asyncio.AbstractEventLoop | None = None
_lock = asyncio.Lock()


class DatabaseUnavailable(Exception):  # noqa: N818 — парное имя к RateLimitUnavailable
    """PostgreSQL недоступен: C-01 отвечает 503 (reliability.md §9.1)."""


async def get_pool(settings: Settings | None = None) -> asyncpg.Pool:
    """Единственный пул процесса; ``settings`` читаются из окружения, если не переданы."""
    global _pool, _loop
    loop = asyncio.get_running_loop()
    if _pool is not None and _loop is not loop:
        _discard(_pool)  # подключения чужого (закрытого) цикла — только обрыв
        _pool = None
    if _pool is not None:
        return _pool
    async with _lock:
        if _pool is None:
            _loop = loop
            settings = settings or Settings.load()
            try:
                _pool = await asyncpg.create_pool(
                    settings.pg_dsn_with_password,
                    min_size=1,
                    max_size=10,
                    command_timeout=30,
                    timeout=5,
                )
            except (TimeoutError, OSError, asyncpg.PostgresConnectionError) as exc:
                raise DatabaseUnavailable(f"{type(exc).__name__}: {exc}") from exc
    return _pool


async def close_pool() -> None:
    """Закрыть пул (остановка процесса); повторный вызов безопасен; пул чужого цикла — обрыв."""
    global _pool, _loop
    if _pool is None:
        return
    pool, _pool = _pool, None
    own_loop = _loop is asyncio.get_running_loop()
    _loop = None
    if own_loop:
        await pool.close()
    else:
        _discard(pool)


def _discard(pool: asyncpg.Pool) -> None:
    """Пул цикла, который уже закрыт: обрыв подключений; если и это невозможно (транспорты
    закрытого цикла), ссылка просто отбрасывается — сокеты освободит сборщик мусора."""
    with contextlib.suppress(RuntimeError):
        pool.terminate()


@asynccontextmanager
async def transaction(pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    """Подключение из пула в транзакции: commit на выходе, rollback при исключении."""
    async with pool.acquire() as conn, conn.transaction():
        yield conn
