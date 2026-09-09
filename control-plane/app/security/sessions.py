"""Сессии (security.md §7.1): непрозрачный идентификатор в cookie, запись ``sess:{id}`` в Redis с
TTL, набор ``user_sessions:{subject_id}`` для инвалидации «выход везде» и блокировки (Н-27).
Задача 001.12 — интерфейс; логика (создание, чтение, отзыв) — 001.14: методы поднимают
``NotImplementedError``. Ключи и модель зафиксированы здесь, чтобы обработчики и тесты 001.13
собирались."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

import redis.asyncio as redis_async

SessionKind = Literal["user", "admin"]


def session_key(sid: str) -> str:
    return f"sess:{sid}"


def subject_sessions_key(subject_id: str) -> str:
    return f"user_sessions:{subject_id}"


@dataclass(frozen=True, slots=True)
class Session:
    """Запись сессии из Redis."""

    id: str
    kind: SessionKind
    subject_id: str
    ip: str
    ua: str
    created_at: dt.datetime
    ttl: int


class SessionStore:
    """Хранилище сессий поверх клиента Redis (``app.redis.get_redis``)."""

    def __init__(self, client: redis_async.Redis) -> None:
        self._redis = client

    async def create(self, kind: SessionKind, subject_id: str, ip: str, ua: str, ttl: int) -> str:
        """Создать сессию, вернуть её идентификатор (≥ 128 бит энтропии) — 001.14."""
        raise NotImplementedError("SessionStore.create — задача 001.14")

    async def get(self, sid: str) -> Session | None:
        """Сессия по идентификатору или ``None`` (истекла, отозвана) — 001.14."""
        raise NotImplementedError("SessionStore.get — задача 001.14")

    async def revoke(self, sid: str) -> None:
        """Отозвать одну сессию — 001.14."""
        raise NotImplementedError("SessionStore.revoke — задача 001.14")

    async def revoke_all(self, subject_id: str, except_sid: str | None = None) -> None:
        """Отозвать все сессии субъекта, кроме указанной (смена пароля, блокировка) — 001.14."""
        raise NotImplementedError("SessionStore.revoke_all — задача 001.14")
