"""Обработчики обслуживания хранения (data-model.md §4.5; постановка §16.2; R-22, R-48, Н-21,
Н-22): ``partitions.ensure``, ``partitions.drop_expired``, ``retention.purge``.

Задача 001.33: три типа зарегистрированы заглушками, которые отказывают без повторов
(``NonRetryableError`` → ``failed`` с причиной): «успех» задачи удаления по срокам (§16.2, AC-39)
без единого удаления читался бы как выполненное обязательство, а отказ виден в очереди. Вызовы
функций
``SECURITY DEFINER`` миграции 080 (``ensure_partitions(7)``, ``drop_expired_partitions()``),
удаление по срокам из таблиц без партиций (в том числе ``audit_log`` — только функцией
ретенции, AC-39) и расписание (партиции 01:00 UTC, ретенция 02:00 UTC) — 001.37.

До 001.37 партиции обслуживает обработчик ``ensure_partitions`` (``handlers/partitions.py``,
001.14): он один делает и создание, и удаление, и он один стоит в ``scheduler.SCHEDULE``.
Типы этого модуля в расписание не ставятся, пока они заглушки: задача ``partitions.ensure``,
завершившаяся «успехом» без единой партиции, оставила бы журналы без места для вставки. Страж —
``tests/unit/jobs/test_handlers.py``; 001.37 переводит расписание на эти типы и снимает
``ensure_partitions`` вместе со стражем.
"""

from __future__ import annotations

import asyncpg

from app.jobs.queue import Job, NonRetryableError

PARTITIONS_ENSURE = "partitions.ensure"
PARTITIONS_DROP_EXPIRED = "partitions.drop_expired"
RETENTION_PURGE = "retention.purge"
# Типы модуля одним набором: реестр и страж расписания читают его, а не переписывают.
MAINTENANCE_TYPES: frozenset[str] = frozenset(
    (PARTITIONS_ENSURE, PARTITIONS_DROP_EXPIRED, RETENTION_PURGE)
)


def _not_implemented(type_: str) -> NonRetryableError:
    return NonRetryableError(f"{type_}: заглушка 001.33, обслуживание хранения — 001.37")


async def ensure(conn: asyncpg.Connection, job: Job) -> None:
    """Заглушка: партиции на семь суток вперёд создаёт ``ensure_partitions`` (001.14); здесь —
    отказ без повторов (001.37)."""
    raise _not_implemented(PARTITIONS_ENSURE)


async def drop_expired(conn: asyncpg.Connection, job: Job) -> None:
    """Заглушка: просроченные партиции удаляет ``ensure_partitions`` (001.14); здесь — отказ без
    повторов (001.37)."""
    raise _not_implemented(PARTITIONS_DROP_EXPIRED)


async def purge(conn: asyncpg.Connection, job: Job) -> None:
    """Заглушка: удаление по срокам §4.5 из таблиц без партиций — 001.37; отказ без повторов."""
    raise _not_implemented(RETENTION_PURGE)
