"""Обработчик ``limits.check`` — сверка расхода пользователя с лимитом после принятого отчёта
(постановка §4.11; interfaces.md §5.4: критичная очередь, бюджет Н-13 на отзыв).

Задача 001.33: тип зарегистрирован, чтобы очередь знала его уже сейчас, а обработчик отказывает
без повторов (``NonRetryableError`` → задача ``failed`` с причиной): между 001.77, которая ставит
задачу, и 001.35, которая её реализует, зелёный «успех» означал бы отзыв, которого не было, —
отказ виден в очереди и в метриках, тишина — нет. Полезная нагрузка — ``{"node_id",
"user_ids"}`` одна на отчёт, ключ идемпотентности ``limits:{node_id}:{counter_epoch}:{report_seq}``
(interfaces.md §5.4); вызов ``LimitsService.check(conn, user_ids)`` в очереди ``critical`` —
001.35; постановку задачи в транзакции приёма отчёта (outbox §5.4) вводит 001.77.
"""

from __future__ import annotations

import asyncpg

from app.jobs.queue import Job, NonRetryableError

LIMITS_CHECK = "limits.check"


async def check_limits(conn: asyncpg.Connection, job: Job) -> None:
    """Заглушка: лимит не сверяется — задача отказывает без повторов, а не «выполняется»."""
    raise NonRetryableError(f"{LIMITS_CHECK}: заглушка 001.33, проверка лимита не реализована")
