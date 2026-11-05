"""Шифрование полей — AES-256-GCM (security.md §7.2): приватные ключи REALITY, credentials,
секреты TOTP, коды, пароль SMTP, ключ CA. Только вызовы ``cryptography`` (``AESGCM``); своя часть —
формат контейнера: ``key_version (1 байт) || nonce (12 байт) || шифртекст с тегом``. Связанные
данные (AAD) — версия ключа и привязка к записи (WI-38): таблица, колонка и ключ строки
(``bound_to``). Привязка в контейнере не хранится и обязательна при обоих вызовах — контейнер,
переставленный в другую строку прямо в базе, не расшифровывается. Чем привязка не защищает:
старый контейнер той же записи (до ротации, из копии) расшифровывается, колонки без шифра
(состояние, версия) не аутентифицируются — писатель базы и так меняет их напрямую. Ключ —
32 байта в base64 из файла
``APP_ENCRYPTION_KEY_FILE`` (``Settings.encryption_key_path``). Ротация по ``key_version`` —
позднее: сейчас один ключ, версия 1.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from typing import Any, NewType

import asyncpg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import SecretError, Settings, read_secret
from app.db.pool import DatabaseUnavailable

KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16  # тег аутентичности GCM


# Привязка контейнера к записи: выдаёт только ``bound_to`` — шифр не примет произвольные байты
# (например, одну таблицу без ключа строки: контейнеры всех строк стали бы взаимозаменяемы).
Binding = NewType("Binding", bytes)


def bound_to(table: str, column: str, *key: uuid.UUID | str) -> Binding:
    """Привязка контейнера к записи — связанные данные AES-GCM: таблица, колонка и части ключа
    строки в порядке ключа (JSON-массив строк: части с разделителем внутри не склеиваются в чужую
    привязку). Части ключа — ``uuid.UUID`` (каноническая запись) или непустая строка; пустая
    таблица, колонка, ключ, его часть или часть иного типа (``None`` из пустой колонки) —
    ``ValueError``."""
    for part in key:
        if not isinstance(part, uuid.UUID | str):
            raise ValueError(f"часть ключа привязки — UUID или строка, получено {type(part)}")
    parts = [table, column, *(str(part) for part in key)]
    if not key or not all(parts):
        raise ValueError("привязка контейнера — таблица, колонка и непустой ключ строки")
    return Binding(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode())


class FieldCipher:
    """AES-256-GCM с фиксированным ключом и версией ключа."""

    def __init__(self, key: bytes, key_version: int = 1) -> None:
        if len(key) != KEY_BYTES:
            raise ValueError(f"ключ AES-256-GCM — {KEY_BYTES} байта, получено {len(key)}")
        if not 1 <= key_version <= 255:
            raise ValueError("key_version — от 1 до 255 (один байт контейнера)")
        self._aead = AESGCM(key)
        self.key_version = key_version

    @classmethod
    def from_settings(cls, settings: Settings, key_version: int = 1) -> FieldCipher:
        """Ключ из файла секрета: base64 от 32 байт (``openssl rand -base64 32``)."""
        raw = read_secret(settings.encryption_key_path)
        try:
            key = base64.b64decode(raw, validate=True)
        except ValueError as exc:  # binascii.Error — подкласс ValueError
            raise SecretError(
                f"ключ шифрования не в base64: {settings.encryption_key_path}"
            ) from exc
        if len(key) != KEY_BYTES:
            raise SecretError(
                f"ключ шифрования — {KEY_BYTES} байта после base64, получено {len(key)}: "
                f"{settings.encryption_key_path}"
            )
        return cls(key, key_version)

    def encrypt(self, b: bytes, *, bound: Binding) -> bytes:
        """Контейнер ``версия || nonce || шифртекст+тег``, привязанный к записи ``bound``
        (``bound_to``); nonce случайный на каждый вызов."""
        version = bytes([self.key_version])
        nonce = os.urandom(NONCE_BYTES)
        return version + nonce + self._aead.encrypt(nonce, b, version + _required(bound))

    def decrypt(self, b: bytes, *, bound: Binding) -> bytes:
        """Открытый текст; чужая версия ключа, усечённый контейнер, подделка или контейнер другой
        записи (привязка не та) → ``InvalidTag``."""
        aad = _required(bound)
        if len(b) < 1 + NONCE_BYTES + TAG_BYTES or b[0] != self.key_version:
            raise InvalidTag  # версия ключа не наша либо контейнер короче минимального
        version, nonce, ciphertext = b[:1], b[1 : 1 + NONCE_BYTES], b[1 + NONCE_BYTES :]
        return self._aead.decrypt(nonce, ciphertext, version + aad)


def _required(bound: Binding) -> bytes:
    """Контейнер без владельца не пишется и не читается: пустая привязка — ``ValueError``."""
    if not bound:
        raise ValueError("контейнер без привязки к записи (bound_to)")
    return bound


class CipherMismatch(SecretError):  # noqa: N818 — имя в ряду SecretError
    """Ключ шифрования процесса не тот, которым зашифрованы данные базы (контрольный контейнер
    не расшифровывается): после восстановления из копии с другим файлом ключа или при чужой
    версии ключа."""


# Контрольный контейнер (WI-38): известный открытый текст, зашифрованный ключом базы, в строке
# ``settings``. Процесс, чей ключ его не расшифровывает, не работает с шифром вовсе — иначе
# каждая пара выглядела бы подменённой и ноды получили бы отзыв всех пользователей.
CHECK_KEY = "field_cipher_check"
CHECK_PLAIN = b"field-cipher-check"
_SELECT_CHECK = "select value #>> '{}' from settings where key = $1"
_INSERT_CHECK = (
    "insert into settings (key, value) values ($1, to_jsonb($2::text)) on conflict (key) do nothing"
)
_checked: FieldCipher | None = None
_checking: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None


async def verify_check(db: Any, cipher: FieldCipher, key: str) -> None:
    """Сверить шифр с контрольным контейнером строки ``settings.key``: нет строки — записать её
    этим шифром (первый процесс после развёртывания: доверие при первом использовании); есть —
    расшифровать. Не расшифровывается, не тот текст или строка пуста — ``CipherMismatch``.
    ``db`` — пул или подключение asyncpg **вне транзакции**: запись строки сверки в чужой
    транзакции откатилась бы вместе с ней, а процесс остался бы «сверенным» без строки, и
    следующий процесс с чужим ключом записал бы свою и сверку прошёл бы."""
    if isinstance(db, asyncpg.Connection) and db.is_in_transaction():
        raise RuntimeError("сверка шифра идёт вне транзакции вызывающего")
    bound = bound_to("settings", "value", key)
    stored = await db.fetchval(_SELECT_CHECK, key)
    if stored is None:
        sealed = base64.b64encode(cipher.encrypt(CHECK_PLAIN, bound=bound)).decode()
        await db.execute(_INSERT_CHECK, key, sealed)
        stored = await db.fetchval(_SELECT_CHECK, key)
    if stored is None:  # строка есть, но значение не строка (jsonb null)
        raise CipherMismatch(f"контрольный контейнер settings.{key} пуст")
    try:
        plain = cipher.decrypt(base64.b64decode(stored, validate=True), bound=bound)
    except (InvalidTag, ValueError) as exc:
        raise CipherMismatch(
            f"ключ шифрования не расшифровывает контрольный контейнер settings.{key}: "
            "файл ключа или его версия не те, которыми зашифрованы данные базы"
        ) from exc
    if plain != CHECK_PLAIN:
        raise CipherMismatch(f"контрольный контейнер settings.{key} расшифрован в чужой текст")


async def checked_cipher() -> FieldCipher:
    """Шифр процесса из файла ключа, сверенный с контрольным контейнером при первом
    использовании (роль api стартует без базы — сверка не на старте, а здесь) и запомненный.
    Сверка идёт на своём подключении, а не на подключении вызывающего: вызывать можно и в
    транзакции — строка сверки не откатится вместе с ней (роаст безопасности WI-38, раунд 3).
    Первые вызовы процесса сверяют по одному (``_check_lock``) — подключение одно, а не по
    подключению на каждый запрос ноды после старта. Несовпадение — ``CipherMismatch`` на каждый
    вызов: файл ключа читается заново, и исправленный файл действует без перезапуска."""
    global _checked
    if _checked is None:
        async with _check_lock():
            if _checked is None:
                settings = Settings.load()
                cipher = FieldCipher.from_settings(settings)
                conn = await _connect_for_check(settings)
                try:
                    await verify_check(conn, cipher, CHECK_KEY)
                finally:
                    await conn.close()
                _checked = cipher
    return _checked


def _check_lock() -> asyncio.Lock:
    """Замок сверки текущего цикла событий: в тестах цикл на каждый тест (как у пула,
    ``app.db.pool``), а замок привязывается к циклу, в котором его ждали."""
    global _checking
    loop = asyncio.get_running_loop()
    if _checking is None or _checking[0] is not loop:
        _checking = (loop, asyncio.Lock())
    return _checking[1]


async def _connect_for_check(settings: Settings) -> asyncpg.Connection:
    """Подключение сверки под ролью пула (``app_rw``) вне пула: вызывающий может держать все
    подключения пула, ожидая этот шифр. База недоступна — ``DatabaseUnavailable`` (503)."""
    try:
        conn: asyncpg.Connection = await asyncpg.connect(
            settings.pg_dsn_with_password, timeout=5, command_timeout=30
        )
    except (TimeoutError, OSError, asyncpg.PostgresConnectionError) as exc:
        raise DatabaseUnavailable(f"{type(exc).__name__}: {exc}") from exc
    return conn
