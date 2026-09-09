"""Задача 001.14 (tdd-strict): ``SessionStore`` на Redis стенда — создание, чтение, отзыв, «выход
везде», TTL бездействия. Ключи вида ``sess:<sid>`` и ``user_sessions:<subject>`` с уникальным
субъектом на прогон удаляются после теста."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import redis.asyncio as redis_async
from app.security import sessions


@pytest.fixture
async def store(redis_url: str) -> AsyncIterator[tuple[sessions.SessionStore, str]]:
    client = redis_async.Redis.from_url(redis_url, decode_responses=True, socket_timeout=2.0)
    subject = f"test-{uuid.uuid4()}"
    try:
        yield sessions.SessionStore(client), subject
    finally:
        sids = await client.smembers(sessions.subject_sessions_key(subject))
        for sid in sids:
            await client.delete(sessions.session_key(sessions._text(sid)))
        await client.delete(sessions.subject_sessions_key(subject))
        await client.aclose()


# EXPECTED_FAIL_REASON: NotImplementedError: SessionStore.create — задача 001.14
async def test_create_and_get_roundtrip(store: tuple[sessions.SessionStore, str]) -> None:
    sessions_store, subject = store
    created = await sessions_store.create("user", subject, "203.0.113.7", "curl/8", 3600)
    sid = created.id
    assert len(sid) >= 32, "идентификатор ≥ 128 бит (§7.1)"
    assert len(created.csrf) >= 32, "маркер CSRF выдаётся вместе с сессией (ревью L-1)"
    session = await sessions_store.get(sid)
    assert session is not None
    assert (session.id, session.kind, session.subject_id) == (sid, "user", subject)
    assert (session.ip, session.ua, session.ttl) == ("203.0.113.7", "curl/8", 3600)
    assert session.csrf == created.csrf
    assert session.created_at.tzinfo is not None
    assert await sessions_store.get("nope") is None
    assert sid != (await sessions_store.create("user", subject, "203.0.113.7", "curl/8", 3600)).id


# EXPECTED_FAIL_REASON: NotImplementedError: SessionStore.create — задача 001.14
async def test_revoke_one_and_all_except_current(
    store: tuple[sessions.SessionStore, str],
) -> None:
    sessions_store, subject = store
    first = (await sessions_store.create("user", subject, "1.1.1.1", "a", 3600)).id
    second = (await sessions_store.create("user", subject, "2.2.2.2", "b", 3600)).id
    third = (await sessions_store.create("user", subject, "3.3.3.3", "c", 3600)).id
    await sessions_store.revoke(first)
    assert await sessions_store.get(first) is None
    assert await sessions_store.get(second) is not None
    await sessions_store.revoke_all(subject, except_sid=third)
    assert await sessions_store.get(second) is None
    assert await sessions_store.get(third) is not None, "текущая сессия сохранена"
    await sessions_store.revoke_all(subject)
    assert await sessions_store.get(third) is None
    await sessions_store.revoke("nope")  # повторный/чужой отзыв — не ошибка


# EXPECTED_FAIL_REASON: NotImplementedError: SessionStore.create — задача 001.14
async def test_ttl_is_sliding_on_access(store: tuple[sessions.SessionStore, str]) -> None:
    """TTL бездействия (Н-27): чтение продлевает сессию; истёкшая сессия исчезает."""
    sessions_store, subject = store
    sid = (await sessions_store.create("admin", subject, "1.1.1.1", "a", 3600)).id
    await sessions_store._redis.expire(sessions.session_key(sid), 5)  # «прошло много времени»
    assert await sessions_store.get(sid) is not None
    assert await sessions_store._redis.ttl(sessions.session_key(sid)) > 3000, "продлена при чтении"
    expired = (await sessions_store.create("user", subject, "1.1.1.1", "a", 1)).id
    await sessions_store._redis.expire(sessions.session_key(expired), 0)  # мгновенное истечение
    assert await sessions_store.get(expired) is None


# EXPECTED_FAIL_REASON: AssertionError: множество без TTL — ревью 001.14 (S-3)
async def test_subject_set_expires_and_drops_dead_members(
    store: tuple[sessions.SessionStore, str],
) -> None:
    """``user_sessions:<subject>`` не растёт вечно: множество живёт не дольше самой свежей
    сессии (TTL как у неё, продлевается при создании и чтении), а члены без живой сессии
    выбрасываются при следующем создании."""
    sessions_store, subject = store
    set_key = sessions.subject_sessions_key(subject)
    dead = (await sessions_store.create("user", subject, "1.1.1.1", "a", 3600)).id
    assert 3500 < await sessions_store._redis.ttl(set_key) <= 3600, "TTL множества = TTL сессии"
    await sessions_store._redis.expire(set_key, 5)
    live = (await sessions_store.create("user", subject, "1.1.1.1", "a", 3600)).id
    assert await sessions_store._redis.ttl(set_key) > 3500, "продлено при создании"
    await sessions_store._redis.delete(sessions.session_key(dead))  # сессия истекла
    await sessions_store.create("user", subject, "1.1.1.1", "a", 3600)
    members = {sessions._text(m) for m in await sessions_store._redis.smembers(set_key)}
    assert dead not in members and live in members, "мёртвый член выброшен, живые на месте"
    await sessions_store._redis.expire(set_key, 5)
    assert await sessions_store.get(live) is not None
    assert await sessions_store._redis.ttl(set_key) > 3500, "продлено при чтении"
