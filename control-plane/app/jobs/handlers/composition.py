"""Обработчик ``composition.publish_user`` — выдача и обновление доступа пользователя на его
нодах (§5.4; бюджет Н-13 на отзыв): полезная нагрузка ``{"user_id": …}``, исполнитель —
``CompositionService.publish_user_on`` на подключении обработчика (задача 001.29). Ключ
идемпотентности ``publish_user:{user_id}:{xid}`` и постановка в транзакции бизнес-операции —
001.29b.
"""

from __future__ import annotations

import uuid

import asyncpg

from app.domain.composition import CompositionService
from app.jobs.queue import Job, NonRetryableError

PUBLISH_USER = "composition.publish_user"


def user_of(job: Job) -> uuid.UUID:
    """Пользователь задачи из полезной нагрузки. Негодная нагрузка — ошибка постановки, а не
    сбой среды: повтор её не исправит, поэтому ``NonRetryableError`` (задача сразу ``failed``)."""
    try:
        return uuid.UUID(str(job.payload["user_id"]))
    except (KeyError, ValueError, TypeError) as exc:  # TypeError — нагрузка не объект
        raise NonRetryableError(f"{PUBLISH_USER}: нет user_id в полезной нагрузке") from exc


async def publish_user(conn: asyncpg.Connection, job: Job) -> None:
    """Строки ``node_user_state`` пользователя на нодах его групп доступа с новыми
    ``updated_seq`` и ``PUBLISH node:{id}`` после коммита. Подключение обработчика приходит без
    транзакции: исполнитель открывает свою на каждую ноду."""
    await CompositionService(pool=None).publish_user_on(conn, user_of(job))
