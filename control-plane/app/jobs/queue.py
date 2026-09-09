"""Очередь задач в PostgreSQL (таблица ``jobs``, миграция 090; interfaces.md §5.4).

Постановка — ``INSERT`` в транзакции доменной операции (outbox); уведомление ``NOTIFY
jobs_<queue>`` PostgreSQL доставляет только при коммите этой транзакции. Выборка — ``UPDATE`` по
подзапросу ``FOR UPDATE SKIP LOCKED``: один исполнитель на задачу, попытка засчитывается при
выборке.
Идемпотентность (R-46, §4.4): ключ уникален среди ``pending``/``running`` — повторная постановка
активной задачи возвращает существующую (``Enqueued.created = False``), уведомление не шлётся;
после ``done``/``failed``/``dead`` ключ свободен.
Повторы (001.74): исключение обработчика → ``retry`` (снова ``pending`` с ``run_at`` в будущем и
текстом ошибки) до ``max_attempts``, затем ``bury`` → ``dead``; ``NonRetryableError`` из
обработчика → ``fail`` → терминальный ``failed``. Исполнитель, умерший посреди задачи, оставляет
её ``running``: по истечении аренды (``LEASE``, срок от ``locked_at``) выборка забирает такую
задачу снова — попытка засчитывается, так что бесконечно умирающая задача доходит до ``dead``
(at-least-once §5.8: обработчик, работающий дольше аренды, может быть выполнен дважды —
обработчики идемпотентны по ключу). Завершение фехтуется владельцем: ``complete``/``fail``/
``bury``/``retry`` меняют строку только если её ``locked_by`` — вызывающий исполнитель, так что
медленный «зомби», у которого задачу перехватили по аренде, не закроет и не вернёт в очередь
чужую работу (``LookupError``). Времена ``claimed_at``/``finished_at`` остаются после
завершения — по ним считается ожидание задачи (метрика ``jobs_wait_seconds``).
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

import asyncpg

Queue = Literal["critical", "background"]
QUEUES: tuple[Queue, ...] = ("critical", "background")
STATUSES: tuple[str, ...] = ("pending", "running", "done", "failed", "dead")
DEFAULT_MAX_ATTEMPTS = 3
# Аренда выборки: задача, которую исполнитель держит ``running`` дольше, считается брошенной
# (процесс умер) и выбирается заново. Обработчики короткие (секунды); длинные — своя задача.
LEASE = dt.timedelta(minutes=5)


def channel(queue: str) -> str:
    """Канал LISTEN/NOTIFY очереди (§5.4): ``jobs_critical``, ``jobs_background``."""
    return f"jobs_{queue}"


class NonRetryableError(Exception):
    """Обработчик знает, что повторять бессмысленно (получателя нет, данные противоречивы):
    задача сразу ``failed``, без повторов и без ``dead``."""


class Enqueued(NamedTuple):
    """Результат ``enqueue``: идентификатор задачи и признак ``created`` — создана сейчас
    (``True``) или найдена активная с тем же ключом (``False``)."""

    id: int
    created: bool


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


# FOR UPDATE SKIP LOCKED — один исполнитель на задачу без ожидания чужих блокировок (§5.4).
# Выбираются готовые pending и брошенные running — те, чья аренда ($4 от locked_at) истекла:
# исполнитель умер посреди задачи, и без этого она висела бы running вечно, держа ключ (001.74).
# Попытка засчитывается при каждой выборке, поэтому и брошенная задача доходит до dead.
# $3 — известные исполнителю типы (NULL — любые, пустой массив — ничего): задачи новых типов
# ждут исполнителя нового выпуска, а не падают у старого (наше решение в духе expand/contract;
# см. «Уточнения» задачи 001.11). Запрос статичен: параметры только через $1…$4.
CLAIM_SQL = """
update jobs
   set status = 'running', locked_at = now(), locked_by = $2, attempts = attempts + 1,
       claimed_at = now()
 where id = (
       select id from jobs
        where queue = $1::job_queue
          and ((status = 'pending' and run_at <= now())
               or (status = 'running' and locked_at < now() - $4::interval))
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
) -> Enqueued:
    """Поставить задачу; вернуть ``Enqueued`` (id и признак создания). ``NOTIFY jobs_<queue>`` с
    id уходит при коммите транзакции ``conn`` (PostgreSQL доставляет уведомления только
    закоммиченных транзакций — обёртка «после коммита» не нужна, тест 001.11 это доказывает).

    Дубль ключа среди активных задач — ``ON CONFLICT … DO NOTHING`` по частичному индексу, а не
    ошибка PostgreSQL: транзакция доменной операции остаётся живой, возвращается существующая
    активная задача (постановка «или уже стоит» внутри outbox, §4.4) без уведомления.
    """
    for _ in range(2):  # вторая попытка — если активная задача завершилась между вставкой и чтением
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
        if job_id is not None:
            await conn.execute("select pg_notify($1, $2)", channel(queue), str(job_id))
            return Enqueued(job_id, created=True)
        existing: int | None = await conn.fetchval(
            "select id from jobs where idempotency_key = $1 and status in ('pending', 'running')",
            idempotency_key,
        )
        if existing is not None:
            return Enqueued(existing, created=False)
    raise RuntimeError(f"постановка «{idempotency_key}»: активная задача исчезает между запросами")


