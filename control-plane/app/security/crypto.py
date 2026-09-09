"""Шифрование полей — AES-256-GCM (security.md §7.2): приватные ключи REALITY, credentials,
секреты TOTP, коды, пароль SMTP, ключ CA. Только вызовы ``cryptography`` (``AESGCM``); своя часть —
формат контейнера: ``key_version (1 байт) || nonce (12 байт) || шифртекст с тегом``, версия
входит в аутентифицируемые данные (AAD). Ключ — 32 байта в base64 из файла
``APP_ENCRYPTION_KEY_FILE`` (``Settings.encryption_key_path``). Ротация по ``key_version`` —
позднее: сейчас один ключ, версия 1.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import SecretError, Settings, read_secret

KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16  # тег аутентичности GCM


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

    def encrypt(self, b: bytes) -> bytes:
        """Контейнер ``версия || nonce || шифртекст+тег``; nonce случайный на каждый вызов."""
        version = bytes([self.key_version])
        nonce = os.urandom(NONCE_BYTES)
        return version + nonce + self._aead.encrypt(nonce, b, version)

    def decrypt(self, b: bytes) -> bytes:
        """Открытый текст; чужая версия ключа, усечённый контейнер или подделка → ``InvalidTag``."""
        if len(b) < 1 + NONCE_BYTES + TAG_BYTES or b[0] != self.key_version:
            raise InvalidTag  # версия ключа не наша либо контейнер короче минимального
        version, nonce, ciphertext = b[:1], b[1 : 1 + NONCE_BYTES], b[1 + NONCE_BYTES :]
        return self._aead.decrypt(nonce, ciphertext, version)
