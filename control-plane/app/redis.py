"""Клиент Redis (§5.4: пробуждение long-poll, счётчики лимитов) — лениво, один на процесс."""

from __future__ import annotations

import redis.asyncio as redis_async

from app.config import Settings

_client: redis_async.Redis | None = None


async def get_redis(settings: Settings | None = None) -> redis_async.Redis:
    """Единственный клиент процесса; подключение устанавливается при первой команде."""
    global _client
    if _client is None:
        settings = settings or Settings.load()
        _client = redis_async.Redis.from_url(settings.redis_url, decode_responses=True)
    return _client


async def close_redis() -> None:
    """Закрыть клиент (остановка процесса); повторный вызов безопасен."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
