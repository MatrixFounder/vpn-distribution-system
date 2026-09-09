"""Задача 001.09, стражи схемы «события, доставки, очередь задач, аудит, настройки» по каталогам
PostgreSQL.

``test_catalog_matches_data_model`` сверяет колонки, ограничения и индексы шести таблиц с
data-model.md §4.2.6 и §4.4 плюс отклонения, объявленные в задаче: частичные индексы ``jobs``
(выборка pending, уникальность ключа идемпотентности среди активных — R-46), три индекса
``audit_log``, два триггера append-only, строку ``settings.state_generation``.
``test_app_rw_privileges`` (TC-UNIT-01): права — по §4.6, включая запрет ``UPDATE``/``DELETE`` на
``audit_log`` у ``app_rw``; ``test_audit_log_privileges``: владелец без UPDATE/DELETE/TRUNCATE,
``app_audit_purge`` — DELETE и SELECT только колонки ``ts``; права на последовательности identity.
``test_purge_audit_log``: SECURITY DEFINER от ``app_audit_purge``, search_path и timezone
закреплены, EXECUTE только у ``app_rw`` (у PUBLIC отозван явно — умолчания 0001 функций
``app_audit_purge`` не касаются). Без базы тесты падают, не пропускаются.
"""

from __future__ import annotations

import asyncpg
import pytest

from ._introspect import (
    EXPECTED_TABLE_GRANTS,
    SCHEMA,
    Column,
    assert_table_matches,
    table_grants,
)

EXPECTED_COLUMNS: dict[str, list[Column]] = {
    "events": [
        ("id", "uuid", False, "uuidv7()"),
        ("type", "event_type", False, None),
        ("user_id", "uuid", True, None),
        ("node_id", "uuid", True, None),
        ("payload", "jsonb", False, "'{}'::jsonb"),
        ("dedup_key", "text", False, None),
        ("created_at", "timestamptz", False, "now()"),
    ],
    "email_deliveries": [
        ("id", "uuid", False, "uuidv7()"),
        ("event_id", "uuid", False, None),
        ("recipient", "citext", False, None),
        ("language", "text", False, None),
        ("status", "delivery_status", False, "'pending'::delivery_status"),
        ("attempts", "int4", False, "0"),
        ("last_error", "text", True, None),
        ("sent_at", "timestamptz", True, None),
    ],
    "webhook_deliveries": [
        ("id", "uuid", False, "uuidv7()"),
        ("event_id", "uuid", False, None),
        ("url", "text", False, None),
        ("status", "delivery_status", False, "'pending'::delivery_status"),
        ("attempts", "int4", False, "0"),
        ("next_attempt_at", "timestamptz", True, None),
        ("response_code", "int4", True, None),
    ],
    "jobs": [
        ("id", "int8", False, None),
        ("queue", "job_queue", False, None),
        ("type", "text", False, None),
        ("payload", "jsonb", False, "'{}'::jsonb"),
        ("idempotency_key", "text", False, None),
        ("run_at", "timestamptz", False, "now()"),
        ("attempts", "int4", False, "0"),
        ("max_attempts", "int4", False, None),
        ("locked_at", "timestamptz", True, None),
        ("locked_by", "text", True, None),
        ("status", "job_status", False, "'pending'::job_status"),
        ("last_error", "text", True, None),
        ("created_at", "timestamptz", False, "now()"),
        ("claimed_at", "timestamptz", True, None),  # 110 (001.74)
        ("finished_at", "timestamptz", True, None),  # 110 (001.74)
    ],
    "audit_log": [
        ("id", "int8", False, None),
        ("ts", "timestamptz", False, "now()"),
        ("actor_type", "actor_type", False, None),
        ("actor_id", "uuid", True, None),
        ("actor_role", "text", True, None),
        ("session_id", "text", True, None),
        ("impersonated_user_id", "uuid", True, None),
        ("user_agent", "text", True, None),
        ("action", "text", False, None),
        ("entity_type", "text", False, None),
        ("entity_id", "text", False, None),
        ("old_value", "jsonb", True, None),
        ("new_value", "jsonb", True, None),
        ("ip", "inet", True, None),
        ("result", "text", False, None),
    ],
    "settings": [
        ("key", "text", False, None),
        ("value", "jsonb", False, None),
        ("updated_at", "timestamptz", False, "now()"),
    ],
}

EXPECTED_CONSTRAINTS: dict[str, set[tuple[str, str]]] = {
    "events": {
        ("p", "PRIMARY KEY (id)"),
        ("u", "UNIQUE (dedup_key)"),
    },
    "email_deliveries": {
        ("c", "CHECK ((attempts >= 0))"),
        ("f", "FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE"),
        ("p", "PRIMARY KEY (id)"),
    },
    "webhook_deliveries": {
        ("c", "CHECK ((attempts >= 0))"),
        ("f", "FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE"),
        ("p", "PRIMARY KEY (id)"),
    },
    "jobs": {
        ("c", "CHECK (((locked_at IS NULL) = (locked_by IS NULL)))"),
        ("c", "CHECK ((attempts >= 0))"),
        ("c", "CHECK ((max_attempts > 0))"),
        ("p", "PRIMARY KEY (id)"),
    },
    "audit_log": {
        ("c", "CHECK ((result = ANY (ARRAY['success'::text, 'denied'::text, 'error'::text])))"),
        ("p", "PRIMARY KEY (id)"),
    },
    "settings": {
        ("p", "PRIMARY KEY (key)"),
    },
}

