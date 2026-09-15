"""Реестр обработчиков очереди после задачи 001.33: тип `limits.check` и три типа обслуживания
хранения зарегистрированы (иначе задачи копились бы в `pending` — исполнитель выбирает только
известные типы, §5.4), заглушки отказывают без повторов и не трогают подключение — зелёный
«успех» отзыва или удаления, которых не было, хуже красной задачи, — и ни один из них не стоит
в расписании планировщика, пока он заглушка."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any

import pytest
from app.jobs import scheduler
from app.jobs.handlers import HANDLERS
from app.jobs.handlers.limits import LIMITS_CHECK, check_limits
from app.jobs.handlers.maintenance import (
    MAINTENANCE_TYPES,
    PARTITIONS_DROP_EXPIRED,
    PARTITIONS_ENSURE,
    RETENTION_PURGE,
    drop_expired,
    ensure,
    purge,
)
from app.jobs.handlers.partitions import ensure_partitions
from app.jobs.queue import Job, NonRetryableError


class NoConnection:
    """Подключение, которого нет: заглушка, обратившаяся к нему, валит тест."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"заглушка обратилась к подключению: {name}")


def job(type_: str) -> Job:
    return Job(
        id=1,
        queue="background",
        type=type_,
        payload={"node_id": str(uuid.UUID(int=1)), "user_ids": [str(uuid.UUID(int=2))]},
        idempotency_key=f"test:{type_}",
        run_at=dt.datetime(2026, 9, 10, tzinfo=dt.UTC),
        attempts=1,
        max_attempts=3,
        status="running",
    )


def test_the_new_job_types_are_registered_with_their_own_handlers() -> None:
    """Именно эти обработчики, а не любые под этими ключами."""
    assert LIMITS_CHECK == "limits.check"
    assert MAINTENANCE_TYPES == {"partitions.ensure", "partitions.drop_expired", "retention.purge"}
    assert HANDLERS[LIMITS_CHECK] is check_limits
    assert HANDLERS[PARTITIONS_ENSURE] is ensure
    assert HANDLERS[PARTITIONS_DROP_EXPIRED] is drop_expired
    assert HANDLERS[RETENTION_PURGE] is purge
    assert HANDLERS["ensure_partitions"] is ensure_partitions, "обработчик 001.14 остаётся"


async def test_the_stubs_refuse_without_retries_and_without_touching_the_connection() -> None:
    """Отказ без повторов — задача `failed` с причиной, в которой назван тип; не `dead`
    (повторы бессмысленны) и не «успех» (работа не сделана)."""
    for type_, handler in (
        (LIMITS_CHECK, check_limits),
        (PARTITIONS_ENSURE, ensure),
        (PARTITIONS_DROP_EXPIRED, drop_expired),
        (RETENTION_PURGE, purge),
    ):
        with pytest.raises(NonRetryableError, match=re.escape(f"{type_}: заглушка 001.33")):
            await handler(NoConnection(), job(type_))


def test_the_stub_types_stay_off_the_schedule_until_they_are_real() -> None:
    """`partitions.ensure`, завершившийся «успехом» без единой партиции, оставил бы журналы без
    места для вставки: до 001.37 партиции обслуживает `ensure_partitions`, и только он стоит в
    расписании. 001.37 переводит расписание на новые типы и снимает этот страж вместе с
    заглушками."""
    scheduled = {periodic.type for periodic in scheduler.SCHEDULE}
    assert not scheduled & (MAINTENANCE_TYPES | {LIMITS_CHECK}), scheduled
    assert "ensure_partitions" in scheduled
