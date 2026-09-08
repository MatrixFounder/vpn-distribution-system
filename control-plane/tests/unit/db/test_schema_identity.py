"""Задача 001.04, стражи схемы «учётные записи и аутентификация» по каталогам PostgreSQL.

``test_catalog_matches_data_model`` сверяет колонки (тип, NULL, умолчание), ограничения
(PK/UNIQUE/FK с ON DELETE/CHECK), индексы (с порядком и предикатом), ключ партиционирования и
последовательность с ожиданием по data-model.md §4.2.1 плюс отклонения, объявленные в задаче
(умолчания, индекс по FK ``email_tokens.user_id``, CHECK на ``auth_events.result``). Любое
расхождение миграции 040 со спецификацией красит тест.

``test_app_rw_privileges`` (TC-UNIT-01): права ролей — ровно по §4.6, для таблиц и для
последовательности ``auth_events_id_seq`` (без USAGE на неё ``app_rw`` не вставит ни одной строки в
``auth_events``). Через ``has_table_privilege``/``has_sequence_privilege``: ``information_schema``
из сессии ``app_rw`` чужих прав не показывает. Без базы тесты падают, не пропускаются.
"""

from __future__ import annotations

import asyncpg
import pytest

from ._introspect import (
    EXPECTED_TABLE_GRANTS,
    Column,
    assert_table_matches,
    table_grants,
)

EXPECTED_COLUMNS: dict[str, list[Column]] = {
    "users": [
        ("id", "uuid", False, "uuidv7()"),
        ("email", "citext", False, None),
        ("email_verified_at", "timestamptz", True, None),
        ("password_hash", "text", False, None),
        ("status", "user_status", False, "'active'::user_status"),
        ("language", "text", False, "'en'::text"),  # ru | en, без CHECK — R-51, AC-22
        ("timezone", "text", False, "'UTC'::text"),
        ("aup_version", "text", False, None),
        ("aup_accepted_at", "timestamptz", False, None),
        ("announce_consent", "bool", False, "false"),
        ("created_at", "timestamptz", False, "now()"),
        ("updated_at", "timestamptz", False, "now()"),
        ("deleted_at", "timestamptz", True, None),
    ],
    "admin_users": [
        ("id", "uuid", False, "uuidv7()"),
        ("email", "citext", False, None),
        ("password_hash", "text", False, None),
        ("role", "admin_role", False, None),
        ("totp_secret_enc", "bytea", True, None),
        ("totp_enabled_at", "timestamptz", True, None),
        ("status", "admin_status", False, "'active'::admin_status"),
        ("language", "text", False, "'en'::text"),
        ("created_at", "timestamptz", False, "now()"),
    ],
    "admin_recovery_codes": [
        ("id", "uuid", False, "uuidv7()"),
        ("admin_user_id", "uuid", False, None),
        ("code_hash", "text", False, None),
        ("used_at", "timestamptz", True, None),
    ],
    "email_tokens": [
        ("id", "uuid", False, "uuidv7()"),
        ("user_id", "uuid", False, None),
        ("kind", "email_token_kind", False, None),
        ("token_hash", "text", False, None),
        ("expires_at", "timestamptz", False, None),
        ("used_at", "timestamptz", True, None),
    ],
    "auth_events": [
        ("id", "int8", False, "nextval('auth_events_id_seq'::regclass)"),
        ("user_id", "uuid", True, None),  # без FK: партиции, §4.3
        ("kind", "auth_event_kind", False, None),
        ("ts", "timestamptz", False, "now()"),
        ("source_ip", "inet", True, None),
        ("user_agent", "text", True, None),
        ("result", "text", False, None),
    ],
}

# (тип pg_constraint, определение); NOT NULL живёт в колонках, не здесь.
EXPECTED_CONSTRAINTS: dict[str, set[tuple[str, str]]] = {
    "users": {("p", "PRIMARY KEY (id)"), ("u", "UNIQUE (email)")},
    "admin_users": {("p", "PRIMARY KEY (id)"), ("u", "UNIQUE (email)")},
    "admin_recovery_codes": {
        ("p", "PRIMARY KEY (id)"),
        ("f", "FOREIGN KEY (admin_user_id) REFERENCES admin_users(id) ON DELETE CASCADE"),
    },
    "email_tokens": {
        ("p", "PRIMARY KEY (id)"),
        ("u", "UNIQUE (token_hash)"),
        ("f", "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"),
    },
    "auth_events": {
        ("p", "PRIMARY KEY (ts, id)"),
        ("c", "CHECK ((result = ANY (ARRAY['success'::text, 'denied'::text])))"),
    },
}