async def claim(
    conn: asyncpg.Connection,
    queue: str,
    worker_id: str,
    *,
    types: Sequence[str] | None = None,
    lease: dt.timedelta = LEASE,
) -> Job | None:
    """Забрать одну готовую задачу очереди (``pending`` с ``run_at <= now()`` либо ``running``
    с истёкшей арендой ``lease`` — брошенную умершим исполнителем; самая ранняя по ``run_at,
    id``), пометив её ``running`` за ``worker_id``; ``types`` ограничивает выборку известными
    типами (``None`` — любые, пустой набор — ничего); ``None`` в ответе — очередь пуста или всё
    занято другими."""
    known = None if types is None else list(types)
    record = await conn.fetchrow(CLAIM_SQL, queue, worker_id, known, lease)
    return Job.from_record(record) if record is not None else None


async def complete(conn: asyncpg.Connection, job_id: int, owner: str) -> None:
    """Успех: ``running`` у ``owner`` → ``done``, блокировка снята; ключ идемпотентности
    освобождается."""
    await _finish(conn, job_id, owner, "done", None)


async def fail(conn: asyncpg.Connection, job_id: int, owner: str, error: str) -> None:
    """Отказ без повторов: ``running`` у ``owner`` → ``failed`` с текстом ошибки (терминально)."""
    await _finish(conn, job_id, owner, "failed", error)


async def bury(conn: asyncpg.Connection, job_id: int, owner: str, error: str) -> None:
    """Попытки исчерпаны: ``running`` у ``owner`` → ``dead`` с текстом последней ошибки (DLQ,
    R-46); алерт — по ряду ``control_plane_jobs{status="dead"}`` и записи ERROR исполнителя."""
    await _finish(conn, job_id, owner, "dead", error)


async def retry(
    conn: asyncpg.Connection, job_id: int, owner: str, error: str, delay_s: float
) -> None:
    """Повтор: ``running`` у ``owner`` → ``pending`` с ``run_at = now() + delay_s`` и текстом
    ошибки; блокировка снята, попытка уже засчитана при выборке. Строка не ``running`` или её
    держит другой исполнитель (перехват по аренде) — ``LookupError``."""
    updated = await conn.execute(
        "update jobs set status = 'pending', last_error = $3, "
        "run_at = now() + make_interval(secs => $4), locked_at = null, locked_by = null "
        "where id = $1 and status = 'running' and locked_by = $2",
        job_id,
        owner,
        error,
        delay_s,
    )
    if updated != "UPDATE 1":
        raise LookupError(f"задача {job_id} не выполняется исполнителем {owner} — повторять нечего")


async def _finish(
    conn: asyncpg.Connection, job_id: int, owner: str, status: str, error: str | None
) -> None:
    updated = await conn.execute(
        "update jobs set status = $3::job_status, last_error = $4, locked_at = null, "
        "locked_by = null, finished_at = now() "
        "where id = $1 and status = 'running' and locked_by = $2",
        job_id,
        owner,
        status,
        error,
    )
    if updated != "UPDATE 1":
        raise LookupError(f"задача {job_id} не выполняется исполнителем {owner} — завершать нечего")


async def wait_seconds(conn: asyncpg.Connection, window: dt.timedelta) -> dict[str, float]:
    """Наибольшее время ожидания задачи по очередям: среди выбранных за последние ``window`` —
    от готовности (``run_at``) до выборки (``claimed_at``; строки в ожидании повтора, у которых
    ``run_at`` перенесён позже прошлой выборки, не считаются), и среди **ещё не выбранных**
    готовых (``pending``, ``run_at <= now()``) — от готовности до сейчас: стоящая очередь без
    исполнителя видна как растущее ожидание, а не как 0. Очередь без задач — 0.0."""
    rows = await conn.fetch(
        "select queue::text, max(wait) as wait from ("
        "  select queue, extract(epoch from claimed_at - run_at) as wait from jobs"
        "   where claimed_at >= now() - $1::interval and claimed_at >= run_at"
        "  union all"
        "  select queue, extract(epoch from now() - run_at) from jobs"
        "   where status = 'pending' and run_at <= now()"
        ") waits group by 1",
        window,
    )
    waits: dict[str, float] = dict.fromkeys(QUEUES, 0.0)
    for row in rows:
        waits[row["queue"]] = max(0.0, float(row["wait"]))
    return waits


async def depth(conn: asyncpg.Connection) -> dict[tuple[str, str], int]:
    """Число задач по (очередь, статус) — для ``/metrics``; нулевые пары включены."""
    rows = await conn.fetch("select queue::text, status::text, count(*) from jobs group by 1, 2")
    counts: dict[tuple[str, str], int] = {
        (queue, status): 0 for queue in QUEUES for status in STATUSES
    }
    for row in rows:
        counts[(row["queue"], row["status"])] = row["count"]
    return counts
