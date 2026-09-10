"""Реестр обработчиков задач: ``HANDLERS[тип задачи] → корутина(conn, job)``.

Задача 001.11 регистрирует ``noop``; 001.14 — ``ensure_partitions`` (обслуживание партиций
§4.5); 001.28 — ``composition.publish_user`` (заглушка потока состава). LOGIC-задачи (отзыв
доступа, публикация состава, уведомления — ``send_email`` в 001.52, агрегация, сверки — §5.4)
добавляют свои обработчики сюда. Задачи типов, которых здесь нет,
исполнитель не выбирает — они ждут выпуска с обработчиком. Обработчик выполняется после выборки
задачи, вне транзакции выборки; свои транзакции он открывает сам.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import asyncpg

from app.jobs.handlers.composition import PUBLISH_USER, publish_user
from app.jobs.handlers.partitions import ensure_partitions
from app.jobs.queue import Job

Handler = Callable[[asyncpg.Connection, Job], Awaitable[None]]


async def noop(conn: asyncpg.Connection, job: Job) -> None:
    """Заглушка: ничего не делает, задача завершается успехом (TC-E2E-01 задачи 001.11)."""


HANDLERS: dict[str, Handler] = {
    "noop": noop,
    "ensure_partitions": ensure_partitions,
    PUBLISH_USER: publish_user,
}
