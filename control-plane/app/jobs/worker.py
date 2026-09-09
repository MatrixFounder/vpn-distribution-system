"""Исполнитель очереди (C-02): ``python -m app.jobs.worker --queue critical|background``.

Цикл: забрать и выполнить все готовые задачи известных типов (``HANDLERS``), затем ждать
``NOTIFY jobs_<queue>`` или страховочный интервал опроса (1 с для критичной очереди, 10 с для
фоновой — §5.4). Задачи неизвестных типов не выбираются — остаются исполнителю, который их знает
(поэтапный выпуск §17.2). Исключение обработчика — повтор с экспоненциальной задержкой и
джиттером (``backoff``: 1, 2, 4 … с, потолок 5 мин) до ``max_attempts``, затем ``dead`` с записью
ERROR в журнале (событие для алерта; ряд ``control_plane_jobs{status="dead"}``);
``NonRetryableError`` — ``failed`` сразу (001.74). Процесс не умирает от ошибок базы: сбой
выборки или завершения журналируется, подключения пула обновляются (``expire_connections`` —
устаревшие кэши типов после миграций), цикл продолжается после паузы; обрыв подключения LISTEN
обнаруживается и
подключение восстанавливается. Остановка — SIGTERM/SIGINT или событие ``stop`` (тесты).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import random
import signal
import socket
import sys
from collections.abc import Callable, Sequence
from typing import Literal

import asyncpg

from app.config import Settings
from app.db.pool import close_pool, get_pool
from app.jobs import queue as jobs
from app.jobs.handlers import HANDLERS

log = logging.getLogger(__name__)

POLL_INTERVAL: dict[str, float] = {"critical": 1.0, "background": 10.0}
RECOVERY_PAUSE = 1.0  # пауза после ошибки базы перед следующей попыткой
LISTENER_RECONNECT_PAUSE = 1.0

# Повторы (R-46): задержка удваивается с каждой попыткой от BACKOFF_BASE до BACKOFF_CAP, плюс
# джиттер до BACKOFF_JITTER доли интервала — повторы не бьют в базу синхронно.
BACKOFF_BASE = 1.0
BACKOFF_CAP = 300.0
BACKOFF_JITTER = 0.25
# Бюджет ожидания задачи критичного пути — половина p95 Н-13 (10 с): interfaces.md §5.4.
CRITICAL_WAIT_BUDGET_SECONDS = 5.0

Decision = Literal["retry", "dead", "failed"]


def backoff(
    attempt: int,
    *,
    base: float | None = None,
    cap: float | None = None,
    rng: Callable[[], float] = random.random,
) -> float:
    """Задержка перед повтором после ``attempt``-й неудачной попытки (1, 2, 4 … с базы,
    не выше потолка) плюс джиттер ``[0, BACKOFF_JITTER)`` от интервала; ``rng`` — источник
    джиттера (тесты передают детерминированный)."""
    if attempt < 1:
        raise ValueError("номер попытки начинается с 1")
    base = BACKOFF_BASE if base is None else base
    cap = BACKOFF_CAP if cap is None else cap
    delay: float = min(cap, base * 2.0 ** (attempt - 1))
    return float(delay * (1 + BACKOFF_JITTER * rng()))


def decide(exc: BaseException, *, attempts: int, max_attempts: int) -> tuple[Decision, int]:
    """Судьба задачи после исключения обработчика: ``failed`` без повторов, если обработчик
    сказал ``NonRetryableError``; ``dead``, если попытки исчерпаны; иначе ``retry``. Второй
    элемент — номер попытки (для задержки и журнала)."""
    if isinstance(exc, jobs.NonRetryableError):
        return "failed", attempts
    if attempts >= max_attempts:
        return "dead", attempts
    return "retry", attempts


# Ошибки, после которых цикл живёт дальше: любые ошибки PostgreSQL и сети — транзиентные
# (разрыв, перезапуск базы, устаревший кэш типов) и постоянные (отозванное право, опечатка в
# SQL) не различаются: и те и другие журналируются каждую паузу, пока не исправят среду или код;
# счётчик подряд идущих отказов и метрика — 001.68. Ошибки Python (KeyError, TypeError…) не
# глотаются — они означают дефект кода.
RECOVERABLE = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, asyncio.TimeoutError)


def worker_id(queue: str) -> str:
    """Идентификатор исполнителя в ``jobs.locked_by``: хост, pid, очередь."""
    return f"{socket.gethostname()}:{os.getpid()}:{queue}"


async def recover(pool: asyncpg.Pool, exc: BaseException, where: str) -> None:
    """Журналировать ошибку базы и обновить подключения пула: устаревшие кэши типов
    (``cache lookup failed for type`` после пересоздания перечислений) и разорванные сессии
    уходят при возврате в пул, новые подключения создаются заново."""
    log.warning("%s: %s: %s — подключения пула будут обновлены", where, type(exc).__name__, exc)
    await pool.expire_connections()


async def process_one(pool: asyncpg.Pool, job: jobs.Job, worker: str) -> bool:
    """Выполнить выбранную задачу обработчиком её типа; вернуть успех. Обработчик и завершение
    идут на разных подключениях: подключение, испорченное обработчиком (прерванная транзакция),
    не используется для записи статуса. Завершение — от имени ``worker``: если задачу за это
    время перехватили по аренде, запись не проходит (``LookupError`` → предупреждение)."""
    handler = HANDLERS.get(job.type)
    error: str | None = None
    decision: Decision = "failed"
    if handler is None:  # недостижимо при выборке по HANDLERS; защита от гонки реестра
        error = f"неизвестный тип задачи: {job.type}"
        decision, _ = decide(
            RuntimeError(error), attempts=job.attempts, max_attempts=job.max_attempts
        )
    else:
        try:
            async with pool.acquire() as conn:
                await handler(conn, job)
        except Exception as exc:  # обработчик упал — повтор/dead/failed, исполнитель живёт
            error = f"{type(exc).__name__}: {exc}"[:2000]
            decision, _ = decide(exc, attempts=job.attempts, max_attempts=job.max_attempts)
            if (
                decision == "dead"
            ):  # событие для алерта: правило по control_plane_jobs{status="dead"}
                log.error(
                    "задача %s (%s) переведена в dead после %d попыток: %s",
                    job.id,
                    job.type,
                    job.attempts,
                    error,
                )
            elif decision == "retry":
                log.warning(
                    "задача %s (%s): попытка %d из %d не удалась, повтор: %s",
                    job.id,
                    job.type,
                    job.attempts,
                    job.max_attempts,
                    error,
                )
            else:
                log.exception("задача %s (%s) отклонена без повторов", job.id, job.type)
    try:
        async with pool.acquire() as conn:
            if error is None:
                await jobs.complete(conn, job.id, worker)
            elif decision == "retry":
                await jobs.retry(conn, job.id, worker, error, backoff(job.attempts))
            elif decision == "dead":
                await jobs.bury(conn, job.id, worker, error)
            else:
                await jobs.fail(conn, job.id, worker, error)
    except LookupError as exc:
        # Строку изменил кто-то ещё (удалена, переведена вручную, перехвачена по аренде у
        # медленного исполнителя): завершать нечего, это не повод останавливать исполнителя.
        log.warning("задача %s: %s", job.id, exc)
    return error is None


async def run_once(pool: asyncpg.Pool, queue: str, worker: str) -> int:
    """Одна итерация: забирать и выполнять задачи известных типов, пока очередь не опустеет;
    вернуть число выполненных. Ошибки базы поднимаются вызывающему (``run`` их переживает)."""
    processed = 0
    types = tuple(HANDLERS)
    while True:
        async with pool.acquire() as conn:
            job = await jobs.claim(conn, queue, worker, types=types)
        if job is None:
            return processed
        await process_one(pool, job, worker)
        processed += 1


async def connect_listener(
    settings: Settings, queue: str, wakeup: asyncio.Event
) -> asyncpg.Connection:
    """Отдельное подключение LISTEN: уведомление и обрыв подключения будят цикл."""
    listener = await asyncpg.connect(
        settings.pg_dsn_with_password,
        server_settings={"application_name": f"worker:{queue}:listener"},
    )
    await listener.add_listener(jobs.channel(queue), lambda *_: wakeup.set())
    listener.add_termination_listener(lambda *_: wakeup.set())
    return listener


async def run(
    queue: str, *, stop: asyncio.Event | None = None, pool: asyncpg.Pool | None = None
) -> None:
    """Главный цикл исполнителя: LISTEN на канале очереди плюс страховочный опрос.

    ``pool`` — пул процесса; если не передан, берётся общий (``get_pool``) и закрывается при
    выходе; переданный пул принадлежит вызывающему и остаётся открытым.
    """
    if queue not in POLL_INTERVAL:
        raise ValueError(f"неизвестная очередь: {queue}")
    stop = stop or asyncio.Event()
    settings = Settings.load()
    owns_pool = pool is None
    pool = pool or await get_pool(settings)
    worker = worker_id(queue)
    wakeup = asyncio.Event()
    listener: asyncpg.Connection | None = None
    try:
        while not stop.is_set():
            # Сброс — в самом начале витка: уведомление или обрыв, пришедшие во время разбора
            # очереди, не теряются, а взведённый обрывом флаг не превращает паузу переподключения
            # в холостую прокрутку (ревью 001.11, раунд 2).
            wakeup.clear()
            if listener is None or listener.is_closed():
                try:
                    listener = await connect_listener(settings, queue, wakeup)
                    log.info("исполнитель %s слушает %s", worker, jobs.channel(queue))
                except RECOVERABLE as exc:
                    log.warning(
                        "LISTEN недоступен: %s — повтор через %.0f с", exc, LISTENER_RECONNECT_PAUSE
                    )
                    await _wait(stop, wakeup, LISTENER_RECONNECT_PAUSE)
                    continue
            try:
                await run_once(pool, queue, worker)
            except RECOVERABLE as exc:
                await recover(pool, exc, f"исполнитель {worker}")
                await _wait(stop, wakeup, RECOVERY_PAUSE)
                continue
            await _wait(stop, wakeup, POLL_INTERVAL[queue])
    finally:
        if listener is not None and not listener.is_closed():
            await listener.close()
        if owns_pool:
            await close_pool()


async def _wait(stop: asyncio.Event, wakeup: asyncio.Event, seconds: float) -> None:
    """Ждать остановки, пробуждения или таймаута — что раньше."""
    stop_task = asyncio.ensure_future(stop.wait())
    wake_task = asyncio.ensure_future(wakeup.wait())
    try:
        await asyncio.wait(
            {stop_task, wake_task}, timeout=seconds, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for task in (stop_task, wake_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.jobs.worker", description=__doc__)
    parser.add_argument("--queue", choices=sorted(POLL_INTERVAL), required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "info").upper())

    async def serve() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run(args.queue, stop=stop)

    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
