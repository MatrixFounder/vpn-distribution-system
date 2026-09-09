"""Сессии (security.md §7.1): непрозрачный идентификатор в cookie, запись ``sess:{id}`` (хеш
Redis) с TTL бездействия — чтение продлевает срок (Н-27), набор ``user_sessions:{subject_id}``
для «выхода везде» и блокировки. Идентификатор — 256 бит из ``secrets``; в записи хранится и
``csrf`` — токен для заголовка ``X-CSRF-Token`` (§7.3, ``csrf.py``). Логика — 001.14."""

from __future__ import annotations

import datetime as dt
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

import redis.asyncio as redis_async
from fastapi import Response

SessionKind = Literal["user", "admin"]

# Cookie сессии (§7.1): непрозрачный идентификатор, HttpOnly, Secure, SameSite=Lax, весь сайт.
SESSION_COOKIE = "sid"
# Срок бездействия сессии пользователя: постановка его не задаёт (Н-27 — 12 ч только для
# администратора), принято 30 суток (решение 001.14, объявлено в задаче).
USER_SESSION_TTL = 30 * 24 * 3600


def set_session_cookie(response: Response, sid: str, ttl: int) -> None:
    """Выставить cookie сессии с атрибутами §7.1."""
    response.set_cookie(
        SESSION_COOKIE, sid, max_age=ttl, path="/", httponly=True, secure=True, samesite="lax"
    )


def clear_session_cookie(response: Response) -> None:
    """Снять cookie сессии теми же атрибутами (иначе браузер не сопоставит cookie)."""
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=True, samesite="lax")


def _text(value: bytes | str) -> str:
    """redis-py типизирует ответы как ``bytes | str``; клиент создаётся с decode_responses."""
    return value.decode() if isinstance(value, bytes) else value


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
    csrf: str = ""


class SessionStore:
    """Хранилище сессий поверх клиента Redis (``app.redis.get_redis``); ошибки Redis
    поднимаются как есть — вызывающие слои решают, fail-closed это или «сессии недействительны»
    (§9.1)."""

    def __init__(self, client: redis_async.Redis) -> None:
        self._redis = client

    async def create(
        self, kind: SessionKind, subject_id: str, ip: str, ua: str, ttl: int
    ) -> Session:
        """Создать сессию с TTL бездействия; вернуть её (идентификатор 256 бит, маркер CSRF —
        вместе, без повторного чтения). Множество сессий субъекта получает тот же TTL и
        очищается от членов без живой сессии — иначе оно росло бы с каждым входом вечно."""
        sid = secrets.token_urlsafe(32)
        created_at = dt.datetime.now(dt.UTC)
        session = Session(
            id=sid,
            kind=kind,
            subject_id=subject_id,
            ip=ip,
            ua=ua[:512],
            created_at=created_at,
            ttl=ttl,
            csrf=secrets.token_urlsafe(32),
        )
        record = {
            "kind": kind,
            "subject_id": subject_id,
            "ip": ip,
            "ua": session.ua,
            "created_at": created_at.isoformat(),
            "ttl": str(ttl),
            "csrf": session.csrf,
        }
        set_key = subject_sessions_key(subject_id)
        members = [_text(member) for member in await self._redis.smembers(set_key)]
        alive: list[int] = []
        if members:
            async with self._redis.pipeline(transaction=False) as pipe:
                for member in members:
                    pipe.exists(session_key(member))
                alive = await pipe.execute()
        stale = [member for member, exists in zip(members, alive, strict=True) if not exists]
        async with self._redis.pipeline(transaction=True) as pipe:
            if stale:
                pipe.srem(set_key, *stale)
            pipe.hset(session_key(sid), mapping=cast("Mapping[Any, Any]", record))
            pipe.expire(session_key(sid), ttl)
            pipe.sadd(set_key, sid)
            pipe.expire(set_key, ttl, gt=True)
            pipe.expire(set_key, ttl, nx=True)
            await pipe.execute()
        return session

    async def get(self, sid: str) -> Session | None:
        """Сессия по идентификатору или ``None`` (нет, истекла, отозвана); чтение продлевает
        TTL бездействия."""
        raw = await self._redis.hgetall(session_key(sid))
        if not raw:
            return None
        record = {_text(key): _text(value) for key, value in raw.items()}
        ttl = int(record["ttl"])
        kind = record["kind"]
        if kind not in ("user", "admin"):
            return None  # чужая или повреждённая запись — не сессия
        set_key = subject_sessions_key(record["subject_id"])
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.expire(session_key(sid), ttl)
            pipe.expire(set_key, ttl, gt=True)  # множество живёт не меньше самой свежей сессии
            pipe.expire(set_key, ttl, nx=True)
            await pipe.execute()
        return Session(
            id=sid,
            kind=cast("SessionKind", kind),
            subject_id=record["subject_id"],
            ip=record["ip"],
            ua=record["ua"],
            created_at=dt.datetime.fromisoformat(record["created_at"]),
            ttl=ttl,
            csrf=record.get("csrf", ""),
        )

    async def revoke(self, sid: str) -> None:
        """Отозвать одну сессию; отсутствующая — не ошибка."""
        owner = await self._redis.hget(session_key(sid), "subject_id")
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(session_key(sid))
            if owner:
                pipe.srem(subject_sessions_key(_text(owner)), sid)
            await pipe.execute()

    async def revoke_all(self, subject_id: str, except_sid: str | None = None) -> None:
        """Отозвать все сессии субъекта, кроме указанной (смена пароля, блокировка, «выход
        везде»)."""
        members = await self._redis.smembers(subject_sessions_key(subject_id))
        async with self._redis.pipeline(transaction=True) as pipe:
            for raw_sid in members:
                sid = _text(raw_sid)
                if sid != except_sid:
                    pipe.delete(session_key(sid))
                    pipe.srem(subject_sessions_key(subject_id), sid)
            await pipe.execute()
