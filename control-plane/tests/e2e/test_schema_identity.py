"""Сквозные проверки задачи 001.04 (миграция 040 «учётные записи и аутентификация»).

TC-E2E-01: миграция применяется и откатывается на один шаг (``python -m app.cli migrate`` /
``--rollback``), откат удаляет только её объекты. TC-E2E-02: ограничения §4.2.1 отклоняют
нарушающие строки под ролью app_rw. Партиции ``auth_events`` создаёт планировщик 001.08 на семь
суток вперёд, поэтому пробы кладутся в далёкое прошлое (2000-01-01): такую партицию он не создаст,
а строка без партиции отклоняется независимо от состояния стенда.
"""

from __future__ import annotations

import datetime as dt
import uuid

import asyncpg
import psycopg
import pytest
from app.cli import migrate_dsn
from psycopg import sql

from ._cli import run_cli
from ._spec import EXPECTED_ENUMS

IDENTITY_TABLES = {"users", "admin_users", "admin_recovery_codes", "email_tokens", "auth_events"}

PROBE_DAY = dt.datetime(2000, 1, 1, tzinfo=dt.UTC)  # партиция-проба вне окна планировщика
PROBE_TS = PROBE_DAY + dt.timedelta(hours=12)

USER_ROW = {
    "email": "Alice@Example.com",
    "password_hash": "$argon2id$stub",
    "aup_version": "2026-09",
    "aup_accepted_at": dt.datetime(2026, 9, 8, tzinfo=dt.UTC),
}


async def existing_tables(pg_dsn: str) -> set[str]:
    """Таблицы схемы public, известные app_rw (включая партиционированные родительские)."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        rows = await conn.fetch(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relkind in ('r', 'p')"
        )
        return {r["relname"] for r in rows}
    finally:
        await conn.close()


async def insert_user(conn: asyncpg.Connection, **overrides: object) -> uuid.UUID:
    """Вставить пользователя с обязательными полями; вернуть id (uuid v7)."""
    row = {**USER_ROW, **overrides}
    user_id: uuid.UUID = await conn.fetchval(
        "insert into users (email, password_hash, aup_version, aup_accepted_at, language) "
        "values ($1, $2, $3, $4, $5) returning id",
        row["email"],
        row["password_hash"],
        row["aup_version"],
        row["aup_accepted_at"],
        row.get("language", "en"),
    )
    return user_id


async def test_migration_040_apply_rollback_one_step(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """TC-E2E-01: после apply таблицы есть; --rollback снимает только 040 (типы 0001 остаются)."""
    applied = run_cli(migrate_env, "migrate")
    assert applied.returncode == 0, applied.stderr
    assert IDENTITY_TABLES <= await existing_tables(pg_dsn)

    conn = await asyncpg.connect(pg_dsn)
    try:
        partition_key = await conn.fetchval(
            "select pg_get_partkeydef('public.auth_events'::regclass)"
        )
        assert partition_key == "RANGE (ts)", "auth_events партиционирована по диапазону ts"
        id_default = await conn.fetchval(
            "select column_default from information_schema.columns "
            "where table_name = 'auth_events' and column_name = 'id'"
        )
        assert id_default == "nextval('auth_events_id_seq'::regclass)"
        email_type = await conn.fetchval(
            "select udt_name from information_schema.columns "
            "where table_name = 'users' and column_name = 'email'"
        )
        assert email_type == "citext"
        uuid_version = await conn.fetchval("select uuid_extract_version(uuidv7())")
        assert uuid_version == 7, "идентификаторы — uuid v7 (§4.2)"
    finally:
        await conn.close()

    rolled_back = run_cli(migrate_env, "migrate", "--rollback")
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert "откачено миграций — 1" in rolled_back.stdout
    tables = await existing_tables(pg_dsn)
    assert not (IDENTITY_TABLES & tables), tables
    conn = await asyncpg.connect(pg_dsn)
    try:
        enums = await conn.fetchval(
            "select count(*) from pg_type t join pg_namespace n on n.oid = t.typnamespace "
            "where t.typtype = 'e' and n.nspname = 'public'"
        )
        assert enums == len(EXPECTED_ENUMS), "откат 040 не трогает перечисления 0001"
        seq = await conn.fetchval(
            "select count(*) from pg_class where relname = 'auth_events_id_seq'"
        )
        assert seq == 0, "последовательность удалена вместе с таблицей (OWNED BY)"
    finally:
        await conn.close()

    reapplied = run_cli(migrate_env, "migrate")
    assert reapplied.returncode == 0, reapplied.stderr
    assert IDENTITY_TABLES <= await existing_tables(pg_dsn)


async def test_constraints_reject_bad_rows(pg_dsn: str, migrate_env: dict[str, str]) -> None:
    """TC-E2E-02: UNIQUE (citext), CHECK, FK и enum отклоняют нарушающие строки под app_rw."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    conn = await asyncpg.connect(pg_dsn)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            user_id = await insert_user(conn)
            assert user_id.version == 7

            async def rejected(exc: type[Exception], query: str, *args: object) -> None:
                # Точка сохранения откатывается исключением, внешняя транзакция живёт дальше.
                with pytest.raises(exc):
                    async with conn.transaction():
                        await conn.execute(query, *args)

            await rejected(
                asyncpg.UniqueViolationError,
                "insert into users (email, password_hash, aup_version, aup_accepted_at) "
                "values ($1, $2, $3, $4)",
                "alice@example.COM",  # citext: регистр не делает адрес другим
                "x",
                "2026-09",
                USER_ROW["aup_accepted_at"],
            )
            # R-51 / AC-22: третий язык принимается без изменения схемы (CHECK на язык нет).
            await insert_user(conn, email="bob@example.com", language="de")
            await rejected(
                asyncpg.InvalidTextRepresentationError,
                "insert into users (email, password_hash, aup_version, aup_accepted_at, status) "
                "values ($1, $2, $3, $4, $5::user_status)",
                "carol@example.com",
                "x",
                "2026-09",
                USER_ROW["aup_accepted_at"],
                "frozen",
            )
            await rejected(
                asyncpg.ForeignKeyViolationError,
                "insert into admin_recovery_codes (admin_user_id, code_hash) values ($1, $2)",
                uuid.uuid4(),
                "hash",
            )
            await rejected(
                asyncpg.ForeignKeyViolationError,
                "insert into email_tokens (user_id, kind, token_hash, expires_at) "
                "values ($1, 'verify', $2, now() + interval '1 hour')",
                uuid.uuid4(),
                "t1",
            )
            await conn.execute(
                "insert into email_tokens (user_id, kind, token_hash, expires_at) "
                "values ($1, 'reset', $2, now() + interval '1 hour')",
                user_id,
                "same-token",
            )
            await rejected(
                asyncpg.UniqueViolationError,
                "insert into email_tokens (user_id, kind, token_hash, expires_at) "
                "values ($1, 'verify', $2, now() + interval '1 hour')",
                user_id,
                "same-token",
            )
            await rejected(
                asyncpg.NotNullViolationError,
                "insert into admin_users (email, password_hash) values ($1, $2)",  # role NOT NULL
                "root@example.com",
                "x",
            )
            # Удаление администратора уносит его резервные коды (ON DELETE CASCADE).
            admin_id = await conn.fetchval(
                "insert into admin_users (email, password_hash, role) "
                "values ($1, $2, 'operator') returning id",
                "op@example.com",
                "x",
            )
            await conn.execute(
                "insert into admin_recovery_codes (admin_user_id, code_hash) values ($1, 'c1')",
                admin_id,
            )
            await conn.execute("delete from admin_users where id = $1", admin_id)
            left = await conn.fetchval(
                "select count(*) from admin_recovery_codes where admin_user_id = $1", admin_id
            )
            assert left == 0
        finally:
            await tx.rollback()  # стенд остаётся без тестовых строк
    finally:
        await conn.close()