EXPECTED_INDEXES: dict[str, set[str]] = {
    "users": {
        "CREATE UNIQUE INDEX users_pkey ON control_plane.users USING btree (id)",
        "CREATE UNIQUE INDEX users_email_key ON control_plane.users USING btree (email)",
    },
    "admin_users": {
        "CREATE UNIQUE INDEX admin_users_pkey ON control_plane.admin_users USING btree (id)",
        "CREATE UNIQUE INDEX admin_users_email_key ON control_plane.admin_users USING btree "
        "(email)",
    },
    "admin_recovery_codes": {
        "CREATE UNIQUE INDEX admin_recovery_codes_pkey ON control_plane.admin_recovery_codes "
        "USING btree (id)",
        "CREATE INDEX admin_recovery_codes_unused_idx ON control_plane.admin_recovery_codes "
        "USING btree (admin_user_id) WHERE (used_at IS NULL)",
    },
    "email_tokens": {
        "CREATE UNIQUE INDEX email_tokens_pkey ON control_plane.email_tokens USING btree (id)",
        "CREATE UNIQUE INDEX email_tokens_token_hash_key ON control_plane.email_tokens "
        "USING btree (token_hash)",
        "CREATE INDEX email_tokens_user_id_idx ON control_plane.email_tokens USING btree (user_id)",
    },
    "auth_events": {
        "CREATE UNIQUE INDEX auth_events_pkey ON ONLY control_plane.auth_events USING btree (ts, "
        "id)",
        "CREATE INDEX auth_events_user_id_ts_idx ON ONLY control_plane.auth_events "
        "USING btree (user_id, ts DESC)",
    },
}

SCHEMA_IDENTITY_TABLES = list(EXPECTED_COLUMNS)
SEQUENCE_PRIVILEGES = ["USAGE", "SELECT", "UPDATE"]
EXPECTED_SEQUENCE_GRANTS: dict[str, set[str]] = {
    "app_rw": {"USAGE", "SELECT"},
    "app_backup": set(),
}


async def test_catalog_matches_data_model(pg_dsn: str) -> None:
    """Колонки, ограничения, индексы, ключ партиционирования и последовательность — по §4.2.1."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        for table in SCHEMA_IDENTITY_TABLES:
            await assert_table_matches(
                conn,
                table,
                EXPECTED_COLUMNS[table],
                EXPECTED_CONSTRAINTS[table],
                EXPECTED_INDEXES[table],
            )

        partition_key = await conn.fetchval(
            "select pg_get_partkeydef('control_plane.auth_events'::regclass)"
        )
        assert partition_key == "RANGE (ts)", "auth_events партиционирована по суткам ts (§4.5)"
        sequence = await conn.fetchrow(
            "select s.seqtypid::regtype::text as type, d.refobjid::regclass::text as tbl, "
            "a.attname from pg_sequence s join pg_depend d on d.objid = s.seqrelid "
            "and d.deptype = 'a' join pg_attribute a on a.attrelid = d.refobjid "
            "and a.attnum = d.refobjsubid where s.seqrelid = "
            "'control_plane.auth_events_id_seq'::regclass"
        )
        assert sequence is not None and tuple(sequence) == ("bigint", "auth_events", "id"), (
            "auth_events_id_seq — bigint, OWNED BY auth_events.id"
        )
    finally:
        await conn.close()


@pytest.mark.parametrize("table", SCHEMA_IDENTITY_TABLES)
async def test_app_rw_privileges(pg_dsn: str, table: str) -> None:
    """TC-UNIT-01: app_rw — ровно DML, app_backup — только SELECT на каждой таблице группы."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        assert await table_grants(conn, table) == EXPECTED_TABLE_GRANTS
    finally:
        await conn.close()


async def test_sequence_privileges(pg_dsn: str) -> None:
    """auth_events_id_seq: app_rw — USAGE и SELECT (иначе INSERT в auth_events невозможен),
    app_backup — ничего (§4.6)."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        actual = {
            role: {
                priv
                for priv in SEQUENCE_PRIVILEGES
                if await conn.fetchval(
                    "select has_sequence_privilege($1, 'control_plane.auth_events_id_seq', $2)",
                    role,
                    priv,
                )
            }
            for role in EXPECTED_SEQUENCE_GRANTS
        }
        assert actual == EXPECTED_SEQUENCE_GRANTS
    finally:
        await conn.close()