EXPECTED_INDEXES: dict[str, set[str]] = {
    "events": {
        "CREATE UNIQUE INDEX events_dedup_key_key ON control_plane.events USING btree (dedup_key)",
        "CREATE UNIQUE INDEX events_pkey ON control_plane.events USING btree (id)",
    },
    "email_deliveries": {
        "CREATE INDEX email_deliveries_event_id_idx ON control_plane.email_deliveries USING btree "
        "(event_id)",
        "CREATE UNIQUE INDEX email_deliveries_pkey ON control_plane.email_deliveries USING btree "
        "(id)",
    },
    "webhook_deliveries": {
        "CREATE INDEX webhook_deliveries_event_id_idx ON control_plane.webhook_deliveries USING "
        "btree (event_id)",
        "CREATE UNIQUE INDEX webhook_deliveries_pkey ON control_plane.webhook_deliveries USING "
        "btree (id)",
    },
    "jobs": {
        "CREATE INDEX jobs_pending_idx ON control_plane.jobs USING btree (queue, status, run_at) "
        "WHERE (status = 'pending'::job_status)",
        "CREATE UNIQUE INDEX jobs_idempotency_active_idx ON control_plane.jobs USING btree "
        "(idempotency_key) WHERE (status = ANY (ARRAY['pending'::job_status, "
        "'running'::job_status]))",
        "CREATE UNIQUE INDEX jobs_pkey ON control_plane.jobs USING btree (id)",
        "CREATE INDEX jobs_claimed_at_idx ON control_plane.jobs USING btree (claimed_at) "
        "WHERE (claimed_at IS NOT NULL)",  # 110 (001.74)
    },
    "audit_log": {
        "CREATE INDEX audit_log_actor_id_ts_idx ON control_plane.audit_log USING btree "
        "(actor_id, ts)",
        "CREATE INDEX audit_log_entity_type_entity_id_idx ON control_plane.audit_log USING btree "
        "(entity_type, entity_id)",
        "CREATE INDEX audit_log_ts_idx ON control_plane.audit_log USING btree (ts)",
        "CREATE UNIQUE INDEX audit_log_pkey ON control_plane.audit_log USING btree (id)",
    },
    "settings": {
        "CREATE UNIQUE INDEX settings_pkey ON control_plane.settings USING btree (key)",
    },
}

SCHEMA_OPS_TABLES = list(EXPECTED_COLUMNS)
# audit_log — append-only (§7.2, R-37): приложению только чтение и вставка.
EXPECTED_GRANTS_BY_TABLE: dict[str, dict[str, set[str]]] = {
    "audit_log": {"app_rw": {"SELECT", "INSERT"}, "app_backup": {"SELECT"}},
}
AUDIT_PRIVILEGES = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"]
SEQUENCES = ["jobs_id_seq", "audit_log_id_seq"]
EXPECTED_SEQUENCE_GRANTS: dict[str, set[str]] = {"app_rw": {"USAGE", "SELECT"}, "app_backup": set()}
EXPECTED_TRIGGERS = {
    "CREATE TRIGGER audit_log_immutable BEFORE DELETE OR UPDATE ON control_plane.audit_log "
    "FOR EACH ROW EXECUTE FUNCTION audit_log_immutable()",
    "CREATE TRIGGER audit_log_immutable_truncate BEFORE TRUNCATE ON control_plane.audit_log "
    "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable()",
}


