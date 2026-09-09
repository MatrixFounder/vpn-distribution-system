"""Модульные проверки очереди задач без базы (задача 001.11): разбор строки, каналы, статичность
SQL выборки (FOR UPDATE SKIP LOCKED — критерий приёмки), слоты периодических задач."""

from __future__ import annotations

import datetime as dt

from app.jobs import queue as jobs
from app.jobs import scheduler


def test_claim_sql_is_static_and_skips_locked() -> None:
    assert "for update skip locked" in jobs.CLAIM_SQL
    assert "status = 'pending'" in jobs.CLAIM_SQL and "run_at <= now()" in jobs.CLAIM_SQL
    assert "attempts = attempts + 1" in jobs.CLAIM_SQL, "попытка засчитывается при выборке"
    assert "{" not in jobs.CLAIM_SQL and "%" not in jobs.CLAIM_SQL, "без форматирования строк"


def test_channel_names() -> None:
    assert [jobs.channel(q) for q in jobs.QUEUES] == ["jobs_critical", "jobs_background"]


def test_schedule_maintains_partitions_hourly() -> None:
    """001.14: партиции на неделю вперёд обслуживает планировщик раз в час; тип задачи
    зарегистрирован у исполнителя (иначе задача останется pending)."""
    from app.jobs.handlers import HANDLERS

    periodic = {p.name: p for p in scheduler.SCHEDULE}["ensure_partitions"]
    assert periodic.interval == dt.timedelta(hours=1) and periodic.queue == "background"
    assert periodic.type in HANDLERS


def test_periodic_slot_and_key() -> None:
    periodic = scheduler.Periodic("aggregate", dt.timedelta(hours=1), "background", "noop", {})
    t0 = dt.datetime(2026, 9, 9, 10, 0, tzinfo=dt.UTC)
    assert periodic.idempotency_key(t0) == periodic.idempotency_key(t0 + dt.timedelta(minutes=59))
    assert periodic.idempotency_key(t0) != periodic.idempotency_key(t0 + dt.timedelta(hours=1))
    assert periodic.idempotency_key(t0).startswith("aggregate:")
