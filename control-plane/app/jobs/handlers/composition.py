"""Обработчики публикации состава (§5.4; бюджет Н-13 на отзыв), очередь ``critical``:

- ``composition.publish_user`` — доступ пользователя на его нодах: полезная нагрузка
  ``{"user_id": …}``, исполнитель ``CompositionService.publish_user_on`` (001.29);
- ``composition.publish_node`` — порция состава ноды: ``{"node_id": …}`` и у продолжения
  ``"after"`` — последний пользователь прежней порции; исполнитель
  ``CompositionService.publish_node_on`` (001.29b).

Обе ставит бизнес-операция в своей транзакции (``enqueue_publish_user``,
``enqueue_publish_node``; ключи ``publish_user:{user_id}:{xid}``, ``publish_node:{node_id}:{xid}``),
продолжение состава ноды ставит сам исполнитель (``publish_node:{node_id}:after:{user_id}``).
"""

from __future__ import annotations

import uuid

import asyncpg

from app.domain.composition import PUBLISH_NODE_JOB, PUBLISH_USER_JOB, CompositionService
from app.jobs.queue import Job, NonRetryableError

PUBLISH_USER = PUBLISH_USER_JOB
PUBLISH_NODE = PUBLISH_NODE_JOB


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


def node_of(job: Job) -> tuple[uuid.UUID, uuid.UUID | None]:
    """Нода задачи ``composition.publish_node`` и пользователь, после которого идёт порция
    (``"after"``; нет — с начала), из полезной нагрузки; негодная нагрузка — ``NonRetryableError``,
    как у ``user_of``."""
    try:
        node_id = uuid.UUID(str(job.payload["node_id"]))
    except (KeyError, ValueError, TypeError) as exc:  # TypeError — нагрузка не объект
        raise NonRetryableError(f"{PUBLISH_NODE}: нет node_id в полезной нагрузке") from exc
    after = job.payload.get("after")
    if after is None:
        return node_id, None
    try:
        return node_id, uuid.UUID(str(after))
    except ValueError as exc:
        raise NonRetryableError(f"{PUBLISH_NODE}: after — не UUID") from exc


async def publish_node(conn: asyncpg.Connection, job: Job) -> None:
    """Порция состава ноды: строки её пользователей под их блокировками публикации, одно
    ``PUBLISH node:{id}`` после последнего коммита порции и продолжение, если остались
    пользователи. Подключение обработчика приходит без транзакции."""
    node_id, after = node_of(job)
    await CompositionService(pool=None).publish_node_on(conn, node_id, after)
