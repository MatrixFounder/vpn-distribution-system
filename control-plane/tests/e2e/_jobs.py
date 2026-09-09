"""Помощники сквозных тестов очереди задач.

Стенд живой: контейнеры worker-critical/worker-background/scheduler работают с той же базой.
Герметичность тестов — на двух свойствах кода: исполнители выбирают только задачи известных им
типов (``claim(types=HANDLERS)``), поэтому тесты регистрируют собственные типы ``test-*`` в
``HANDLERS`` своего процесса и ставят только их — живые исполнители такие задачи не трогают;
планировщик тестов берёт свой ключ лидерства. Ключи идемпотентности с префиксом ``test-jobs:``
удаляются до и после.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest
from app.db import pool as pool_module
from app.jobs import queue as jobs
from app.jobs.handlers import HANDLERS

PREFIX = "test-jobs:"  # ключи идемпотентности задач, созданных тестами
NOOP = "test-noop"  # тип задачи, известный только процессу тестов
BOOM = "test-boom"  # тип задачи, обработчик которого падает
VANISH = "test-vanish"  # тип задачи, обработчик которого удаляет её строку
FATAL = "test-fatal"  # тип задачи, обработчик которого отказывается без повторов (001.74)
TEST_LOCK_KEY = 0x7E57_0000_0000_0000 | (os.getpid() & 0xFFFF_FFFF)  # ключ лидерства тестов


async def noop(conn: asyncpg.Connection, job: jobs.Job) -> None:
    """Заглушка тестов: успех."""


async def boom(conn: asyncpg.Connection, job: jobs.Job) -> None:
    raise RuntimeError(f"сломалось {job.payload.get('x')}")


async def vanish(conn: asyncpg.Connection, job: jobs.Job) -> None:
    """Строка задачи исчезает во время обработки (уборка, оператор): завершать нечего."""
    await conn.execute("delete from jobs where id = $1", job.id)


async def fatal(conn: asyncpg.Connection, job: jobs.Job) -> None:
    """Обработчик знает, что повторять бессмысленно (например, получателя не существует)."""
    raise jobs.NonRetryableError(f"без повторов {job.payload.get('x')}")


@asynccontextmanager
async def stand_pool(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[asyncpg.Pool]:
    """Пул процесса под окружением стенда; тестовые типы в реестре; свои задачи удаляются до и
    после."""
    key = tmp_path / "key"
    key.write_text("k" * 44)
    monkeypatch.setenv("PG_DSN", pg_dsn)
    monkeypatch.setenv("APP_ROLE", "worker-background")
    monkeypatch.setenv("APP_ENCRYPTION_KEY_FILE", str(key))
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.delenv("PG_PASSWORD_FILE", raising=False)
    monkeypatch.setitem(HANDLERS, NOOP, noop)
    monkeypatch.setitem(HANDLERS, BOOM, boom)
    monkeypatch.setitem(HANDLERS, VANISH, vanish)
    monkeypatch.setitem(HANDLERS, FATAL, fatal)
    await pool_module.close_pool()
    pool = await pool_module.get_pool()
    await cleanup(pool)
    try:
        yield pool
    finally:
        await cleanup(pool)
        await pool_module.close_pool()


async def cleanup(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("delete from jobs where idempotency_key like $1", f"{PREFIX}%")


async def job_row(pool: asyncpg.Pool, job_id: int) -> dict[str, object]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "select status::text, attempts, locked_at, locked_by, last_error from jobs "
            "where id = $1",
            job_id,
        )
    assert row is not None, job_id
    return dict(row)


async def job_times(pool: asyncpg.Pool, job_id: int) -> dict[str, object]:
    """Времена задачи: постановка, готовность, последняя выборка и завершение (001.74)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "select created_at, run_at, claimed_at, finished_at, status::text from jobs "
            "where id = $1",
            job_id,
        )
    assert row is not None, job_id
    return dict(row)
