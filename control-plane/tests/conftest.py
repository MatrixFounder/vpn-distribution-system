"""Общие фикстуры тестов Control Plane.

`pg_dsn`, `migrate_env` и `redis_url` читаются из окружения стенда разработки
(`deploy/compose/.env.example`, задача 001.02; `MIGRATE_DSN` — подключение `app_migrate` для
`app.cli migrate`). `app_client` — HTTP-клиент к приложению в процессе теста через транспорт
ASGI (задача 001.10): без сети и без lifespan, исключения приложения превращаются в ответ 500
единого формата, а не поднимаются в тест.
"""

import base64
import os
import secrets
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from app.main import create_app

from tests._pki import make_ca


@pytest.fixture(scope="session", autouse=True)
def app_environment(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Полное окружение `Settings` для ленивых пула и Redis в тестах: адреса стенда (как в
    фикстурах ниже), роль `api`, ключ шифрования — временный файл с 32 случайными байтами в
    base64, внутренний CA нод — одноразовая пара ключ/сертификат (`tests/_pki.py`), которую
    приложение читает тем же путём, что на стенде (`CA_KEY_FILE`/`CA_CERT_FILE`). Уже заданные
    переменные не трогаются; отдельные тесты переопределяют их monkeypatch."""
    secrets_dir = tmp_path_factory.mktemp("secrets")
    key_file = secrets_dir / "app_encryption_key"
    key_file.write_text(base64.b64encode(secrets.token_bytes(32)).decode())
    ca_key_pem, ca_cert_pem = make_ca()
    ca_key_file, ca_cert_file = secrets_dir / "ca_key", secrets_dir / "ca_cert"
    ca_key_file.write_bytes(ca_key_pem)
    ca_cert_file.write_bytes(ca_cert_pem)
    defaults = {
        "PG_DSN": "postgresql://app_rw:app@127.0.0.1:5432/control_plane",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "APP_ROLE": "api",
        "APP_ENCRYPTION_KEY_FILE": str(key_file),
        "CA_KEY_FILE": str(ca_key_file),
        "CA_CERT_FILE": str(ca_cert_file),
        "SUBSCRIPTION_DOMAINS": "sub1.example.com,sub2.example.com",  # два домена §4.2
    }
    added = [name for name in defaults if name not in os.environ]
    for name in added:
        os.environ[name] = defaults[name]
    try:
        yield
    finally:
        for name in added:
            os.environ.pop(name, None)
        for path in (key_file, ca_key_file, ca_cert_file):
            Path(path).unlink(missing_ok=True)


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    """DSN PostgreSQL стенда разработки под ролью `app_rw` (роли создаёт bootstrap 001.03)."""
    return os.environ.get("PG_DSN", "postgresql://app_rw:app@127.0.0.1:5432/control_plane")


@pytest.fixture(scope="session")
def migrate_env() -> dict[str, str]:
    """Окружение `python -m app.cli migrate`: MIGRATE_DSN по умолчанию — стенд под app_migrate."""
    env = dict(os.environ)
    env.setdefault("MIGRATE_DSN", "postgresql://app_migrate:app@127.0.0.1:5432/control_plane")
    return env


@pytest.fixture(scope="session")
def redis_url() -> str:
    """URL Redis стенда разработки."""
    return os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")


@pytest.fixture
async def app_client() -> AsyncIterator[httpx.AsyncClient]:
    """HTTP-клиент к приложению `create_app()` через транспорт ASGI."""
    transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://control-plane") as client:
        yield client
