"""TC-UNIT-01 задачи 001.10: ``app.config.Settings`` разбирает окружение стенда, отсутствующий
или пустой секрет даёт ошибку старта, пароль из файла подставляется в DSN с экранированием."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import SecretError, Settings, dsn_with_password
from pydantic import ValidationError

STAND_ENV = {
    "PG_DSN": "postgresql://app_rw@postgres:5432/control_plane",
    "PG_PASSWORD_FILE": "",  # заполняется в фикстуре
    "REDIS_URL": "redis://redis:6379/0",
    "APP_ROLE": "api",
    "APP_ENV": "stand",
    "SUBSCRIPTION_DOMAINS": "sub1.example.com, sub2.example.com,",
    "APP_ENCRYPTION_KEY_FILE": "",  # заполняется в фикстуре
}


@pytest.fixture
def stand_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """Окружение стенда с настоящими файлами секретов во временном каталоге."""
    password = tmp_path / "pg_app_rw_password"
    password.write_text(" p@ss:word/#1 \r\n")  # краевые пробелы — часть секрета
    key = tmp_path / "app_encryption_key"
    key.write_text("k" * 44)
    env = dict(STAND_ENV, PG_PASSWORD_FILE=str(password), APP_ENCRYPTION_KEY_FILE=str(key))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return env


def test_settings_parse_stand_environment(stand_env: dict[str, str]) -> None:
    settings = Settings.load()
    assert settings.pg_dsn == stand_env["PG_DSN"]
    assert settings.redis_url == "redis://redis:6379/0"
    assert settings.app_role == "api"
    assert settings.app_env == "stand"
    assert settings.subscription_domains == ["sub1.example.com", "sub2.example.com"]
    assert settings.encryption_key_path == stand_env["APP_ENCRYPTION_KEY_FILE"]
    expected_dsn = "postgresql://app_rw:%20p%40ss%3Aword%2F%231%20@postgres:5432/control_plane"
    assert settings.pg_dsn_with_password == expected_dsn, (  # noqa: S105 — тестовый пароль
        "пароль из файла экранирован, перевод строки отброшен, пробелы сохранены"
    )


def test_missing_or_empty_secret_fails_startup(
    stand_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("APP_ENCRYPTION_KEY_FILE", str(tmp_path / "absent"))
    with pytest.raises(SecretError, match="недоступен"):
        Settings.load()
    empty = tmp_path / "empty"
    empty.write_text(" \t\n")  # только пробелы — тоже пусто
    monkeypatch.setenv("APP_ENCRYPTION_KEY_FILE", str(empty))
    with pytest.raises(SecretError, match="пуст"):
        Settings.load()
    monkeypatch.setenv("APP_ENCRYPTION_KEY_FILE", stand_env["APP_ENCRYPTION_KEY_FILE"])
    monkeypatch.setenv("PG_PASSWORD_FILE", str(tmp_path / "absent"))
    with pytest.raises(SecretError):
        Settings.load()


def test_invalid_values_are_rejected(
    stand_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ROLE", "cron")
    with pytest.raises(ValidationError):
        Settings.load()
    monkeypatch.setenv("APP_ROLE", "scheduler")
    monkeypatch.delenv("PG_DSN")
    with pytest.raises(ValidationError):
        Settings.load()


def test_api_role_requires_subscription_domains(
    stand_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Кабинет и подписка без доменов не работают: роль api не стартует с пустым
    SUBSCRIPTION_DOMAINS, остальным ролям домены не нужны (001.15)."""
    monkeypatch.setenv("SUBSCRIPTION_DOMAINS", " , ")
    with pytest.raises(ValidationError, match="SUBSCRIPTION_DOMAINS"):
        Settings.load()
    monkeypatch.setenv("APP_ROLE", "worker-background")
    assert Settings.load().subscription_domains == []


def test_dsn_with_password_keeps_ipv6_and_explicit_password(tmp_path: Path) -> None:
    secret = tmp_path / "s"
    secret.write_text("x")
    assert dsn_with_password("postgresql://u@[::1]:5432/db", str(secret)) == (
        "postgresql://u:x@[::1]:5432/db"
    )
    assert dsn_with_password("postgresql://u:already@h/db", str(secret)) == (
        "postgresql://u:already@h/db"
    ), "пароль в URL не перезаписывается"
    assert dsn_with_password("postgresql://u@h/db", None) == "postgresql://u@h/db"
