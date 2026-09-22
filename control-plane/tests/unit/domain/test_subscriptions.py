"""Правила подписки, которые не требуют базы (задача 001.22; §4.11): начало нового периода при
продлении и остаток лимита. Обе функции чистые намеренно — это правила, а не запросы, и денежное
правило должно проверяться без стенда (`tdd-strict` описания задачи).

Сигнатуры службы закреплены текстом отдельно: их правят 001.20 (коды), 001.35 (лимиты) и 001.16
(кабинет), и молчаливое переименование параметра сломало бы вызывающих не здесь, а у них.
Совпадение набора состояний с базой и поведение переходов на живых данных — сквозной тест
``tests/e2e/test_subscriptions.py``.
"""

from __future__ import annotations

import datetime as dt
import inspect
from typing import Any

import pytest
from app.domain.subscriptions import (
    SubscriptionService,
    SubscriptionState,
    next_period_start,
    remaining_of,
)

NOW = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)
DAY = dt.timedelta(days=1)


def test_tc_unit_01_next_period_start() -> None:
    """TC-UNIT-01 описания задачи, дословно: активная подписка с датой окончания завтра даёт
    завтра, истёкшая — текущий момент."""
    assert next_period_start("active", NOW + DAY, NOW) == NOW + DAY
    assert next_period_start("expired", NOW - DAY, NOW) == NOW


@pytest.mark.parametrize("state", ["active", "suspended_quota", "suspended_admin"])
def test_a_suspended_subscription_keeps_its_remaining_days(state: SubscriptionState) -> None:
    """Состояния, снимающие доступ, продлеваются как активные: доступ снят лимитом или
    администратором, а срок продолжает идти. Считать их истёкшими значило бы отбирать при
    продлении ещё и оставшиеся дни — §4.11 такого не говорит ни о лимите, ни о блокировке."""
    assert next_period_start(state, NOW + 5 * DAY, NOW) == NOW + 5 * DAY


def test_a_period_that_already_ended_starts_from_now() -> None:
    """Дата окончания в прошлом при ещё не переведённом состоянии (планировщик 001.83 ходит раз
    в минуту, и между истечением и переводом есть окно) даёт текущий момент: от прошлой даты
    новый период начался бы задним числом и часть срока сгорела бы сразу."""
    assert next_period_start("active", NOW - DAY, NOW) == NOW
    assert next_period_start("active", NOW, NOW) == NOW, "ровно истёкший — тоже от текущего"


def test_without_a_subscription_the_period_starts_from_now() -> None:
    assert next_period_start("none", None, NOW) == NOW


@pytest.mark.parametrize(
    ("limit", "used", "left"),
    [
        (100, 40, 60),
        (100, 100, 0),
        (100, 140, 0),  # перерасход Н-17б: разность отрицательна, остаток — ноль
        (0, 0, 0),
        (None, 10**12, None),  # Unlimited: остатка не существует, а не «ноль»
    ],
)
def test_remaining_never_goes_below_zero(limit: int | None, used: int, left: int | None) -> None:
    """Остаток — «лимит минус израсходованное», но не меньше нуля: списание за перерасход
    ограничено Н-17б, а ``remaining`` кабинета и статистики объявлены ``ge=0`` — отрицательное
    значение pydantic отверг бы при сборке ответа. ``None`` — только Unlimited."""
    assert remaining_of(limit, used) == left


@pytest.mark.parametrize(
    ("method", "signature"),
    [
        (
            SubscriptionService.activate,
            "(self, user_id: 'uuid.UUID', plan_id: 'uuid.UUID', source: 'PeriodSource', *, "
            "source_id: 'uuid.UUID | None' = None) -> 'PeriodId'",
        ),
        (
            SubscriptionService.renew,
            "(self, user_id: 'uuid.UUID', source: 'PeriodSource', *, "
            "source_id: 'uuid.UUID | None' = None) -> 'PeriodId'",
        ),
        (
            SubscriptionService.change_plan,
            "(self, user_id: 'uuid.UUID', plan_id: 'uuid.UUID', actor: 'uuid.UUID') -> 'None'",
        ),
        (
            SubscriptionService.add_traffic,
            "(self, user_id: 'uuid.UUID', bytes: 'int', actor: 'uuid.UUID | None', "
            "reason: 'str | None', *, ref_key: 'str | None' = None) -> 'None'",
        ),
        (SubscriptionService.expire, "(self, user_id: 'uuid.UUID') -> 'None'"),
        (
            SubscriptionService.set_state,
            "(self, user_id: 'uuid.UUID', state: 'SubscriptionState', reason: 'str') -> 'None'",
        ),
        (SubscriptionService.remaining, "(self, user_id: 'uuid.UUID') -> 'int | None'"),
        (
            next_period_start,
            "(state: 'SubscriptionState', period_end: 'dt.datetime | None', "
            "now: 'dt.datetime') -> 'dt.datetime'",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else value.__qualname__,
)
def test_the_declared_signature_is_the_one_callers_will_find(method: Any, signature: str) -> None:
    """Позиционные параметры — те же, что объявила 001.21; добавленные 001.22 ``source_id`` и
    ``ref_key`` только именованные и со значением по умолчанию, поэтому прежние вызовы целы."""
    assert str(inspect.signature(method)) == signature
