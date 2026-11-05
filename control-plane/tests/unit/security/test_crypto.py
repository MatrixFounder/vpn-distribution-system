"""TC-UNIT-02 задачи 001.12: AES-256-GCM обратим, подделка и чужая версия ключа отвергаются,
ключ читается из файла настроек. WI-38: контейнер привязан к своей записи — расшифровывается
только той же привязкой (таблица, колонка, ключ строки)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from pathlib import Path
from typing import Any, cast

import asyncpg
import pytest
from app.config import SecretError, Settings
from app.security import crypto
from app.security.crypto import (
    KEY_BYTES,
    NONCE_BYTES,
    TAG_BYTES,
    Binding,
    CipherMismatch,
    FieldCipher,
    bound_to,
    checked_cipher,
    verify_check,
)
from cryptography.exceptions import InvalidTag

ROW = bound_to("t", "secret_enc", "00000000-0000-7000-8000-000000000001")


def test_roundtrip_32_bytes_and_container_layout() -> None:
    cipher = FieldCipher(os.urandom(KEY_BYTES))
    plain = os.urandom(32)
    box = cipher.encrypt(plain, bound=ROW)
    assert cipher.decrypt(box, bound=ROW) == plain
    assert len(box) == 1 + NONCE_BYTES + len(plain) + TAG_BYTES, "версия || nonce || текст+тег"
    assert box[0] == 1 and cipher.key_version == 1
    assert cipher.encrypt(plain, bound=ROW) != box, "nonce случайный — контейнеры различаются"
    assert cipher.decrypt(cipher.encrypt(b"", bound=ROW), bound=ROW) == b""


def test_tampering_truncation_wrong_key_and_version_fail() -> None:
    key = os.urandom(KEY_BYTES)
    cipher = FieldCipher(key)
    box = cipher.encrypt(b"x" * 32, bound=ROW)
    for position in (1, 1 + NONCE_BYTES, len(box) - 1):  # nonce, шифртекст, тег
        forged = bytearray(box)
        forged[position] ^= 0x01
        with pytest.raises(InvalidTag):
            cipher.decrypt(bytes(forged), bound=ROW)
    with pytest.raises(InvalidTag):
        cipher.decrypt(box[:-1], bound=ROW)
    with pytest.raises(InvalidTag):
        cipher.decrypt(b"", bound=ROW)
    with pytest.raises(InvalidTag):
        FieldCipher(os.urandom(KEY_BYTES)).decrypt(box, bound=ROW)  # другой ключ
    with pytest.raises(InvalidTag):
        FieldCipher(key, key_version=2).decrypt(box, bound=ROW)  # версия ключа не наша
    v2 = FieldCipher(key, key_version=2)
    assert v2.decrypt(v2.encrypt(b"v2", bound=ROW), bound=ROW) == b"v2"


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
    assert cipher.decrypt(cipher.encrypt(b"secret", bound=ROW), bound=ROW) == b"secret"

    key_file.write_text("k" * 44)  # не base64 → не 32 байта
    with pytest.raises(SecretError):
        FieldCipher.from_settings(Settings.load())
    key_file.write_text(base64.b64encode(os.urandom(16)).decode())
    with pytest.raises(SecretError):
        FieldCipher.from_settings(Settings.load())


def test_a_container_opens_only_with_its_own_binding() -> None:
    """WI-38: связанные данные — версия ключа и привязка к записи. Контейнер, переставленный в
    другую строку, колонку или таблицу, не расшифровывается; порядок частей ключа значим
    (пара «пользователь × нода» ≠ «нода × пользователь»)."""
    cipher = FieldCipher(os.urandom(KEY_BYTES))
    user, node = "00000000-0000-7000-8000-0000000000a1", "00000000-0000-7000-8000-0000000000b1"
    own = bound_to("node_user_credentials", "vless_uuid_enc", user, node)
    box = cipher.encrypt(b"k" * 16, bound=own)
    assert cipher.decrypt(box, bound=own) == b"k" * 16
    assert len(box) == 1 + NONCE_BYTES + 16 + TAG_BYTES, "привязка в контейнере не хранится"
    for other in (
        bound_to(
            "node_user_credentials", "vless_uuid_enc", user, "00000000-0000-7000-8000-0000000000b2"
        ),
        bound_to(
            "node_user_credentials", "vless_uuid_enc", "00000000-0000-7000-8000-0000000000a2", node
        ),
        bound_to("node_user_credentials", "trojan_password_enc", user, node),
        bound_to("inbound_secrets", "vless_uuid_enc", user, node),
        bound_to("node_user_credentials", "vless_uuid_enc", node, user),
    ):
        with pytest.raises(InvalidTag):
            cipher.decrypt(box, bound=other)


def test_the_binding_is_canonical_and_never_empty() -> None:
    """Привязка — однозначная запись частей: части с разделителем внутри не склеиваются в чужую
    привязку; пустая привязка и пустые части отвергаются — контейнер без владельца не пишется."""
    assert bound_to("t", "c", "a|b") != bound_to("t", "c", "a", "b")
    assert bound_to("t", "c", "a", "b") == bound_to("t", "c", "a", "b")
    for parts in (("", "c", "k"), ("t", "", "k"), ("t", "c"), ("t", "c", "")):
        with pytest.raises(ValueError):
            bound_to(*parts)
    cipher = FieldCipher(os.urandom(KEY_BYTES))
    with pytest.raises(ValueError):
        cipher.encrypt(b"x", bound=Binding(b""))
    with pytest.raises(ValueError):
        cipher.decrypt(cipher.encrypt(b"x", bound=ROW), bound=Binding(b""))


@pytest.mark.parametrize(
    "parts",
    [("a|b",), ("a", "b"), ("a:b",), ("a\x00b",), ('a","b',), ("a]", "[b"), ("a\\", "b")],
)
def test_the_binding_round_trips_its_parts(parts: tuple[str, ...]) -> None:
    """Привязка — однозначная запись частей при любых разделителях внутри частей (``|``, ``:``,
    нулевой байт, кавычки, скобки): разбор возвращает ровно те части, и разные наборы частей не
    дают одну привязку."""
    assert json.loads(bound_to("t", "c", *parts)) == ["t", "c", *parts]
    assert bound_to("t", "c", "a|b") != bound_to("t", "c", "a", "b")
    assert bound_to("t", "c", "a:b") != bound_to("t", "c", "a", "b")


def test_a_key_part_is_a_uuid_or_a_non_empty_string() -> None:
    """``None`` из пустой колонки (или иной тип) не превращается в строку ``"None"``: такие
    строки получили бы одну привязку на всех. UUID пишется канонически."""
    user = uuid.UUID("00000000-0000-7000-8000-0000000000a1")
    assert bound_to("t", "c", user) == bound_to("t", "c", str(user))
    for bad in (None, 5, b"k"):
        with pytest.raises(ValueError):
            bound_to("t", "c", cast(Any, bad))


def test_the_key_version_is_authenticated_with_the_binding() -> None:
    """Версия ключа входит в связанные данные: контейнер версии 1 с переписанным байтом версии не
    расшифровывается шифром версии 2 того же ключа."""
    key = os.urandom(KEY_BYTES)
    box = bytearray(FieldCipher(key, key_version=1).encrypt(b"v1", bound=ROW))
    box[0] = 2
    with pytest.raises(InvalidTag):
        FieldCipher(key, key_version=2).decrypt(bytes(box), bound=ROW)


async def test_the_check_container_detects_a_foreign_key(pg_dsn: str) -> None:
    """Контрольный контейнер (WI-38): первый сверяющий процесс записывает его своим ключом;
    тот же ключ сверку проходит, чужой ключ и чужая версия ключа — ``CipherMismatch``, подменённое
    значение — тоже. Имя ключа — под префиксом уборки, строка `field_cipher_check` стенда не
    трогается."""
    name = f"t19-check-{uuid.uuid4().hex[:8]}"
    conn = await asyncpg.connect(pg_dsn)
    try:
        key = os.urandom(KEY_BYTES)
        owner = FieldCipher(key)
        await verify_check(conn, owner, name)
        assert await conn.fetchval("select count(*) from settings where key = $1", name) == 1
        await verify_check(conn, FieldCipher(key), name)
        for foreign in (FieldCipher(os.urandom(KEY_BYTES)), FieldCipher(key, key_version=2)):
            with pytest.raises(CipherMismatch):
                await verify_check(conn, foreign, name)
        other = base64.b64encode(
            owner.encrypt(b"other", bound=bound_to("settings", "value", name))
        ).decode()
        await conn.execute(
            "update settings set value = to_jsonb($2::text) where key = $1", name, other
        )
        with pytest.raises(CipherMismatch):
            await verify_check(conn, owner, name)
    finally:
        await conn.execute("delete from settings where key like 't19-check-%'")
        await conn.close()


async def test_the_process_cipher_is_checked_once(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``checked_cipher`` сверяет шифр процесса при первом использовании на своём подключении,
    закрывает его и запоминает шифр: одновременные первые вызовы открывают одно подключение,
    следующие в базу не ходят. Ключ процесса не тот — ``CipherMismatch``, шифр не
    запоминается, подключение сверки закрыто."""
    name = f"t19-check-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(crypto, "CHECK_KEY", name)
    monkeypatch.setattr(crypto, "_checked", None)
    opened: list[asyncpg.Connection] = []
    connect = crypto._connect_for_check

    async def counting(settings: Settings) -> asyncpg.Connection:
        conn = await connect(settings)
        opened.append(conn)
        return conn

    monkeypatch.setattr(crypto, "_connect_for_check", counting)
    conn = await asyncpg.connect(pg_dsn)
    try:
        first, *others = await asyncio.gather(*(checked_cipher() for _ in range(5)))
        assert all(other is first for other in others)
        assert await checked_cipher() is first
        assert len(opened) == 1 and opened[0].is_closed()
        foreign = base64.b64encode(
            FieldCipher(os.urandom(KEY_BYTES)).encrypt(
                crypto.CHECK_PLAIN, bound=bound_to("settings", "value", name)
            )
        ).decode()
        await conn.execute(
            "update settings set value = to_jsonb($2::text) where key = $1", name, foreign
        )
        monkeypatch.setattr(crypto, "_checked", None)
        with pytest.raises(CipherMismatch):
            await checked_cipher()
        assert crypto._checked is None
        assert len(opened) == 2 and opened[1].is_closed()
    finally:
        await conn.execute("delete from settings where key like 't19-check-%'")
        await conn.close()


