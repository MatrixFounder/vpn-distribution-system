"""Интерфейсы-заглушки задачи 001.12: сигнатуры на месте, логика помечена NotImplementedError;
ключи лимитов §5.12; недоступный Redis → RateLimitUnavailable (без стенда: закрытый порт)."""

from __future__ import annotations

import asyncio
import inspect

import pytest
import redis.asyncio as redis_async
from app.errors import ApiError
from app.redis import close_redis, get_redis
from app.security import csrf, deps, ratelimit, sessions
from fastapi import Request
from redis.exceptions import RedisError


def _request(method: str) -> Request:
    return Request(
        {"type": "http", "method": method, "path": "/", "headers": [], "query_string": b""}
    )


async def test_session_store_interface_and_keys() -> None:
    """Ключи и сигнатура контракта; поведение — tests/unit/security/test_sessions.py (001.14).
    Без Redis операции поднимают ошибку клиента, а не молча «успешны»."""
    store = sessions.SessionStore(
        redis_async.Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.5)
    )
    assert sessions.session_key("abc") == "sess:abc"
    assert sessions.subject_sessions_key("u1") == "user_sessions:u1"
    with pytest.raises(RedisError):
        await store.create("user", "u1", "203.0.113.1", "ua", 3600)
    assert inspect.signature(sessions.SessionStore.create).return_annotation in (
        "Session",
        sessions.Session,
    ), "create возвращает сессию с csrf (ревью 001.14 L-1)"
    with pytest.raises(RedisError):
        await store.get("abc")
    assert set(inspect.signature(sessions.SessionStore.create).parameters) == {
        "self",
        "kind",
        "subject_id",
        "ip",
        "ua",
        "ttl",
    }


async def test_csrf_dependency_safe_methods_and_anonymous() -> None:
    """Безопасные методы и запросы без сессионной cookie проходят без обращения к Redis;
    проверка с сессией — tests/e2e/test_auth.py (001.14)."""
    for method in ("GET", "HEAD", "OPTIONS", "POST", "DELETE"):
        await csrf.require_csrf(_request(method))
    assert csrf.CSRF_HEADER == "X-CSRF-Token" and csrf.CSRF_COOKIE == "csrf"


def test_ratelimit_keys_follow_section_5_12() -> None:
    assert ratelimit.login_ip("203.0.113.5") == "rl:login:ip:203.0.113.5"
    assert ratelimit.login_account("u1") == "rl:login:account:u1"
    assert ratelimit.register_email_domain("Example.COM") == "rl:register:domain:example.com"
    assert ratelimit.reset_email("A@B.io") == "rl:reset:email:a@b.io"
    assert ratelimit.subscription_token("h") == "rl:subscription:token:h"
    assert ratelimit.subscription_unknown_ip("1.2.3.4") == "rl:subscription-unknown:ip:1.2.3.4"
    assert ratelimit.node_identity("n") == "rl:node:identity:n"
    assert ratelimit.enrollment("1.2.3.4", "th") == "rl:enroll:ip-token:1.2.3.4:th"


async def test_ratelimiter_fail_closed_and_stub() -> None:
    limiter = ratelimit.RateLimiter(
        redis_async.Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.5)
    )
    with pytest.raises(ratelimit.RateLimitUnavailable):
        await limiter.ensure_available()
    with pytest.raises(ratelimit.RateLimitUnavailable):  # check тоже fail-closed
        await limiter.check("rl:login:ip:x", 5, 60)


async def test_fail_closed_is_bounded_when_redis_accepts_but_never_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis жив на уровне TCP, но не отвечает (пауза, зависание): клиент из ``get_redis``
    (таймауты 1 с / 2 с) даёт ``RateLimitUnavailable`` за ограниченное время, а не висит.
    Таймаут подключения поведенчески не воспроизвести без сети (молчащий сокет соединение
    принимает) — он закреплён проверкой конфигурации клиента."""
    release = asyncio.Event()

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await release.wait()  # принимаем соединение и молчим до конца теста
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{port}/0")
    await close_redis()
    try:
        client = await get_redis()
        kwargs = client.connection_pool.connection_kwargs
        assert (kwargs["socket_connect_timeout"], kwargs["socket_timeout"]) == (1.0, 2.0)
        limiter = ratelimit.RateLimiter(client)
        started = asyncio.get_running_loop().time()
        with pytest.raises(ratelimit.RateLimitUnavailable):
            await asyncio.wait_for(limiter.ensure_available(), timeout=10)
        assert asyncio.get_running_loop().time() - started < 5.0, "таймаут команды не ограничен"
    finally:
        await close_redis()
        release.set()
        server.close()
        await server.wait_closed()


async def test_current_user_and_admin_active_permission_stubbed() -> None:
    """``current_user`` действует с 001.15, ``current_admin`` — с 001.18 (минимальный вид: сессия
    вида ``admin``): без cookie — 401 до обращения к Redis; разрешения — заглушка до 001.46."""
    for dependency in (deps.current_user, deps.current_admin):
        with pytest.raises(ApiError) as denied:
            await dependency(_request("GET"))
        assert (denied.value.status, denied.value.code) == (401, "unauthenticated")
    with pytest.raises(NotImplementedError, match="users.read"):
        await deps.require_permission("users.read")(_request("GET"))