def owner_connection(migrate_env: dict[str, str]) -> psycopg.Connection:
    """Подключение app_migrate (через MIGRATE_DSN) для операций владельца в тестах."""
    dsn = migrate_dsn(migrate_env["MIGRATE_DSN"], migrate_env.get("MIGRATE_PASSWORD_FILE"))
    return psycopg.connect(
        dsn.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True
    )


async def test_auth_events_no_partition_rejects_rows(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Без партиции на дату строка отклоняется (партиции — планировщик 001.08); дата пробы —
    2000-01-01, партиции на неё планировщик не создаст."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    conn = await asyncpg.connect(pg_dsn)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            with pytest.raises(asyncpg.CheckViolationError):  # no partition of relation found
                await conn.execute(
                    "insert into auth_events (kind, ts, result) values ('login', $1, 'success')",
                    PROBE_TS,
                )
        finally:
            await tx.rollback()  # при красном гейте строка не остаётся на стенде
    finally:
        await conn.close()


async def test_auth_events_insert_as_app_rw(pg_dsn: str, migrate_env: dict[str, str]) -> None:
    """С партицией на дату app_rw вставляет строку: id из auth_events_id_seq (USAGE выдан
    умолчаниями §4.6), PK (ts, id) и CHECK result действуют. Партицию создаёт и удаляет app_owner;
    она живёт только внутри теста."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    with owner_connection(migrate_env) as owner:
        owner.execute("SET ROLE app_owner")
        owner.execute("DROP TABLE IF EXISTS auth_events_probe")
        owner.execute(
            sql.SQL(
                "CREATE TABLE auth_events_probe PARTITION OF auth_events "
                "FOR VALUES FROM ({start}) TO ({stop})"
            ).format(
                start=sql.Literal(PROBE_DAY.isoformat()),
                stop=sql.Literal((PROBE_DAY + dt.timedelta(days=1)).isoformat()),
            )
        )
        try:
            conn = await asyncpg.connect(pg_dsn)
            try:
                first = await conn.fetchval(
                    "insert into auth_events (kind, ts, result, source_ip) "
                    "values ('login', $1, 'success', '198.51.100.7') returning id",
                    PROBE_TS,
                )
                second = await conn.fetchval(
                    "insert into auth_events (kind, ts, result) values ('logout', $1, 'denied') "
                    "returning id",
                    PROBE_TS,
                )
                assert isinstance(first, int) and second == first + 1, "id из последовательности"
                with pytest.raises(asyncpg.UniqueViolationError):  # PK (ts, id)
                    await conn.execute(
                        "insert into auth_events (id, kind, ts, result) "
                        "values ($1, 'login', $2, 'success')",
                        first,
                        PROBE_TS,
                    )
                with pytest.raises(asyncpg.CheckViolationError):  # result вне success | denied
                    await conn.execute(
                        "insert into auth_events (kind, ts, result) values ('login', $1, 'maybe')",
                        PROBE_TS,
                    )
                with pytest.raises(asyncpg.InvalidTextRepresentationError):  # kind — enum
                    await conn.execute(
                        "insert into auth_events (kind, ts, result) values ('bogus', $1, 'denied')",
                        PROBE_TS,
                    )
                stored = await conn.fetchval(
                    "select count(*) from auth_events where ts >= $1 and ts < $2",
                    PROBE_DAY,
                    PROBE_DAY + dt.timedelta(days=1),
                )
                assert stored == 2
            finally:
                await conn.close()
        finally:
            owner.execute("DROP TABLE auth_events_probe")  # уносит и пробные строки