class _RollbackError(Exception):
    """Откат транзакции вызывающего."""


async def test_a_check_inside_the_callers_transaction_is_committed(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сверка идёт на своём подключении: вызывающий в транзакции (ротация 001.29d, TOTP 001.89
    шифруют в своей транзакции) получает сверенный шифр, и строка сверки закоммичена, даже если
    его транзакция откатилась (роаст безопасности WI-38, раунд 3)."""
    name = f"t19-check-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(crypto, "CHECK_KEY", name)
    monkeypatch.setattr(crypto, "_checked", None)
    conn = await asyncpg.connect(pg_dsn)
    try:
        with pytest.raises(_RollbackError):
            async with conn.transaction():
                await conn.execute("select 1")
                cipher = await checked_cipher()
                raise _RollbackError
        assert crypto._checked is cipher
        assert await conn.fetchval("select count(*) from settings where key = $1", name) == 1
    finally:
        await conn.execute("delete from settings where key like 't19-check-%'")
        await conn.close()


KAT_KEY = bytes(range(KEY_BYTES))
# Контрольный контейнер под ключом KAT_KEY: снят один раз и закреплён литералом.
KAT_CHECK = "AeDi9vKaMrs72I9MlLtcAiQ7UKNc9Sv7rhfqzcZHHV/bVaPvFxD6IqRwiKh/aCo="


class _CheckRow:
    """Строка ``settings`` с контрольным контейнером известного ответа; записи не нужно."""

    def __init__(self, value: str) -> None:
        self.value = value
        self.keys: list[str] = []

    async def fetchval(self, query: str, key: str) -> str:
        self.keys.append(key)
        return self.value

    async def execute(self, *args: Any) -> None:
        raise AssertionError("строка сверки уже есть — запись не нужна")


async def test_the_check_container_is_pinned_by_a_known_answer() -> None:
    """Имя строки сверки, открытый текст и привязка контрольного контейнера закреплены известным
    ответом: контейнер, снятый фиксированным ключом, сверку проходит. Смена текста или привязки
    после развёртывания оставила бы каждый процесс без шифра, а смена имени строки заново
    доверила бы первому процессу (роаст безопасности WI-38, раунд 3)."""
    row = _CheckRow(KAT_CHECK)
    await verify_check(row, FieldCipher(KAT_KEY), crypto.CHECK_KEY)
    assert row.keys == ["field_cipher_check"]
    with pytest.raises(CipherMismatch):
        await verify_check(
            _CheckRow(KAT_CHECK), FieldCipher(os.urandom(KEY_BYTES)), "field_cipher_check"
        )


async def test_the_check_runs_outside_a_transaction(pg_dsn: str) -> None:
    """Сверка в транзакции вызывающего отвергается: запись строки сверки откатилась бы с ней, а
    процесс остался бы «сверенным» без строки (WI-38, роаст безопасности, раунд 2)."""
    name = f"t19-check-{uuid.uuid4().hex[:8]}"
    conn = await asyncpg.connect(pg_dsn)
    try:
        async with conn.transaction():
            with pytest.raises(RuntimeError, match="вне транзакции"):
                await verify_check(conn, FieldCipher(os.urandom(KEY_BYTES)), name)
        assert await conn.fetchval("select count(*) from settings where key = $1", name) == 0
    finally:
        await conn.execute("delete from settings where key like 't19-check-%'")
        await conn.close()
