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

Column = tuple[str, str, bool, str | None]  # имя, тип (udt_name), допускает NULL, умолчание

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
        "CREATE UNIQUE INDEX users_pkey ON public.users USING btree (id)",
        "CREATE UNIQUE INDEX users_email_key ON public.users USING btree (email)",
    },
    "admin_users": {
        "CREATE UNIQUE INDEX admin_users_pkey ON public.admin_users USING btree (id)",
        "CREATE UNIQUE INDEX admin_users_email_key ON public.admin_users USING btree (email)",
    },
    "admin_recovery_codes": {
        "CREATE UNIQUE INDEX admin_recovery_codes_pkey ON public.admin_recovery_codes "
        "USING btree (id)",
        "CREATE INDEX admin_recovery_codes_unused_idx ON public.admin_recovery_codes "
        "USING btree (admin_user_id) WHERE (used_at IS NULL)",
    },
    "email_tokens": {
        "CREATE UNIQUE INDEX email_tokens_pkey ON public.email_tokens USING btree (id)",
        "CREATE UNIQUE INDEX email_tokens_token_hash_key ON public.email_tokens "
        "USING btree (token_hash)",
        "CREATE INDEX email_tokens_user_id_idx ON public.email_tokens USING btree (user_id)",
    },
    "auth_events": {
        "CREATE UNIQUE INDEX auth_events_pkey ON ONLY public.auth_events USING btree (ts, id)",
        "CREATE INDEX auth_events_user_id_ts_idx ON ONLY public.auth_events "
        "USING btree (user_id, ts DESC)",
    },
}

SCHEMA_IDENTITY_TABLES = list(EXPECTED_COLUMNS)
TABLE_PRIVILEGES = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]
SEQUENCE_PRIVILEGES = ["USAGE", "SELECT", "UPDATE"]
EXPECTED_TABLE_GRANTS: dict[str, set[str]] = {
    "app_rw": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "app_backup": {"SELECT"},
}
EXPECTED_SEQUENCE_GRANTS: dict[str, set[str]] = {
    "app_rw": {"USAGE", "SELECT"},
    "app_backup": set(),
}


async def test_catalog_matches_data_model(pg_dsn: str) -> None:
    """Колонки, ограничения, индексы, ключ партиционирования и последовательность — по §4.2.1."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        for table in SCHEMA_IDENTITY_TABLES:
            columns = await conn.fetch(
                "select column_name, udt_name, is_nullable, column_default "
                "from information_schema.columns where table_schema = 'public' "
                "and table_name = $1 order by ordinal_position",
                table,
            )
            actual: list[Column] = [
                (r["column_name"], r["udt_name"], r["is_nullable"] == "YES", r["column_default"])
                for r in columns
            ]
            assert actual == EXPECTED_COLUMNS[table], f"{table}: колонки расходятся с §4.2.1"

            constraints = await conn.fetch(
                "select contype::text as kind, pg_get_constraintdef(oid) as def "
                "from pg_constraint where conrelid = ('public.' || $1)::regclass "
                "and contype in ('p', 'u', 'f', 'c', 'x')",
                table,
            )
            assert {(r["kind"], r["def"]) for r in constraints} == EXPECTED_CONSTRAINTS[table], (
                f"{table}: ограничения расходятся с §4.2.1"
            )

            indexes = await conn.fetch(
                "select indexdef from pg_indexes where schemaname = 'public' and tablename = $1",
                table,
            )
            assert {r["indexdef"] for r in indexes} == EXPECTED_INDEXES[table], (
                f"{table}: индексы расходятся с §4.2.1"
            )

        partition_key = await conn.fetchval(
            "select pg_get_partkeydef('public.auth_events'::regclass)"
        )
        assert partition_key == "RANGE (ts)", "auth_events партиционирована по суткам ts (§4.5)"
        sequence = await conn.fetchrow(
            "select s.seqtypid::regtype::text as type, d.refobjid::regclass::text as tbl, "
            "a.attname from pg_sequence s join pg_depend d on d.objid = s.seqrelid "
            "and d.deptype = 'a' join pg_attribute a on a.attrelid = d.refobjid "
            "and a.attnum = d.refobjsubid where s.seqrelid = 'public.auth_events_id_seq'::regclass"
        )
        assert sequence is not None and tuple(sequence) == ("bigint", "auth_events", "id"), (
            "auth_events_id_seq — bigint, OWNED BY auth_events.id"
        )
    finally:
        await conn.close()


async def table_grants(conn: asyncpg.Connection, table: str) -> dict[str, set[str]]:
    """Привилегии ролей app_rw/app_backup на таблицу по has_table_privilege."""
    return {
        role: {
            priv
            for priv in TABLE_PRIVILEGES
            if await conn.fetchval("select has_table_privilege($1, $2, $3)", role, table, priv)
        }
        for role in EXPECTED_TABLE_GRANTS
    }


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
                    "select has_sequence_privilege($1, 'public.auth_events_id_seq', $2)",
                    role,
                    priv,
                )
            }
            for role in EXPECTED_SEQUENCE_GRANTS
        }
        assert actual == EXPECTED_SEQUENCE_GRANTS
    finally:
        await conn.close()
