"""Обслуживание партиций (§4.5; функции миграции 080): создание партиций на семь суток вперёд и
удаление просроченных. Вызывается планировщиком раз в час (``SCHEDULE``); первый запуск нужен уже
задаче 001.14 — ``auth_events`` партиционирована по суткам, без партиции вставка отклоняется.
Удаление по срокам и остальные периодические задачи — 001.37."""

from __future__ import annotations

import logging

import asyncpg

from app.jobs.queue import Job

log = logging.getLogger(__name__)


async def ensure_partitions(conn: asyncpg.Connection, job: Job) -> None:
    """``ensure_partitions(7)`` и ``drop_expired_partitions()`` под ``app_rw`` (SECURITY
    DEFINER, миграция 080)."""
    created = await conn.fetchval("select ensure_partitions(7)")
    dropped = await conn.fetchval("select drop_expired_partitions()")
    log.info("партиции: создано %s, удалено %s", created, dropped)
