"""Клиент Redis (§5.4: пробуждение long-poll, счётчики лимитов) — лениво, один на процесс.

Клиент привязан к циклу событий, в котором создан: под uvicorn/исполнителем цикл один; в тестах
циклы сменяются на каждый тест, и клиент прежнего (закрытого) цикла отбрасывается и создаётся
заново — иначе первая же команда падала «Event loop is closed» (001.12).
"""

from __future__ import annotations

import asyncio
import contextlib

import redis.asyncio as redis_async

from app.config import Settings

_client: redis_async.Redis | None = None
_loop: asyncio.AbstractEventLoop | None = None


async def get_redis(settings: Settings | None = None) -> redis_async.Redis:
    """Единственный клиент процесса; подключение устанавливается при первой команде."""
    global _client, _loop
    loop = asyncio.get_running_loop()
    if _client is not None and _loop is not loop:
        stale = _client
        _client = None
        with contextlib.suppress(Exception):  # сокеты чужого цикла закрыть штатно нельзя
            await stale.aclose()
    if _client is None:
        _loop = loop
        settings = settings or Settings.load()
        # Короткие таймауты: fail-closed (§9.1) должен отвечать 503 быстро, а не висеть.
        _client = redis_async.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=2.0,
        )
    return _client


async def close_redis() -> None:
    """Закрыть клиент (остановка процесса); повторный вызов безопасен; клиент чужого цикла
    отбрасывается без штатного закрытия."""
    global _client, _loop
    if _client is None:
        return
    client, _client = _client, None
    _loop = None
    # Ошибка закрытия (сокеты чужого цикла, уже разорванное подключение) не должна ронять
    # остановку процесса или тест.
    with contextlib.suppress(Exception):
        await client.aclose()
