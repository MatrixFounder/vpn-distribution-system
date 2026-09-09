"""TC-E2E-01 задачи 001.12: fail-closed при недоступном Redis (§9.1) — эндпоинты с лимитом
частоты отвечают 503 с Retry-After в едином формате; при доступном Redis стенда заглушки отвечают
501; эндпоинты без лимита от Redis не зависят. Недоступность моделируется адресом на закрытый
порт (Redis стенда общий — его не останавливаем)."""

from __future__ import annotations

import httpx
import pytest
from app.main import create_app
from app.redis import close_redis


async def test_rate_limited_endpoints_fail_closed_without_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")  # закрытый порт
    monkeypatch.setenv("PG_DSN", "postgresql://app_rw@127.0.0.1:1/control_plane")
    await close_redis()
    try:
        transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            login = await client.post("/api/v1/auth/login")
            assert login.status_code == 503
            assert login.headers["retry-after"] == "5"
            assert login.json() == {
                "error": {
                    "code": "service_unavailable",
                    "message": "сервис временно недоступен",
                    "details": {},
                }
            }
            assert "Redis" not in login.text, "подробности отказа — в журнал, не клиенту"
            assert (await client.get("/s/sometoken")).status_code == 503
            assert (await client.get("/api/v1/me")).status_code == 501, "без лимита — не зависит"
            assert (await client.get("/healthz")).status_code == 200
    finally:
        await close_redis()


async def test_rate_limited_endpoints_answer_when_redis_is_up(
    redis_url: str, monkeypatch: pytest.MonkeyPatch, app_client: httpx.AsyncClient
) -> None:
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("PG_DSN", "postgresql://app_rw@127.0.0.1:1/control_plane")
    await close_redis()
    try:
        login = await app_client.post("/api/v1/auth/login")
        assert login.status_code == 501 and login.json()["error"]["code"] == "not_implemented"
        assert (await app_client.get("/s/sometoken")).status_code == 501
    finally:
        await close_redis()
