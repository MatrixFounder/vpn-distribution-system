"""Обработчики доставки уведомлений: ``notify.email`` и ``notify.webhook`` (§4.17; interfaces.md
§5.4, очередь ``background``; ключи ``notify:email:{event_id}`` и
``notify:webhook:{event_id}:{sha256(url)}`` ставит ``app.notifications.events.emit``).

Задача 001.51: оба типа зарегистрированы, чтобы очередь знала их уже сейчас (тип вне реестра
исполнитель не выбирает — задачи копились бы в ``pending`` без красного сигнала), а обработчики
отказывают без повторов (``NonRetryableError`` → ``failed`` с причиной), по правилу заглушек
001.21 и 001.33: «успех» доставки, которой не было, засчитался бы за отправленное уведомление.
Выбор каналов по таблице §4.17 (``channels_for``) и язык получателя — 001.52, почта — 001.78,
webhook — 001.79.
"""

from __future__ import annotations

import asyncpg

from app.jobs.queue import Job, NonRetryableError
from app.notifications.delivery import NOTIFY_EMAIL, NOTIFY_WEBHOOK

# Типы модуля одним набором: реестр и страж расписания читают его, а не переписывают.
NOTIFICATION_TYPES: frozenset[str] = frozenset((NOTIFY_EMAIL, NOTIFY_WEBHOOK))


async def notify_email(conn: asyncpg.Connection, job: Job) -> None:
    """Заглушка: письмо не отправляется — отказ без повторов (001.78)."""
    raise NonRetryableError(f"{NOTIFY_EMAIL}: заглушка 001.51, доставка почты — 001.78")


async def notify_webhook(conn: asyncpg.Connection, job: Job) -> None:
    """Заглушка: webhook не вызывается — отказ без повторов (001.79)."""
    raise NonRetryableError(f"{NOTIFY_WEBHOOK}: заглушка 001.51, доставка webhook — 001.79")
