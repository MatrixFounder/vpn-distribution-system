"""TC-E2E-01 задачи 001.12: fail-closed при недоступном Redis (§9.1) — эндпоинты с лимитом
частоты отвечают 503 с Retry-After в едином формате; при доступном Redis стенда заглушки отвечают
501; эндпоинты без лимита от Redis не зависят. Недоступность моделируется адресом на закрытый
порт (Redis стенда общий — его не останавливаем)."""

from __future__ import annotations

import httpx
import pytest
from app.main import create_app
from app.redis import close_redis

# Все семь маршрутов /auth с валидными телами: fail-closed должен закрывать каждый (§7.3, §9.1).
AUTH_REQUESTS: list[tuple[str, dict[str, object] | None]] = [
    ("/api/v1/auth/register", {"email": "a@b.io", "password": "x" * 8, "aup_version": "1"}),
    ("/api/v1/auth/verify", {"token": "t" * 32}),
    ("/api/v1/auth/login", {"email": "a@b.io", "password": "x" * 8}),
    ("/api/v1/auth/logout", None),
    ("/api/v1/auth/logout-all", None),
    ("/api/v1/auth/reset-request", {"email": "a@b.io"}),
    ("/api/v1/auth/reset-confirm", {"token": "t" * 32, "password": "x" * 8}),
]


async def test_rate_limited_endpoints_fail_closed_without_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")  # закрытый порт
    monkeypatch.setenv("PG_DSN", "postgresql://app_rw@127.0.0.1:1/control_plane")
    await close_redis()
    try:
        transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            login = await client.post("/api/v1/auth/login")  # без тела: 503 раньше валидации
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
            for path, body in AUTH_REQUESTS:  # каждый маршрут /auth, с валидным телом
                response = await client.post(path, json=body)
                assert response.status_code == 503, path
                assert response.headers["retry-after"] == "5", path
                assert "set-cookie" not in response.headers, path
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
        login = await app_client.post("/api/v1/auth/login")  # без тела → валидация, не 503
        assert login.status_code == 422 and login.json()["error"]["code"] == "validation_error"
        assert (await app_client.get("/s/sometoken")).status_code == 501
    finally:
        await close_redis()