async def test_catalog_matches_data_model(pg_dsn: str) -> None:
    """Шесть таблиц группы, частичные индексы jobs, триггеры audit_log, строка state_generation."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        for table in SCHEMA_OPS_TABLES:
            await assert_table_matches(
                conn,
                table,
                EXPECTED_COLUMNS[table],
                EXPECTED_CONSTRAINTS[table],
                EXPECTED_INDEXES[table],
            )
        triggers = await conn.fetch(
            "select t.tgenabled::text as enabled, pg_get_triggerdef(t.oid) as def from "
            "pg_trigger t where t.tgrelid = $1::regclass and not t.tgisinternal",
            f"{SCHEMA}.audit_log",
        )
        assert {r["enabled"] for r in triggers} == {"O"}, "триггеры audit_log включены"
        assert {r["def"] for r in triggers} == EXPECTED_TRIGGERS
        assert (
            await conn.fetchval(
                "select has_function_privilege('app_rw', $1, 'EXECUTE')",
                f"{SCHEMA}.audit_log_immutable()",
            )
            is False
        ), "триггерная функция недоступна приложению напрямую"
        for sequence, column in zip(SEQUENCES, ["jobs", "audit_log"], strict=True):
            owned = await conn.fetchrow(
                "select d.refobjid::regclass::text as tbl, a.attname, a.attidentity::text as ident "
                "from pg_depend d join pg_attribute a on a.attrelid = d.refobjid "
                "and a.attnum = d.refobjsubid where d.objid = $1::regclass and d.deptype = 'i'",
                f"{SCHEMA}.{sequence}",
            )
            assert owned is not None and tuple(owned) == (column, "id", "a"), (
                f"{sequence}: identity ALWAYS колонки {column}.id"
            )
        generation = await conn.fetchval(
            "select value from settings where key = 'state_generation'"
        )
        assert generation == "0", "settings.state_generation засеяна нулём (§4.2.6)"
    finally:
        await conn.close()


@pytest.mark.parametrize("table", SCHEMA_OPS_TABLES)
async def test_app_rw_privileges(pg_dsn: str, table: str) -> None:
    """TC-UNIT-01: app_rw — DML (для audit_log только SELECT, INSERT), app_backup — SELECT."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        expected = EXPECTED_GRANTS_BY_TABLE.get(table, EXPECTED_TABLE_GRANTS)
        assert await table_grants(conn, table) == expected
    finally:
        await conn.close()


async def test_audit_log_privileges(pg_dsn: str) -> None:
    """Append-only на привилегиях (§4.6, §7.2): владелец app_owner (и app_migrate через него) без
    UPDATE/DELETE/TRUNCATE; app_audit_purge — только DELETE и SELECT колонки ts."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        table = f"{SCHEMA}.audit_log"
        privileges = {
            role: {
                priv
                for priv in AUDIT_PRIVILEGES
                if await conn.fetchval("select has_table_privilege($1, $2, $3)", role, table, priv)
            }
            for role in ("app_owner", "app_migrate", "app_audit_purge")
        }
        assert privileges == {
            "app_owner": {"SELECT", "INSERT"},
            "app_migrate": {"SELECT", "INSERT"},
            "app_audit_purge": {"DELETE"},
        }, privileges
        readable = await conn.fetch(
            "select attname from pg_attribute where attrelid = $1::regclass and attnum > 0 "
            "and not attisdropped and has_column_privilege('app_audit_purge', attrelid, attnum, "
            "'SELECT') order by attnum",
            table,
        )
        assert [r["attname"] for r in readable] == ["ts"], "роль очистки читает только ts"
    finally:
        await conn.close()


@pytest.mark.parametrize("sequence", SEQUENCES)
async def test_sequence_privileges(pg_dsn: str, sequence: str) -> None:
    """Последовательности identity: app_rw — USAGE, SELECT; app_backup — ничего."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        for role, expected in EXPECTED_SEQUENCE_GRANTS.items():
            actual = {
                privilege
                for privilege in ("USAGE", "SELECT", "UPDATE")
                if await conn.fetchval(
                    "select has_sequence_privilege($1, $2, $3)",
                    role,
                    f"{SCHEMA}.{sequence}",
                    privilege,
                )
            }
            assert actual == expected, f"{sequence}: {role}"
    finally:
        await conn.close()


async def test_purge_audit_log(pg_dsn: str) -> None:
    """purge_audit_log: SECURITY DEFINER от app_audit_purge (единственный держатель DELETE),
    search_path и timezone закреплены, возвращает bigint, без параметров; EXECUTE только у app_rw —
    не у app_backup, не у app_owner и не у PUBLIC (для функций app_audit_purge умолчание PUBLIC
    отозвано явно в 090)."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        function = f"{SCHEMA}.purge_audit_log()"
        row = await conn.fetchrow(
            "select pg_get_userbyid(proowner) as owner, prosecdef, proconfig, "
            "prorettype::regtype::text as returns, pronargs from pg_proc "
            "where oid = $1::regprocedure",
            function,
        )
        assert row is not None
        assert row["owner"] == "app_audit_purge", "владелец — держатель DELETE на audit_log"
        assert row["prosecdef"] is True, "SECURITY DEFINER (§4.6)"
        assert list(row["proconfig"]) == [f"search_path={SCHEMA}, pg_temp", "TimeZone=UTC"]
        assert (row["returns"], row["pronargs"]) == ("bigint", 0), "срок хранения не параметр"
        privileges = {
            role: await conn.fetchval(
                "select has_function_privilege($1, $2, 'EXECUTE')", role, function
            )
            for role in ("app_rw", "app_backup", "app_owner")
        }
        assert privileges == {"app_rw": True, "app_backup": False, "app_owner": False}
        public_execute = await conn.fetchval(
            "select coalesce(bool_or(a.grantee = 0), false) from pg_proc p, "
            "lateral aclexplode(p.proacl) a where p.oid = $1::regprocedure "
            "and a.privilege_type = 'EXECUTE'",
            function,
        )
        assert public_execute is False, "EXECUTE у PUBLIC отозван (0001)"
    finally:
        await conn.close()
