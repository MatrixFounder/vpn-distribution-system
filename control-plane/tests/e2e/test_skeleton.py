"""Сквозные проверки каркаса C-01 (задача 001.10) через транспорт ASGI.

TC-E2E-01: приложение стартует без базы, ``/healthz`` и ``/openapi.json`` отвечают 200, в схеме
три префикса маршрутов и версия. TC-E2E-02: единый формат ошибок §5.1 — 404 маршрутизации, 405,
422 валидации с подробностями, 501 заглушек с кодом ``not_implemented``, 500 без подробностей.
Плюс ``/metrics`` в формате Prometheus, ленивые пул и Redis против стенда.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from pathlib import Path

import asyncpg
import httpx
import pytest
from app import __version__
from app.agent_api.deps import current_node
from app.config import SecretError
from app.db import pool as pool_module
from app.db.pool import DatabaseUnavailable
from app.main import create_app
from app.redis import close_redis, get_redis
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from tests._agent import HEADERS, node_as
from tests._pki import key_usage, make_ca

PREFIXES = ("/api/v1/", "/agent/v1/", "/s/")
# Заголовки ноды (001.28): без них раздел /agent/v1 отвечает 426 или 401 до разбора запроса.


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


async def test_the_api_start_refuses_a_missing_or_broken_node_ca(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Роль ``api`` выпускает сертификаты нод (§7.1, 001.25): без ``CA_KEY_FILE`` или
    ``CA_CERT_FILE``, с отсутствующим, пустым или нечитаемым файлом, с ключом не от сертификата,
    с ключом под паролем, с сертификатом без ``CA:TRUE``, с ``keyUsage`` без ``keyCertSign`` или
    ``extendedKeyUsage`` без ``clientAuth``, с промежуточным (не самоподписанным) CA или с AKI
    чужого ключа, с CA истёкшим или ещё не начавшимся — старт
    (lifespan) падает ``SecretError``, а не 500 на первом enrollment после зелёной проверки
    живости (роаст 001.25, раунды 1, 2, 4, 5 и 6). Воркерам CA не нужен: их старт без него
    проходит.
    Годная пара — старт роли ``api`` в ``test_app_starts_and_publishes_schema`` (пару задаёт
    ``tests/conftest.py``)."""
    now = dt.datetime.now(dt.UTC)
    key_pem, cert_pem = make_ca()
    other_key, _ = make_ca()
    foreign = ec.generate_private_key(ec.SECP256R1())
    files = {
        "key": key_pem,
        "cert": cert_pem,
        "other_key": other_key,
        "empty": b"",
        "junk": b"-----BEGIN PRIVATE KEY-----\nnot base64\n-----END PRIVATE KEY-----\n",
    }
    pairs = {
        "locked": make_ca(password=b"secret"),
        "not_ca": make_ca(ca=False),
        "no_cert_sign": make_ca(key_usage=key_usage(digital_signature=True, crl_sign=True)),
        "no_client_auth": make_ca(extended_key_usage=[ExtendedKeyUsageOID.SERVER_AUTH]),
        "intermediate": make_ca("intermediate CA", issued_by=make_ca("root CA")),
        "foreign_aki": make_ca(
            authority=lambda key, issuer, serial: (
                x509.AuthorityKeyIdentifier.from_issuer_public_key(foreign.public_key())
            )
        ),
        "expired": make_ca(
            not_before=now - dt.timedelta(days=400), not_after=now - dt.timedelta(days=1)
        ),
        "future": make_ca(
            not_before=now + dt.timedelta(days=1), not_after=now + dt.timedelta(days=400)
        ),
    }
    for name, (pair_key, pair_cert) in pairs.items():
        files[f"{name}.key"], files[f"{name}.crt"] = pair_key, pair_cert
    for name, content in files.items():
        (tmp_path / name).write_bytes(content)
    good = {"CA_KEY_FILE": str(tmp_path / "key"), "CA_CERT_FILE": str(tmp_path / "cert")}

    def pair(name: str) -> dict[str, str | None]:
        return {
            "CA_KEY_FILE": str(tmp_path / f"{name}.key"),
            "CA_CERT_FILE": str(tmp_path / f"{name}.crt"),
        }

    broken: list[tuple[dict[str, str | None], str]] = [
        ({"CA_KEY_FILE": None}, "CA_KEY_FILE"),
        ({"CA_CERT_FILE": None}, "CA_CERT_FILE"),
        ({"CA_KEY_FILE": str(tmp_path / "absent")}, "CA недоступен"),
        ({"CA_CERT_FILE": str(tmp_path / "absent")}, "CA недоступен"),
        ({"CA_KEY_FILE": str(tmp_path / "empty")}, "CA негоден"),
        ({"CA_KEY_FILE": str(tmp_path / "junk")}, "CA негоден"),
        ({"CA_CERT_FILE": str(tmp_path / "junk")}, "CA негоден"),
        ({"CA_KEY_FILE": str(tmp_path / "other_key")}, "CA негоден"),
        (pair("locked"), "CA негоден"),
        (pair("not_ca"), "CA:TRUE"),
        (pair("no_cert_sign"), "keyCertSign"),
        (pair("no_client_auth"), "extendedKeyUsage"),
        (pair("intermediate"), "не самоподписанный"),
        (pair("foreign_aki"), "чужой ключ"),
        (pair("expired"), "вне своего срока"),
        (pair("future"), "вне своего срока"),
    ]
    monkeypatch.setenv("APP_ROLE", "api")
    for change, reason in broken:
        for name, value in {**good, **change}.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        app = create_app()
        with pytest.raises(SecretError, match=reason):
            async with app.router.lifespan_context(app):
                pytest.fail(f"api стартовал с негодным CA: {change}")
    for name in good:
        monkeypatch.delenv(name)
    monkeypatch.setenv("APP_ROLE", "worker-critical")
    app = create_app()
    async with app.router.lifespan_context(app):
        pass


