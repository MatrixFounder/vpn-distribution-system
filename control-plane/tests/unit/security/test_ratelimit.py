"""TC-UNIT-02 задачи 001.14 (tdd-strict): скользящее окно ``RateLimiter.check`` на Redis стенда.

Ключи уникальны на прогон (``rl:test:<uuid>:…``) и удаляются после теста.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import redis.asyncio as redis_async
from app.security import ratelimit


@pytest.fixture
async def limiter(redis_url: str) -> AsyncIterator[tuple[ratelimit.RateLimiter, str]]:
    client = redis_async.Redis.from_url(redis_url, decode_responses=True, socket_timeout=2.0)
    prefix = f"rl:test:{uuid.uuid4()}"
    try:
        yield ratelimit.RateLimiter(client), prefix
    finally:
        keys = [key async for key in client.scan_iter(match=f"{prefix}*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


# EXPECTED_FAIL_REASON: NotImplementedError: RateLimiter.check — задача 001.14
async def test_sixth_call_within_window_is_rejected(
    limiter: tuple[ratelimit.RateLimiter, str],
) -> None:
    """Лимит 5 за 60 с: пять вызовов — истина, шестой — ложь."""
    rate, prefix = limiter
    results = [await rate.check(f"{prefix}:login", 5, 60) for _ in range(6)]
    assert results == [True] * 5 + [False]


# EXPECTED_FAIL_REASON: NotImplementedError: RateLimiter.check — задача 001.14
async def test_keys_are_independent_and_window_slides(
    limiter: tuple[ratelimit.RateLimiter, str],
) -> None:
    """Счётчики разных ключей не влияют друг на друга; после окна попытки снова принимаются
    (окно 1 с — чтобы тест не ждал минуту)."""
    rate, prefix = limiter
    assert [await rate.check(f"{prefix}:a", 2, 1) for _ in range(3)] == [True, True, False]
    assert await rate.check(f"{prefix}:b", 2, 1) is True, "другой ключ — свой счётчик"
    await asyncio.sleep(1.1)
    assert await rate.check(f"{prefix}:a", 2, 1) is True, "окно сдвинулось — попытка принята"


# EXPECTED_FAIL_REASON: NotImplementedError: RateLimiter.check — задача 001.14
async def test_rejected_calls_still_count(limiter: tuple[ratelimit.RateLimiter, str]) -> None:
    """Отклонённые попытки тоже считаются: перебор не «протекает» через границу лимита."""
    rate, prefix = limiter
    for _ in range(8):
        await rate.check(f"{prefix}:c", 3, 60)
    assert await rate.check(f"{prefix}:c", 3, 60) is False


# EXPECTED_FAIL_REASON: NotImplementedError: RateLimiter.check — задача 001.14
async def test_counter_key_expires_with_window(
    limiter: tuple[ratelimit.RateLimiter, str],
) -> None:
    """Ключ счётчика получает TTL порядка окна — Redis не копит счётчики вечно."""
    rate, prefix = limiter
    await rate.check(f"{prefix}:d", 5, 60)
    ttl = await rate._redis.ttl(f"{prefix}:d")
    assert 0 < ttl <= 61


# EXPECTED_FAIL_REASON: AttributeError: RateLimiter.peek/reset — ревью 001.14 (S-1)
async def test_peek_counts_without_recording_and_reset_clears(
    limiter: tuple[ratelimit.RateLimiter, str],
) -> None:
    """Порог по учётной записи (§5.12) считает только отказы: ``peek`` читает число попыток в
    окне, не добавляя новую; ``reset`` обнуляет счётчик после успешного входа."""
    rate, prefix = limiter
    assert await rate.peek(f"{prefix}:e", 60) == 0
    for _ in range(3):
        await rate.check(f"{prefix}:e", 10, 60)
    assert await rate.peek(f"{prefix}:e", 60) == 3
    assert await rate.peek(f"{prefix}:e", 60) == 3, "чтение не засчитывает попытку"
    await rate.reset(f"{prefix}:e")
    assert await rate.peek(f"{prefix}:e", 60) == 0
    await rate.reset(f"{prefix}:e")  # повторный сброс — не ошибка


# EXPECTED_FAIL_REASON: AttributeError: RateLimiter.peek — ревью 001.14 (S-1)
async def test_peek_ignores_attempts_outside_window(
    limiter: tuple[ratelimit.RateLimiter, str],
) -> None:
    rate, prefix = limiter
    await rate.check(f"{prefix}:f", 10, 1)
    assert await rate.peek(f"{prefix}:f", 1) == 1
    await asyncio.sleep(1.1)
    assert await rate.peek(f"{prefix}:f", 1) == 0, "старые отметки не считаются"


# EXPECTED_FAIL_REASON: AttributeError: RateLimiter.retry_after — ревью 001.14 (L-2)
async def test_retry_after_is_time_until_oldest_attempt_leaves_window(
    limiter: tuple[ratelimit.RateLimiter, str],
) -> None:
    """«Отказ с задержкой» (§5.12): при отказе клиенту сообщается, через сколько секунд окно
    освободится — от самой старой отметки; пустой ключ — 0."""
    rate, prefix = limiter
    assert await rate.retry_after(f"{prefix}:g", 60) == 0
    for _ in range(3):
        await rate.check(f"{prefix}:g", 2, 60)
    assert 58 <= await rate.retry_after(f"{prefix}:g", 60) <= 60
    await rate.check(f"{prefix}:h", 1, 1)
    await asyncio.sleep(1.1)
    assert await rate.retry_after(f"{prefix}:h", 1) == 0
