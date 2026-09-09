"""TC-UNIT-02 задачи 001.12: AES-256-GCM обратим, подделка и чужая версия ключа отвергаются,
ключ читается из файла настроек."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
from app.config import SecretError, Settings
from app.security.crypto import KEY_BYTES, NONCE_BYTES, TAG_BYTES, FieldCipher
from cryptography.exceptions import InvalidTag


def test_roundtrip_32_bytes_and_container_layout() -> None:
    cipher = FieldCipher(os.urandom(KEY_BYTES))
    plain = os.urandom(32)
    box = cipher.encrypt(plain)
    assert cipher.decrypt(box) == plain
    assert len(box) == 1 + NONCE_BYTES + len(plain) + TAG_BYTES, "версия || nonce || текст+тег"
    assert box[0] == 1 and cipher.key_version == 1
    assert cipher.encrypt(plain) != box, "nonce случайный — контейнеры различаются"
    assert cipher.decrypt(cipher.encrypt(b"")) == b""


def test_tampering_truncation_wrong_key_and_version_fail() -> None:
    key = os.urandom(KEY_BYTES)
    cipher = FieldCipher(key)
    box = cipher.encrypt(b"x" * 32)
    for position in (1, 1 + NONCE_BYTES, len(box) - 1):  # nonce, шифртекст, тег
        forged = bytearray(box)
        forged[position] ^= 0x01
        with pytest.raises(InvalidTag):
            cipher.decrypt(bytes(forged))
    with pytest.raises(InvalidTag):
        cipher.decrypt(box[:-1])
    with pytest.raises(InvalidTag):
        cipher.decrypt(b"")
    with pytest.raises(InvalidTag):
        FieldCipher(os.urandom(KEY_BYTES)).decrypt(box)  # другой ключ
    with pytest.raises(InvalidTag):
        FieldCipher(key, key_version=2).decrypt(box)  # версия ключа не наша
    assert FieldCipher(key, key_version=2).decrypt(FieldCipher(key, 2).encrypt(b"v2")) == b"v2"


def test_key_and_version_are_validated() -> None:
    with pytest.raises(ValueError):
        FieldCipher(os.urandom(16))
    with pytest.raises(ValueError):
        FieldCipher(os.urandom(KEY_BYTES), key_version=0)
    with pytest.raises(ValueError):
        FieldCipher(os.urandom(KEY_BYTES), key_version=256)


def test_cipher_from_settings_reads_base64_key_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "app_encryption_key"
    key_file.write_text(base64.b64encode(os.urandom(KEY_BYTES)).decode())
    monkeypatch.setenv("PG_DSN", "postgresql://app_rw@127.0.0.1:1/control_plane")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("APP_ROLE", "api")
    monkeypatch.setenv("APP_ENCRYPTION_KEY_FILE", str(key_file))
    monkeypatch.delenv("PG_PASSWORD_FILE", raising=False)
    cipher = FieldCipher.from_settings(Settings.load())
    assert cipher.decrypt(cipher.encrypt(b"secret")) == b"secret"

    key_file.write_text("k" * 44)  # не base64 → не 32 байта
    with pytest.raises(SecretError):
        FieldCipher.from_settings(Settings.load())
    key_file.write_text(base64.b64encode(os.urandom(16)).decode())
    with pytest.raises(SecretError):
        FieldCipher.from_settings(Settings.load())
