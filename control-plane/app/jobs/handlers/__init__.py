"""Реестр обработчиков задач: ``HANDLERS[тип задачи] → корутина(conn, job)``.

Задача 001.11 регистрирует только ``noop``; LOGIC-задачи (отзыв доступа, публикация состава,
уведомления, агрегация, сверки — §5.4) добавляют свои обработчики сюда. Обработчик выполняется
после выборки задачи, вне транзакции выборки; свои транзакции он открывает сам.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import asyncpg

from app.jobs.queue import Job

Handler = Callable[[asyncpg.Connection, Job], Awaitable[None]]


async def noop(conn: asyncpg.Connection, job: Job) -> None:
    """Заглушка: ничего не делает, задача завершается успехом (TC-E2E-01 задачи 001.11)."""


HANDLERS: dict[str, Handler] = {"noop": noop}
