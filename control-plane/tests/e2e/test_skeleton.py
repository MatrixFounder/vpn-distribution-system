"""Сквозные проверки каркаса C-01 (задача 001.10) через транспорт ASGI.

TC-E2E-01: приложение стартует без базы, ``/healthz`` и ``/openapi.json`` отвечают 200, в схеме
три префикса маршрутов и версия. TC-E2E-02: единый формат ошибок §5.1 — 404 маршрутизации, 405,
422 валидации с подробностями, 501 заглушек с кодом ``not_implemented``, 500 без подробностей.
Плюс ``/metrics`` в формате Prometheus, ленивые пул и Redis против стенда.
"""

from __future__ import annotations

import asyncpg
import httpx
import pytest
from app import __version__
from app.db import pool as pool_module
from app.main import create_app
from app.redis import close_redis, get_redis

PREFIXES = ("/api/v1/", "/agent/v1/", "/s/")


async def test_app_starts_and_publishes_schema(
    app_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-E2E-01: /healthz → 200, /openapi.json → 200 с тремя префиксами и версией; сборка
    приложения и его старт (lifespan) не обращаются к базе и Redis даже при недоступных адресах."""
    monkeypatch.setenv("PG_DSN", "postgresql://app_rw@127.0.0.1:1/control_plane")  # закрытый порт
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("APP_ROLE", "api")
    monkeypatch.setenv("APP_ENCRYPTION_KEY_FILE", "/nonexistent/key")
    offline_app = create_app()
    transport = httpx.ASGITransport(app=offline_app)
    async with (
        offline_app.router.lifespan_context(offline_app),  # транспорт ASGI lifespan не запускает
        httpx.AsyncClient(transport=transport, base_url="http://t") as offline,
    ):
        health = await offline.get("/healthz")
    assert health.status_code == 200 and health.json() == {"status": "ok", "version": __version__}

    schema = await app_client.get("/openapi.json")
    assert schema.status_code == 200
    body = schema.json()
    assert body["info"]["version"] == __version__, "версия приложения в OpenAPI (R-50)"
    assert "v1" in body["info"]["description"], "версия API в описании"
    paths = list(body["paths"])
    for prefix in PREFIXES:
        assert any(p.startswith(prefix) for p in paths), (prefix, paths)
    assert all(any(p.startswith(prefix) for prefix in PREFIXES) for p in paths), (
        "служебные маршруты /healthz и /metrics не входят в схему"
    )
    assert (await app_client.get("/docs")).status_code == 404, "интерактивной документации нет"


async def test_error_format(app_client: httpx.AsyncClient) -> None:
    """TC-E2E-02: все ошибки — ``{"error": {"code", "message", "details"}}`` (§5.1)."""
    missing = await app_client.get("/api/v1/nonexistent")
    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "not_found", "message": "Not Found", "details": {}}}
    wrong_method = await app_client.post("/healthz")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "method_not_allowed"
    assert wrong_method.headers.get("allow"), "заголовок Allow сохраняется"

    invalid = await app_client.get("/agent/v1/state?config_version=abc&users_seq=1")
    assert invalid.status_code == 422
    error = invalid.json()["error"]
    assert error["code"] == "validation_error"
    locations = {tuple(e["loc"]) for e in error["details"]["errors"]}
    assert ("query", "config_version") in locations and ("query", "generation") in locations
    assert all({"loc", "msg", "type"} == set(e) for e in error["details"]["errors"])
    for cursor in ("config_version", "users_seq", "generation"):  # каждый курсор §5.2 — int ≥ 0
        query = {"config_version": 0, "users_seq": 0, "generation": 0, cursor: -1}
        negative = await app_client.get("/agent/v1/state", params=query)
        assert negative.status_code == 422, cursor
        assert [(e["loc"], e["type"]) for e in negative.json()["error"]["details"]["errors"]] == [
            (["query", cursor], "greater_than_equal")
        ], cursor

    # Без редиректов по завершающему слэшу: 307 строился бы из схемы запроса и за прокси уносил
    # токен /s/{token} в http:// (ревью 001.10); лишний слэш — 404 единого формата.
    for path in ("/s/sometoken/", "/api/v1/me/", "/healthz/"):
        slash = await app_client.get(path)
        assert slash.status_code == 404 and "location" not in slash.headers, path
        assert slash.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    ("method", "path", "operation"),
    [
        ("GET", "/api/v1/me", "me.profile"),
        ("GET", "/api/v1/admin/dashboard", "admin.dashboard"),
        ("POST", "/agent/v1/enroll", "agent.enroll"),
        ("GET", "/agent/v1/state?config_version=0&users_seq=0&generation=0", "agent.state"),
        ("GET", "/s/sometoken", "subscription.get"),
    ],
)
async def test_stubs_return_501(
    app_client: httpx.AsyncClient, method: str, path: str, operation: str
) -> None:
    """Заглушки STUB-задач: 501 с кодом not_implemented и именем операции."""
    response = await app_client.request(method, path)
    assert response.status_code == 501, response.text
    assert response.json() == {
        "error": {
            "code": "not_implemented",
            "message": f"операция «{operation}» ещё не реализована",
            "details": {"operation": operation},
        }
    }


async def test_unhandled_exception_is_masked() -> None:
    """Необработанное исключение → 500 ``internal_error`` без текста исключения в теле."""
    app = create_app()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("секретная подробность")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.get("/boom")
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "внутренняя ошибка сервера", "details": {}}
    }
    assert "секретная" not in response.text


async def test_metrics_exposition(app_client: httpx.AsyncClient) -> None:
    """/metrics — текстовый формат Prometheus с показателем живости (до 001.68)."""
    response = await app_client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE control_plane_up gauge\ncontrol_plane_up 1\n" in response.text


async def test_pool_and_redis_are_lazy_and_work(
    pg_dsn: str, redis_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """Пул и Redis создаются при первом обращении и работают против стенда; транзакция откатывает
    при исключении; закрытие идемпотентно."""
    from pathlib import Path

    key_file = Path(str(tmp_path)) / "key"
    key_file.write_text("k" * 44)
    monkeypatch.setenv("PG_DSN", pg_dsn)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("APP_ROLE", "api")
    monkeypatch.setenv("APP_ENCRYPTION_KEY_FILE", str(key_file))
    monkeypatch.delenv("PG_PASSWORD_FILE", raising=False)
    await pool_module.close_pool()
    pool = await pool_module.get_pool()
    assert await pool_module.get_pool() is pool, "один пул на процесс"
    try:
        async with pool_module.transaction(pool) as conn:
            assert await conn.fetchval("select current_user") == "app_rw"
            assert await conn.fetchval("select current_schema()") == "control_plane"
        with pytest.raises(RuntimeError):
            async with pool_module.transaction(pool) as conn:
                await conn.execute(
                    "insert into settings (key, value) values ('skeleton-probe', '1')"
                )
                raise RuntimeError("откат")
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval("select count(*) from settings where key = 'skeleton-probe'")
                == 0
            ), "транзакция откатилась"
    finally:
        async with pool.acquire() as conn:  # страховка стенда, если транзакция не откатилась
            await conn.execute("delete from settings where key = 'skeleton-probe'")
        await pool_module.close_pool()
    await pool_module.close_pool()
    with pytest.raises(asyncpg.InterfaceError):
        await pool.fetchval("select 1")  # закрытый пул не работает

    await close_redis()
    client = await get_redis()
    assert await get_redis() is client
    assert await client.ping() is True
    await close_redis()
    await close_redis()
