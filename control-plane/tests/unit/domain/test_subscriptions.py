"""Заглушка службы подписок (задача 001.21, критерий «сигнатуры объявлены»). Сигнатуры
закреплены текстом: логика 001.22, коды 001.20, лимиты 001.35 и кабинет 001.16 правят этот файл
и рассчитывают на объявленные имена и параметры. Проверяются три свойства, которые заглушка
обязана нести и после реализации: сигнатуры, публикация состава на каждом переходе и отсутствие
обращений к базе (пул — часовой). Совпадение набора состояний с базой — сквозной тест
``tests/e2e/test_subscriptions.py``: перечисление живёт в PostgreSQL, а не в копии здесь.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any

import pytest
from app.accounting.stats import STUB_STATS
from app.domain.composition import CompositionService
from app.domain.plans import STUB_PLAN
from app.domain.subscriptions import (
    STUB_LIMIT_BYTES,
    STUB_PERIOD_ID,
    STUB_REMAINING_BYTES,
    STUB_USED_BILLABLE_BYTES,
    SubscriptionService,
)

USER = uuid.UUID("00000000-0000-7000-8000-0000000000d1")
PLAN = uuid.UUID("00000000-0000-7000-8000-0000000000d2")
ACTOR = uuid.UUID("00000000-0000-7000-8000-0000000000d3")


class Sentinel:
    """Пул, которого нет: любое обращение заглушки к базе — провал."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"заглушка обратилась к базе: {name}")


class RecordingComposition(CompositionService):
    """Поток состава, запоминающий, за кого его разбудили."""

    def __init__(self) -> None:
        super().__init__(Sentinel())
        self.published: list[uuid.UUID] = []

    async def publish_user(self, user_id: uuid.UUID) -> None:
        self.published.append(user_id)


def service() -> tuple[SubscriptionService, RecordingComposition]:
    composition = RecordingComposition()
    return SubscriptionService(Sentinel(), composition), composition


@pytest.mark.parametrize(
    ("method", "signature"),
    [
        (
            SubscriptionService.activate,
            "(self, user_id: 'uuid.UUID', plan_id: 'uuid.UUID', "
            "source: 'PeriodSource') -> 'PeriodId'",
        ),
        (
            SubscriptionService.renew,
            "(self, user_id: 'uuid.UUID', source: 'PeriodSource') -> 'PeriodId'",
        ),
        (
            SubscriptionService.change_plan,
            "(self, user_id: 'uuid.UUID', plan_id: 'uuid.UUID', actor: 'uuid.UUID') -> 'None'",
        ),
        (
            SubscriptionService.add_traffic,
            "(self, user_id: 'uuid.UUID', bytes: 'int', actor: 'uuid.UUID | None', "
            "reason: 'str | None') -> 'None'",
        ),
        (SubscriptionService.expire, "(self, user_id: 'uuid.UUID') -> 'None'"),
        (
            SubscriptionService.set_state,
            "(self, user_id: 'uuid.UUID', state: 'SubscriptionState', reason: 'str') -> 'None'",
        ),
        (SubscriptionService.remaining, "(self, user_id: 'uuid.UUID') -> 'int | None'"),
    ],
    ids=lambda value: value if isinstance(value, str) else value.__qualname__,
)
def test_the_declared_signature_is_the_one_logic_tasks_will_find(
    method: Any, signature: str
) -> None:
    assert str(inspect.signature(method)) == signature


async def test_every_transition_wakes_the_composition_stream() -> None:
    """Состояние подписки едет на ноды потоком состава (§3.4, §4.11): переход, не разбудивший
    поток, не даёт ни отзыва в пределах Н-14, ни восстановления доступа. Свойство закреплено на
    заглушке, чтобы 001.22 не потеряла его, добавляя запись в базу."""
    subscriptions, composition = service()
    await subscriptions.activate(USER, PLAN, "redeem")
    await subscriptions.renew(USER, "admin")
    await subscriptions.change_plan(USER, PLAN, ACTOR)
    await subscriptions.add_traffic(USER, 1024, ACTOR, "докупил трафик")
    await subscriptions.expire(USER)
    await subscriptions.set_state(USER, "suspended_admin", "нарушение AUP")
    assert composition.published == [USER] * 6, "все шесть переходов публикуют состав"


async def test_reading_the_remainder_publishes_nothing() -> None:
    """``remaining`` — чтение: публикация на нём разбудила бы поток состава на каждом открытии
    кабинета."""
    subscriptions, composition = service()
    assert await subscriptions.remaining(USER) == STUB_REMAINING_BYTES
    assert composition.published == []


async def test_the_stub_answers_the_same_period_for_any_arguments() -> None:
    """Соглашение заглушки: период фиксирован. Аргументы намеренно отличаются от фиксированных
    значений — иначе страж не отличил бы выдачу по аргументу от выдачи константы."""
    subscriptions, _ = service()
    assert await subscriptions.activate(USER, PLAN, "redeem") == STUB_PERIOD_ID
    assert await subscriptions.renew(USER, "order") == STUB_PERIOD_ID


def test_the_stub_numbers_agree_with_the_cabinet_the_user_sees() -> None:
    """Ответ §4.2 кабинет собирает из этой заглушки и из ``accounting.stats`` одновременно:
    расхождение показало бы пользователю остаток, не сходящийся с его же статистикой, и период,
    которого нет в статистике."""
    assert STUB_PERIOD_ID == STUB_STATS.period.id
    assert STUB_LIMIT_BYTES == STUB_STATS.limit == STUB_PLAN.traffic_limit_bytes
    assert STUB_USED_BILLABLE_BYTES == STUB_STATS.billable
    assert STUB_REMAINING_BYTES == STUB_STATS.remaining
    # Числа литералами, а не выводом из констант кода: «остаток = лимит − израсходовано» иначе
    # осталось бы верным при любой их подмене — страж рос бы вместе с тем, что проверяет.
    assert (STUB_LIMIT_BYTES, STUB_USED_BILLABLE_BYTES, STUB_REMAINING_BYTES) == (
        107374182400,
        21474836480,
        85899345920,
    )
