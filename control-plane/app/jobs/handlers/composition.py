"""Обработчик ``composition.publish_user`` — выдача и обновление доступа пользователя на его
нодах (§5.4: ключ идемпотентности ``publish:{node_id}:{users_seq}``; бюджет Н-13 на отзыв).

Задача 001.28: обработчик зарегистрирован и завершает задачу успехом, чтобы очередь принимала
задачи этого типа уже сейчас. Запись строк ``node_user_state`` с новым ``updated_seq`` и
``PUBLISH node:{id}`` — 001.29.
"""

from __future__ import annotations

import logging

import asyncpg

from app.jobs.queue import Job

log = logging.getLogger(__name__)

PUBLISH_USER = "composition.publish_user"


async def publish_user(conn: asyncpg.Connection, job: Job) -> None:
    """Заглушка: задача принимается и завершается успехом; состав не меняется (001.29)."""
    log.info("%s: заглушка 001.28, состав не изменён (задача %s)", PUBLISH_USER, job.id)