async def test_error_format(app_client: httpx.AsyncClient) -> None:
    """TC-E2E-02: все ошибки — ``{"error": {"code", "message", "details"}}`` (§5.1)."""
    missing = await app_client.get("/api/v1/nonexistent")
    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "not_found", "message": "Not Found", "details": {}}}
    wrong_method = await app_client.post("/healthz")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "method_not_allowed"
    assert wrong_method.headers.get("allow"), "заголовок Allow сохраняется"

    # Раздел /agent/v1 сперва проверяет версию агента и identity (001.28, 001.25: поиск ноды в
    # базе), поэтому запрос с негодными курсорами предъявляет признаки фиксированной ноды, а поиск
    # подменён (`tests/_agent.py::node_as`) — иначе ответом был бы 426 или 401, а показать здесь
    # надо формат 422 с перечнем полей.
    app = create_app()
    app.dependency_overrides[current_node] = node_as()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://control-plane") as agent:
        invalid = await agent.get("/agent/v1/state?config_version=abc&users_seq=1", headers=HEADERS)
        assert invalid.status_code == 422
        error = invalid.json()["error"]
        assert error["code"] == "validation_error"
        locations = {tuple(e["loc"]) for e in error["details"]["errors"]}
        assert ("query", "config_version") in locations and ("query", "generation") in locations
        assert all({"loc", "msg", "type"} == set(e) for e in error["details"]["errors"])
        for cursor in ("config_version", "users_seq", "generation"):  # каждый курсор §5.2 — ≥ 0
            query = {"config_version": 0, "users_seq": 0, "generation": 0, cursor: -1}
            negative = await agent.get("/agent/v1/state", params=query, headers=HEADERS)
            assert negative.status_code == 422, cursor
            errors = negative.json()["error"]["details"]["errors"]
            assert [(e["loc"], e["type"]) for e in errors] == [
                (["query", cursor], "greater_than_equal")
            ], cursor

    # Без редиректов по завершающему слэшу: 307 строился бы из схемы запроса и за прокси уносил
    # токен /s/{token} в http:// (ревью 001.10); лишний слэш — 404 единого формата.
    for path in ("/s/sometoken/", "/api/v1/me/", "/healthz/"):
        slash = await app_client.get(path)
        assert slash.status_code == 404 and "location" not in slash.headers, path
        assert slash.json()["error"]["code"] == "not_found"


