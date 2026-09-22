"""Обработчики очереди подписок: ``subscription.expire`` — снятие доступа по истечении периода,
``subscription.notify_expiring`` — предупреждение о скором окончании (постановка §4.11 «Состояние
пользователя на ноде», §4.17 «Уведомления», событие ``subscription_expiring``; interfaces.md §5.4).

Задача 001.21: оба типа зарегистрированы, чтобы очередь знала их уже сейчас (тип вне реестра
исполнитель не выбирает — задачи копились бы в ``pending`` без единого красного сигнала), а
обработчики отказывают без повторов (``NonRetryableError`` → ``failed`` с причиной). Правило то
же, что у заглушек 001.33: «успех» истечения, которого не было, оставил бы пользователя с
истёкшей подпиской в inbound и отчитался бы об обратном, а «успех» уведомления был бы засчитан
подавлением повторов §4.17 за уже отправленное. Отказ виден в очереди и в метриках, тишина — нет.

``SubscriptionService.expire`` реализует 001.22; выборку истёкших по ``subscription_periods
(period_end)`` и само расписание (``subscription.expire`` раз в минуту,
``subscription.notify_expiring`` раз в час) — 001.83; событие в ``events`` и доставку почтой и
webhook — 001.51/001.52. Задача несёт не пользователя, а срез: ``expire(user_id)`` домена —
операция над одним, обработчик обходит истёкших. В ``scheduler.SCHEDULE`` эти типы не ставятся,
пока они заглушки (страж — ``tests/unit/jobs/test_handlers.py``).
"""

from __future__ import annotations

import asyncpg

from app.jobs.queue import Job, NonRetryableError

SUBSCRIPTION_EXPIRE = "subscription.expire"
SUBSCRIPTION_NOTIFY_EXPIRING = "subscription.notify_expiring"
# Типы модуля одним набором: реестр и страж расписания читают его, а не переписывают.
SUBSCRIPTION_TYPES: frozenset[str] = frozenset((SUBSCRIPTION_EXPIRE, SUBSCRIPTION_NOTIFY_EXPIRING))


def _not_implemented(type_: str) -> NonRetryableError:
    return NonRetryableError(f"{type_}: заглушка 001.21, жизненный цикл подписки — 001.22")


async def expire(conn: asyncpg.Connection, job: Job) -> None:
    """Заглушка: подписка не истекает и доступ не снимается — отказ без повторов (001.22)."""
    raise _not_implemented(SUBSCRIPTION_EXPIRE)


async def notify_expiring(conn: asyncpg.Connection, job: Job) -> None:
    """Заглушка: предупреждение не отправляется — отказ без повторов (001.22, 001.52)."""
    raise _not_implemented(SUBSCRIPTION_NOTIFY_EXPIRING)
