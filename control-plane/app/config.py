"""Настройки приложения C-01…C-03 из окружения (deployment.md §10.3, ``.env.example``).

Секреты — только файлами: ``PG_PASSWORD_FILE`` дополняет ``PG_DSN`` паролем,
``APP_ENCRYPTION_KEY_FILE`` указывает на ключ AES-256-GCM (§7.2). ``Settings.load()`` читает
окружение и проверяет файлы секретов: отсутствующий или пустой файл — ошибка старта, а не пустая
строка в рантайме. Импорт модуля и ``create_app()`` к базе не обращаются: пул и Redis создаются
лениво (001.10).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

AppRole = Literal["api", "worker-critical", "worker-background", "scheduler"]


class SecretError(RuntimeError):
    """Файл секрета отсутствует, недоступен или пуст."""


def read_secret(path: str | os.PathLike[str]) -> str:
    """Прочитать секрет из файла: содержимое без завершающих переводов строки (пробелы —
    часть секрета, как у Docker secrets); пустой или состоящий из пробелов файл — ошибка."""
    try:
        value = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as exc:
        raise SecretError(f"секрет недоступен: {path} ({exc.strerror})") from exc
    if not value.strip():
        raise SecretError(f"секрет пуст: {path}")
    return value


def dsn_with_password(dsn: str, password_file: str | None) -> str:
    """Подставить в DSN пароль из файла, если в URL его нет (IPv6-хост — в скобках)."""
    parts = urlsplit(dsn)
    if parts.password is not None or not password_file:
        return dsn
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    hostport = f"{host}:{parts.port}" if parts.port else host
    netloc = f"{parts.username or ''}:{quote(read_secret(password_file), safe='')}@{hostport}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class Settings(BaseSettings):
    """Переменные окружения роли процесса; имена — как в ``.env.example`` и docker-compose."""

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    pg_dsn: str = Field(alias="PG_DSN")
    pg_password_file: str | None = Field(default=None, alias="PG_PASSWORD_FILE")
    redis_url: str = Field(alias="REDIS_URL")
    app_role: AppRole = Field(alias="APP_ROLE")
    app_env: Literal["dev", "stand", "prod"] = Field(default="stand", alias="APP_ENV")
    log_level: str = Field(default="info", alias="LOG_LEVEL")
    # NoDecode: строка через запятую, а не JSON (pydantic-settings разбирает списки как JSON).
    subscription_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="SUBSCRIPTION_DOMAINS"
    )
    encryption_key_path: str = Field(alias="APP_ENCRYPTION_KEY_FILE")

    @field_validator("subscription_domains", mode="before")
    @classmethod
    def _split_domains(cls, value: object) -> object:
        """``SUBSCRIPTION_DOMAINS=sub1.example.com,sub2.example.com`` → список без пустых."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("pg_dsn", "redis_url")
    @classmethod
    def _require_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("пустой адрес")
        return value

    @classmethod
    def load(cls) -> Settings:
        """Прочитать окружение и проверить секреты: файл ключа и файл пароля (если задан) должны
        существовать и быть непустыми — иначе ``SecretError`` при старте."""
        settings = cls()  # type: ignore[call-arg]  # значения — из окружения (pydantic-settings)
        read_secret(settings.encryption_key_path)
        if settings.pg_password_file:
            read_secret(settings.pg_password_file)
        return settings

    @property
    def pg_dsn_with_password(self) -> str:
        """DSN ``app_rw`` с паролем из ``PG_PASSWORD_FILE`` (если пароль не в URL)."""
        return dsn_with_password(self.pg_dsn, self.pg_password_file)