async def test_paths_with_control_characters_are_not_routed(app_client: httpx.AsyncClient) -> None:
    """Путь с управляющими символами (C0 и DEL) — 404 неизвестного маршрута до маршрутизации:
    маршруты Starlette — регулярные выражения с ``$``, который совпадает и перед завершающим
    ``\\n``, и ``/agent/v1/enroll%0A`` находил обработчик обмена мимо точных location nginx,
    ``/metrics%0A`` — экспозицию метрик на публичном server (стенд: 200; роаст 001.25, раунд 5).
    Параметр пути (``[^/]+``) принимает любой управляющий символ, поэтому каждый код проверяется
    на пробном маршруте с параметром — в середине и в конце значения: без отказа до маршрутизации
    ответил бы обработчик. На маршруте без параметра 404 дал бы и сам маршрутизатор, и тест не
    отличил бы отказ слоя от промаха маршрута (посадки S146 и S147 раунда 6 так и остались
    зелёными)."""
    not_found = {"error": {"code": "not_found", "message": "Not Found", "details": {}}}
    requests = [
        ("GET", "/metrics%0A"),
        ("GET", "/healthz%0D"),
        ("POST", "/agent/v1/enroll%0A"),
        ("POST", "/agent/v1/reports%0A"),
        ("GET", "/s/abc%0Adef"),
        ("GET", "/api/v1/admin/nodes%7F"),
    ]
    for method, path in requests:
        answer = await app_client.request(method, path, json={} if method == "POST" else None)
        assert (answer.status_code, answer.json()) == (404, not_found), (method, path)
    assert (await app_client.get("/healthz")).status_code == 200, "обычный путь маршрутизируется"

    app = create_app()

    @app.get("/probe/{value}")
    async def probe(value: str) -> dict[str, str]:
        return {"value": value}

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://control-plane") as client:
        assert (await client.get("/probe/ab")).json() == {"value": "ab"}, "проба маршрутизируется"
        for code in (*range(0x20), 0x7F):
            for path in (f"/probe/a%{code:02X}b", f"/probe/ab%{code:02X}"):
                answer = await client.get(path)
                assert (answer.status_code, answer.json()) == (404, not_found), path


@pytest.mark.parametrize(
    ("method", "path", "operation"),
    # Состояние ноды перестало быть заглушкой 501 с задачей 001.28.
    [("GET", "/s/sometoken", "subscription.get")],
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


async def test_error_lines_log_the_path_without_the_subscription_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Н-25: токен подписки — в пути ``/s/{token}``. Журнал запросов ведёт nginx (путь /s/ там
    исключён), access-log и WebSocket uvicorn выключены (``test_uvicorn_writes_no_access_log``), а
    свои строки с путём — 500 и 503 — приложение пишет с путём без токена (роаст 001.25, раунд 6:
    строка ``GET /s/<токен>`` в журнале api). Проверка — строки журналов ``app.*``; текст
    исключений обработчика и трассировку сервера она не читает: обработчику /s/ (001.4x) токен в
    сообщение исключения не класть (роаст раунда 7)."""
    app = create_app()
    token = f"probe-{secrets.token_hex(8)}"

    @app.get("/s/{value}/boom")
    async def boom(value: str) -> None:
        raise RuntimeError("сбой")

    @app.get("/s/{value}/down")
    async def down(value: str) -> None:
        raise DatabaseUnavailable("база недоступна")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            assert (await client.get(f"/s/{token}/boom")).status_code == 500
            assert (await client.get(f"/s/{token}/down")).status_code == 503
    # Только журналы приложения (с трассировкой): httpx пишет свой журнал клиента — это тест.
    formatter = logging.Formatter("%(name)s %(message)s")
    logged = "\n".join(formatter.format(r) for r in caplog.records if r.name.startswith("app"))
    assert "/s/…/boom" in logged and "/s/…/down" in logged, logged
    assert token not in logged


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
