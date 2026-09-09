"""Планировщик (C-03): ``python -m app.jobs.scheduler`` — один активный экземпляр под
``pg_advisory_lock`` (§5.4); по расписанию ``SCHEDULE`` ставит периодические задачи в очередь
с ключом идемпотентности ``<имя>:<слот>``, так что повторный запуск за тот же слот — no-op (R-46).
Живость сессии-держателя не проверяется: при разрыве её сессии блокировку получит другой
экземпляр, и короткое время могут работать два лидера — расщепление обезврежено ключом слота
(один enqueue на слот), а не исключительностью процесса. Ошибки базы в цикле журналируются,
подключения пула обновляются, цикл продолжается. Задача 001.11 — каркас без задач: ``SCHEDULE``
пуст; периодические задачи (истечение подписок, пороги, агрегация, сверки, партиции, сроки
хранения, Offline) добавляют LOGIC-задачи.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.config import Settings
from app.db.pool import close_pool, get_pool
from app.jobs import queue as jobs
from app.jobs.worker import RECOVERABLE, RECOVERY_PAUSE, recover

log = logging.getLogger(__name__)

# Ключ advisory-блокировки лидера: один на кластер, фиксирован (§5.4).
LEADER_LOCK_KEY = 0x434F4E54524F4C01  # "CONTROL" + 01
TICK_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class Periodic:
    """Периодическая задача: каждые ``interval`` секунд ставится ``type`` в ``queue``; слот —
    номер интервала от эпохи, чтобы ключ был общим для всех экземпляров планировщика."""

    name: str
    interval: dt.timedelta
    queue: str
    type: str
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if self.interval <= dt.timedelta(0):
            raise ValueError(f"{self.name}: интервал должен быть положительным")
        if self.queue not in jobs.QUEUES:
            raise ValueError(f"{self.name}: неизвестная очередь {self.queue}")

    def slot(self, now: dt.datetime) -> int:
        return int(now.timestamp() // self.interval.total_seconds())

    def idempotency_key(self, now: dt.datetime) -> str:
        return f"{self.name}:{self.slot(now)}"


SCHEDULE: list[Periodic] = []  # заполняют LOGIC-задачи (001.11 — каркас)


async def tick(pool: asyncpg.Pool, now: dt.datetime, schedule: Sequence[Periodic]) -> int:
    """Поставить задачи всех периодических элементов на текущий слот; вернуть число новых."""
    enqueued = 0
    for periodic in schedule:
        async with pool.acquire() as conn, conn.transaction():
            try:
                await jobs.enqueue(
                    conn,
                    periodic.queue,
                    periodic.type,
                    periodic.payload,
                    periodic.idempotency_key(now),
                )
            except jobs.DuplicateJobError:
                continue  # слот уже поставлен (этим или прежним экземпляром)
        enqueued += 1
    return enqueued


async def acquire_leadership(conn: asyncpg.Connection, lock_key: int = LEADER_LOCK_KEY) -> bool:
    """Попытаться стать единственным планировщиком (сессионная advisory-блокировка)."""
    locked: bool = await conn.fetchval("select pg_try_advisory_lock($1)", lock_key)
    return locked


async def run(
    *,
    stop: asyncio.Event | None = None,
    schedule: Sequence[Periodic] | None = None,
    pool: asyncpg.Pool | None = None,
    lock_key: int = LEADER_LOCK_KEY,
) -> None:
    """Главный цикл: ждать лидерства, затем каждую секунду ставить задачи по расписанию.
    ``pool`` — как у ``worker.run``: переданный остаётся открытым, общий закрывается на выходе;
    ``lock_key`` — ключ лидерства (тесты берут свой, чтобы не спорить с планировщиком стенда)."""
    stop = stop or asyncio.Event()
    schedule = SCHEDULE if schedule is None else schedule
    settings = Settings.load()
    owns_pool = pool is None
    pool = pool or await get_pool(settings)
    leader: asyncpg.Connection | None = None
    try:
        while not stop.is_set():
            if leader is None or leader.is_closed():
                try:
                    leader = await asyncpg.connect(
                        settings.pg_dsn_with_password,
                        server_settings={"application_name": "scheduler:leader"},
                    )
                except RECOVERABLE as exc:
                    log.warning("планировщик: нет подключения (%s) — повтор", exc)
                    await _sleep(stop, 5.0)
                    continue
            try:
                if not await acquire_leadership(leader, lock_key):
                    log.info("планировщик ждёт лидерства")
                    await _sleep(stop, 5.0)
                    continue
            except RECOVERABLE as exc:
                log.warning("планировщик: сбой захвата лидерства (%s)", exc)
                await _sleep(stop, 5.0)
                continue
            break
        if stop.is_set():
            return
        log.info("планировщик — лидер; расписание: %d", len(schedule))
        while not stop.is_set():
            try:
                await tick(pool, dt.datetime.now(dt.UTC), schedule)
            except RECOVERABLE as exc:
                await recover(pool, exc, "планировщик")
                await _sleep(stop, RECOVERY_PAUSE)
                continue
            await _sleep(stop, TICK_SECONDS)
    finally:
        if leader is not None and not leader.is_closed():
            await leader.close()  # закрытие сессии снимает advisory-блокировку
        if owns_pool:
            await close_pool()


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


def main(argv: Sequence[str] | None = None) -> int:
    del argv  # аргументов нет
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "info").upper())

    async def serve() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run(stop=stop)

    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
