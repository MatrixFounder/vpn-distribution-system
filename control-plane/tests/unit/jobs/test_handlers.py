"""Реестр обработчиков очереди после задач 001.33 и 001.21: `limits.check`, три типа обслуживания
хранения и два типа подписки зарегистрированы (иначе задачи копились бы в `pending` —
исполнитель выбирает только известные типы, §5.4), заглушки отказывают без повторов и не трогают
подключение — зелёный «успех» отзыва, удаления или истечения, которых не было, хуже красной
задачи, — и ни один из них не стоит в расписании планировщика, пока он заглушка."""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import uuid
from typing import Any

import pytest
from app.jobs import scheduler
from app.jobs.handlers import HANDLERS
from app.jobs.handlers.composition import (
    PUBLISH_NODE,
    PUBLISH_USER,
    node_of,
    publish_node,
    publish_user,
    user_of,
)
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
from app.jobs.handlers.subscriptions import (
    SUBSCRIPTION_EXPIRE,
    SUBSCRIPTION_NOTIFY_EXPIRING,
    SUBSCRIPTION_TYPES,
    expire,
    notify_expiring,
)
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
    assert SUBSCRIPTION_TYPES == {"subscription.expire", "subscription.notify_expiring"}
    assert HANDLERS[SUBSCRIPTION_EXPIRE] is expire
    assert HANDLERS[SUBSCRIPTION_NOTIFY_EXPIRING] is notify_expiring
    assert HANDLERS["ensure_partitions"] is ensure_partitions, "обработчик 001.14 остаётся"


async def test_the_stubs_refuse_without_retries_and_without_touching_the_connection() -> None:
    """Отказ без повторов — задача `failed` с причиной, в которой назван тип; не `dead`
    (повторы бессмысленны) и не «успех» (работа не сделана)."""
    for type_, handler, task in (
        (LIMITS_CHECK, check_limits, "001.33"),
        (PARTITIONS_ENSURE, ensure, "001.33"),
        (PARTITIONS_DROP_EXPIRED, drop_expired, "001.33"),
        (RETENTION_PURGE, purge, "001.33"),
        (SUBSCRIPTION_EXPIRE, expire, "001.21"),
        (SUBSCRIPTION_NOTIFY_EXPIRING, notify_expiring, "001.21"),
    ):
        with pytest.raises(NonRetryableError, match=re.escape(f"{type_}: заглушка {task}")):
            await handler(NoConnection(), job(type_))


def test_the_stub_types_stay_off_the_schedule_until_they_are_real() -> None:
    """`partitions.ensure`, завершившийся «успехом» без единой партиции, оставил бы журналы без
    места для вставки: до 001.37 партиции обслуживает `ensure_partitions`, и только он стоит в
    расписании. 001.37 переводит расписание на новые типы и снимает этот страж вместе с
    заглушками. `subscription.expire` (раз в минуту) и `subscription.notify_expiring` (раз в час)
    ставит в расписание 001.83 — заглушка в расписании отчитывалась бы об истечении, которого не
    было."""
    scheduled = {periodic.type for periodic in scheduler.SCHEDULE}
    assert not scheduled & (MAINTENANCE_TYPES | SUBSCRIPTION_TYPES | {LIMITS_CHECK}), scheduled
    assert "ensure_partitions" in scheduled


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"user_id": "не-uuid"},
        {"user_id": None},
        {"node_id": str(uuid.UUID(int=1))},
        [],
        ["user_id"],
        "user_id",
        5,
    ],
    ids=["empty", "not-a-uuid", "null", "wrong-key", "array", "array-of-key", "string", "number"],
)
async def test_a_publication_without_a_user_is_refused_without_retries(payload: Any) -> None:
    """Полезная нагрузка `composition.publish_user` — `{"user_id": …}` (001.29). Негодная — ошибка
    постановки, а не сбой среды: повтор её не исправит, задача сразу `failed`, к подключению
    обработчик не обращается. Колонка `jobs.payload` объектом себя не ограничивает, поэтому
    нагрузка — не объект (массив, строка, число) — тот же отказ, а не `TypeError` с повторами."""
    broken = dataclasses.replace(job(PUBLISH_USER), payload=payload)
    with pytest.raises(NonRetryableError, match=re.escape(f"{PUBLISH_USER}: нет user_id")):
        await publish_user(NoConnection(), broken)
    assert user_of(
        dataclasses.replace(job(PUBLISH_USER), payload={"user_id": str(uuid.UUID(int=7))})
    ) == uuid.UUID(int=7)


def test_the_publication_types_are_the_production_names() -> None:
    """Типы задач публикации — имена §5.4, и под ними — свои обработчики (001.29b). Тесты
    ставят задачи под подменёнными типами (``tests/conftest.py``), а исполнитель стенда знает
    только эти: опечатка в имени оставила бы задачи стоять."""
    assert (PUBLISH_USER, PUBLISH_NODE) == ("composition.publish_user", "composition.publish_node")
    assert HANDLERS[PUBLISH_USER] is publish_user
    assert HANDLERS[PUBLISH_NODE] is publish_node


@pytest.mark.parametrize(
    "payload",
    [{}, {"node_id": "не-uuid"}, {"node_id": None}, {"user_id": str(uuid.UUID(int=1))}, [], 5],
    ids=["empty", "not-a-uuid", "null", "wrong-key", "array", "number"],
)
async def test_a_node_publication_without_a_node_is_refused_without_retries(payload: Any) -> None:
    """Полезная нагрузка ``composition.publish_node`` — ``{"node_id": …}`` (001.29b); негодная —
    ``NonRetryableError`` без обращения к подключению, как у ``composition.publish_user``."""
    broken = dataclasses.replace(job(PUBLISH_NODE), payload=payload)
    with pytest.raises(NonRetryableError, match=re.escape(f"{PUBLISH_NODE}: нет node_id")):
        await publish_node(NoConnection(), broken)
    assert node_of(
        dataclasses.replace(job(PUBLISH_NODE), payload={"node_id": str(uuid.UUID(int=9))})
    ) == (uuid.UUID(int=9), None)


def test_a_node_batch_continues_after_its_user() -> None:
    """Продолжение состава ноды несёт ``"after"`` — последнего пользователя прежней порции
    (001.29b, порции); не UUID — ``NonRetryableError``."""
    node, user = str(uuid.UUID(int=9)), str(uuid.UUID(int=3))
    batch = dataclasses.replace(job(PUBLISH_NODE), payload={"node_id": node, "after": user})
    assert node_of(batch) == (uuid.UUID(int=9), uuid.UUID(int=3))
    broken = dataclasses.replace(job(PUBLISH_NODE), payload={"node_id": node, "after": "x"})
    with pytest.raises(NonRetryableError, match="after"):
        node_of(broken)
