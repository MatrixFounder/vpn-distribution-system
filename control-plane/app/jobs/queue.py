"""Очередь задач в PostgreSQL (таблица ``jobs``, миграция 090; interfaces.md §5.4).

Постановка — ``INSERT`` в транзакции доменной операции (outbox); уведомление ``NOTIFY
jobs_<queue>`` PostgreSQL доставляет только при коммите этой транзакции. Выборка — ``UPDATE`` по
подзапросу ``FOR UPDATE SKIP LOCKED``: один исполнитель на задачу, попытка засчитывается при
выборке.
Идемпотентность (R-46, §4.4): ключ уникален среди ``pending``/``running`` — повторная постановка
активной задачи отклоняется ``DuplicateJobError``; после ``done``/``failed`` ключ свободен.
Повторы и ``dead`` — задача 001.74: здесь ``fail`` терминален.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg

Queue = Literal["critical", "background"]
QUEUES: tuple[Queue, ...] = ("critical", "background")
STATUSES: tuple[str, ...] = ("pending", "running", "done", "failed", "dead")
DEFAULT_MAX_ATTEMPTS = 3


def channel(queue: str) -> str:
    """Канал LISTEN/NOTIFY очереди (§5.4): ``jobs_critical``, ``jobs_background``."""
    return f"jobs_{queue}"


class DuplicateJobError(Exception):
    """Задача с таким ключом идемпотентности уже ожидает или выполняется (R-46)."""

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(f"задача с ключом «{idempotency_key}» уже в очереди")
        self.idempotency_key = idempotency_key


@dataclass(frozen=True, slots=True)
class Job:
    """Строка ``jobs`` в момент выборки."""

    id: int
    queue: str
    type: str
    payload: dict[str, Any]
    idempotency_key: str
    run_at: dt.datetime
    attempts: int
    max_attempts: int
    status: str

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> Job:
        payload = record["payload"]
        return cls(
            id=record["id"],
            queue=record["queue"],
            type=record["type"],
            payload=json.loads(payload) if isinstance(payload, str) else dict(payload),
            idempotency_key=record["idempotency_key"],
            run_at=record["run_at"],
            attempts=record["attempts"],
            max_attempts=record["max_attempts"],
            status=record["status"],
        )


# FOR UPDATE SKIP LOCKED — один исполнитель на задачу без ожидания чужих блокировок (§5.4);
# попытка засчитывается при выборке, чтобы упавший посреди работы исполнитель её не терял (001.74).
# $3 — известные исполнителю типы (NULL — любые, пустой массив — ничего): задачи новых типов
# ждут исполнителя нового выпуска, а не падают у старого (наше решение в духе expand/contract;
# см. «Уточнения» задачи 001.11). Запрос статичен: параметры только через $1/$2/$3.
CLAIM_SQL = """
update jobs
   set status = 'running', locked_at = now(), locked_by = $2, attempts = attempts + 1
 where id = (
       select id from jobs
        where queue = $1::job_queue and status = 'pending' and run_at <= now()
          and ($3::text[] is null or type = any($3::text[]))
        order by run_at, id
          for update skip locked
        limit 1)
returning id, queue, type, payload, idempotency_key, run_at, attempts, max_attempts, status
"""


async def enqueue(
    conn: asyncpg.Connection,
    queue: str,
    type: str,  # noqa: A002 — имя поля контракта §5.4
    payload: dict[str, Any],
    idempotency_key: str,
    run_at: dt.datetime | None = None,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> int:
    """Поставить задачу; вернуть её id. ``NOTIFY jobs_<queue>`` с id уходит при коммите
    транзакции ``conn`` (PostgreSQL доставляет уведомления только закоммиченных транзакций).

    Дубль ключа среди активных задач — ``ON CONFLICT … DO NOTHING`` по частичному индексу, а не
    ошибка PostgreSQL: транзакция доменной операции остаётся живой, ``DuplicateJobError`` можно
    перехватить и продолжить (постановка «или уже стоит» внутри outbox).
    """
    job_id: int | None = await conn.fetchval(
        "insert into jobs (queue, type, payload, idempotency_key, run_at, max_attempts) "
        "values ($1::job_queue, $2, $3::jsonb, $4, coalesce($5, now()), $6) "
        "on conflict (idempotency_key) where status in ('pending', 'running') do nothing "
        "returning id",
        queue,
        type,
        json.dumps(payload),
        idempotency_key,
        run_at,
        max_attempts,
    )
    if job_id is None:
        raise DuplicateJobError(idempotency_key)
    await conn.execute("select pg_notify($1, $2)", channel(queue), str(job_id))
    return job_id


async def claim(
    conn: asyncpg.Connection,
    queue: str,
    worker_id: str,
    *,
    types: Sequence[str] | None = None,
) -> Job | None:
    """Забрать одну готовую задачу очереди (``pending``, ``run_at <= now()``, самая ранняя по
    ``run_at, id``), пометив её ``running`` за ``worker_id``; ``types`` ограничивает выборку
    известными типами (``None`` — любые, пустой набор — ничего); ``None`` в ответе — очередь
    пуста или всё занято другими."""
    known = None if types is None else list(types)
    record = await conn.fetchrow(CLAIM_SQL, queue, worker_id, known)
    return Job.from_record(record) if record is not None else None


async def complete(conn: asyncpg.Connection, job_id: int) -> None:
    """Успех: ``running`` → ``done``, блокировка снята; ключ идемпотентности освобождается."""
    await _finish(conn, job_id, "done", None)


async def fail(conn: asyncpg.Connection, job_id: int, error: str) -> None:
    """Ошибка: ``running`` → ``failed`` с текстом ошибки (терминально до 001.74)."""
    await _finish(conn, job_id, "failed", error)


async def _finish(conn: asyncpg.Connection, job_id: int, status: str, error: str | None) -> None:
    updated = await conn.execute(
        "update jobs set status = $2::job_status, last_error = $3, locked_at = null, "
        "locked_by = null where id = $1 and status = 'running'",
        job_id,
        status,
        error,
    )
    if updated != "UPDATE 1":
        raise LookupError(f"задача {job_id} не выполняется — завершать нечего")


async def depth(conn: asyncpg.Connection) -> dict[tuple[str, str], int]:
    """Число задач по (очередь, статус) — для ``/metrics``; нулевые пары включены."""
    rows = await conn.fetch("select queue::text, status::text, count(*) from jobs group by 1, 2")
    counts: dict[tuple[str, str], int] = {
        (queue, status): 0 for queue in QUEUES for status in STATUSES
    }
    for row in rows:
        counts[(row["queue"], row["status"])] = row["count"]
    return counts
