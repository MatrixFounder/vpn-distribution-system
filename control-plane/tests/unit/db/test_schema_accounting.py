"""Задача 001.08, стражи схемы «учёт трафика, гранты, адреса, функции обслуживания партиций».

``test_catalog_matches_data_model`` сверяет колонки, ограничения и индексы десяти таблиц §4.2.5
и реестра ``partition_policies`` с data-model.md плюс отклонения, объявленные в задаче; ключи
партиционирования ``traffic_lines``/``traffic_hourly``; триггер монотонности.
``test_app_rw_privileges``
(TC-UNIT-01): права по §4.6 — ``traffic_lines`` без UPDATE/DELETE, ``traffic_hourly`` без DELETE,
``partition_policies`` только чтение. ``test_maintenance_functions``: ``ensure_partitions`` и
``drop_expired_partitions`` — SECURITY DEFINER, владелец ``app_owner``, ``search_path`` закреплён,
EXECUTE у ``app_rw`` и ни у кого больше. Без базы тесты падают, не пропускаются.
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
    "traffic_reports": [
        ("node_id", "uuid", False, None),
        ("counter_epoch", "uuid", False, None),
        ("report_seq", "int8", False, None),
        ("period_start", "timestamptz", False, None),
        ("period_end", "timestamptz", False, None),
        ("received_at", "timestamptz", False, "now()"),
        ("status", "report_status", False, None),
        ("node_rx_bytes", "int8", False, None),
        ("node_tx_bytes", "int8", False, None),
    ],
    "traffic_lines": [
        ("node_id", "uuid", False, None),
        ("counter_epoch", "uuid", False, None),
        ("report_seq", "int8", False, None),
        ("user_id", "uuid", False, None),
        ("period_start", "timestamptz", False, None),
        ("period_end", "timestamptz", False, None),
        ("raw_uplink_bytes", "int8", False, None),
        ("raw_downlink_bytes", "int8", False, None),
        ("billable_bytes", "int8", False, None),
        ("multiplier_milli", "int4", False, None),
        ("billing_group_id", "uuid", False, None),
    ],
    "traffic_hourly": [
        ("user_id", "uuid", False, None),
        ("node_id", "uuid", False, None),
        ("hour_start", "timestamptz", False, None),
        ("raw_uplink_bytes", "int8", False, None),
        ("raw_downlink_bytes", "int8", False, None),
        ("billable_bytes", "int8", False, None),
        ("multiplier_milli", "int4", False, None),
        ("billing_group_id", "uuid", False, None),
    ],
    "traffic_daily": [
        ("user_id", "uuid", False, None),
        ("node_id", "uuid", False, None),
        ("day", "date", False, None),
        ("raw_uplink_bytes", "int8", False, None),
        ("raw_downlink_bytes", "int8", False, None),
        ("billable_bytes", "int8", False, None),
    ],
    "node_interface_hourly": [
        ("node_id", "uuid", False, None),
        ("hour_start", "timestamptz", False, None),
        ("rx_bytes", "int8", False, None),
        ("tx_bytes", "int8", False, None),
    ],
    "traffic_gaps": [
        ("id", "uuid", False, "uuidv7()"),
        ("node_id", "uuid", False, None),
        ("gap_start", "timestamptz", False, None),
        ("gap_end", "timestamptz", False, None),
        ("reason", "text", False, None),
        ("estimated_bytes", "int8", True, None),
    ],
    "reconciliation_runs": [
        ("id", "uuid", False, "uuidv7()"),
        ("kind", "reconciliation_kind", False, None),
        ("scope", "jsonb", False, "'{}'::jsonb"),
        ("expected", "int8", False, None),
        ("actual", "int8", False, None),
        ("delta_pct", "numeric", False, None),
        ("status", "text", False, None),
        ("created_at", "timestamptz", False, "now()"),
    ],
    "quota_grants": [
        ("id", "uuid", False, "uuidv7()"),
        ("user_id", "uuid", False, None),
        ("node_id", "uuid", False, None),
        ("period_id", "uuid", False, None),
        ("grant_bytes", "int8", False, None),
        ("consumed_bytes", "int8", False, "0"),
        ("issued_at", "timestamptz", False, "now()"),
        ("issued_seq", "int8", False, None),
        ("superseded_at", "timestamptz", True, None),
    ],
    "user_online_ips": [
        ("user_id", "uuid", False, None),
        ("node_id", "uuid", False, None),
        ("ip", "inet", False, None),
        ("last_seen", "timestamptz", False, "now()"),
    ],
    "user_blocked_ips": [
        ("user_id", "uuid", False, None),
        ("ip", "inet", False, None),
        ("blocked_since", "timestamptz", False, "now()"),
    ],
    "partition_policies": [
        ("table_name", "text", False, None),
        ("retention_days", "int4", False, None),
        ("revoke_from_app_rw", "_text", False, "'{}'::text[]"),
    ],
}

EXPECTED_CONSTRAINTS: dict[str, set[tuple[str, str]]] = {
    "traffic_reports": {
        ("c", "CHECK ((node_rx_bytes >= 0))"),
        ("c", "CHECK ((node_tx_bytes >= 0))"),
        ("c", "CHECK ((period_end > period_start))"),
        ("f", "FOREIGN KEY (node_id) REFERENCES nodes(id)"),
        ("p", "PRIMARY KEY (node_id, counter_epoch, report_seq)"),
    },
    "traffic_lines": {
        ("c", "CHECK ((billable_bytes >= 0))"),
        (
            "c",
            "CHECK ((((multiplier_milli >= 0) AND (multiplier_milli <= 10000)) AND "
            "((multiplier_milli % 100) = 0)))",
        ),
        ("c", "CHECK ((period_end > period_start))"),
        ("c", "CHECK ((raw_downlink_bytes >= 0))"),
        ("c", "CHECK ((raw_uplink_bytes >= 0))"),
        ("p", "PRIMARY KEY (period_start, node_id, counter_epoch, report_seq, user_id)"),
    },
    "traffic_hourly": {
        ("c", "CHECK ((billable_bytes >= 0))"),
        (
            "c",
            "CHECK ((((multiplier_milli >= 0) AND (multiplier_milli <= 10000)) AND "
            "((multiplier_milli % 100) = 0)))",
        ),
        ("c", "CHECK ((raw_downlink_bytes >= 0))"),
        ("c", "CHECK ((raw_uplink_bytes >= 0))"),
        ("p", "PRIMARY KEY (hour_start, user_id, node_id, billing_group_id, multiplier_milli)"),
    },
    "traffic_daily": {
        ("c", "CHECK ((billable_bytes >= 0))"),
        ("c", "CHECK ((raw_downlink_bytes >= 0))"),
        ("c", "CHECK ((raw_uplink_bytes >= 0))"),
        ("p", "PRIMARY KEY (user_id, node_id, day)"),
    },
    "node_interface_hourly": {
        ("c", "CHECK ((rx_bytes >= 0))"),
        ("c", "CHECK ((tx_bytes >= 0))"),
        ("p", "PRIMARY KEY (node_id, hour_start)"),
    },
    "traffic_gaps": {
        ("c", "CHECK ((estimated_bytes >= 0))"),
        ("c", "CHECK ((gap_end > gap_start))"),
        ("f", "FOREIGN KEY (node_id) REFERENCES nodes(id)"),
        ("p", "PRIMARY KEY (id)"),
    },
    "reconciliation_runs": {
        ("p", "PRIMARY KEY (id)"),
    },
    "quota_grants": {
        ("c", "CHECK ((consumed_bytes >= 0))"),
        ("c", "CHECK ((grant_bytes >= 0))"),
        ("f", "FOREIGN KEY (node_id) REFERENCES nodes(id)"),
        ("f", "FOREIGN KEY (period_id) REFERENCES subscription_periods(id)"),
        ("f", "FOREIGN KEY (user_id) REFERENCES users(id)"),
        ("p", "PRIMARY KEY (id)"),
    },
    "user_online_ips": {
        ("p", "PRIMARY KEY (user_id, node_id, ip)"),
    },
    "user_blocked_ips": {
        ("p", "PRIMARY KEY (user_id, ip)"),
    },
    "partition_policies": {
        ("c", "CHECK ((retention_days > 0))"),
        ("c", "CHECK ((table_name ~ '^[a-z_]+$'::text))"),
        (
            "c",
            "CHECK ((revoke_from_app_rw <@ ARRAY['SELECT'::text, 'INSERT'::text, "
            "'UPDATE'::text, 'DELETE'::text]))",
        ),
        ("p", "PRIMARY KEY (table_name)"),
    },
}

EXPECTED_INDEXES: dict[str, set[str]] = {
    "traffic_reports": {
        "CREATE UNIQUE INDEX traffic_reports_pkey ON control_plane.traffic_reports USING btree "
        "(node_id, counter_epoch, report_seq)",
    },
    "traffic_lines": {
        "CREATE INDEX traffic_lines_node_id_period_start_idx ON ONLY control_plane.traffic_lines "
        "USING btree (node_id, period_start)",
        "CREATE INDEX traffic_lines_user_id_period_start_idx ON ONLY control_plane.traffic_lines "
        "USING btree (user_id, period_start)",
        "CREATE UNIQUE INDEX traffic_lines_pkey ON ONLY control_plane.traffic_lines USING btree "
        "(period_start, node_id, counter_epoch, report_seq, user_id)",
    },
    "traffic_hourly": {
        "CREATE INDEX traffic_hourly_node_id_hour_start_idx ON ONLY control_plane.traffic_hourly "
        "USING btree (node_id, hour_start)",
        "CREATE INDEX traffic_hourly_user_id_hour_start_idx ON ONLY control_plane.traffic_hourly "
        "USING btree (user_id, hour_start)",
        "CREATE UNIQUE INDEX traffic_hourly_pkey ON ONLY control_plane.traffic_hourly USING "
        "btree (hour_start, user_id, node_id, billing_group_id, multiplier_milli)",
    },
    "traffic_daily": {
        "CREATE INDEX traffic_daily_node_id_day_idx ON control_plane.traffic_daily USING btree "
        "(node_id, day)",
        "CREATE UNIQUE INDEX traffic_daily_pkey ON control_plane.traffic_daily USING btree "
        "(user_id, node_id, day)",
    },
    "node_interface_hourly": {
        "CREATE UNIQUE INDEX node_interface_hourly_pkey ON control_plane.node_interface_hourly "
        "USING btree (node_id, hour_start)",
    },
    "traffic_gaps": {
        "CREATE INDEX traffic_gaps_node_id_gap_start_idx ON control_plane.traffic_gaps USING "
        "btree (node_id, gap_start)",
        "CREATE UNIQUE INDEX traffic_gaps_pkey ON control_plane.traffic_gaps USING btree (id)",
    },
    "reconciliation_runs": {
        "CREATE INDEX reconciliation_runs_kind_created_at_idx ON "
        "control_plane.reconciliation_runs USING btree (kind, created_at)",
        "CREATE UNIQUE INDEX reconciliation_runs_pkey ON control_plane.reconciliation_runs USING "
        "btree (id)",
    },
    "quota_grants": {
        "CREATE INDEX quota_grants_period_id_idx ON control_plane.quota_grants USING btree "
        "(period_id)",
        "CREATE INDEX quota_grants_user_id_node_id_issued_at_idx ON control_plane.quota_grants "
        "USING btree (user_id, node_id, issued_at DESC)",
        "CREATE UNIQUE INDEX quota_grants_pkey ON control_plane.quota_grants USING btree (id)",
    },
    "user_online_ips": {
        "CREATE INDEX user_online_ips_user_id_last_seen_idx ON control_plane.user_online_ips "
        "USING btree (user_id, last_seen DESC)",
        "CREATE UNIQUE INDEX user_online_ips_pkey ON control_plane.user_online_ips USING btree "
        "(user_id, node_id, ip)",
    },
    "user_blocked_ips": {
        "CREATE UNIQUE INDEX user_blocked_ips_pkey ON control_plane.user_blocked_ips USING btree "
        "(user_id, ip)",
    },
    "partition_policies": {
        "CREATE UNIQUE INDEX partition_policies_pkey ON control_plane.partition_policies USING "
        "btree (table_name)",
    },
}

SCHEMA_ACCOUNTING_TABLES = list(EXPECTED_COLUMNS)
EXPECTED_GRANTS_BY_TABLE: dict[str, dict[str, set[str]]] = {
    "traffic_lines": {"app_rw": {"SELECT", "INSERT"}, "app_backup": {"SELECT"}},  # R-23
    "traffic_hourly": {"app_rw": {"SELECT", "INSERT", "UPDATE"}, "app_backup": {"SELECT"}},
    "partition_policies": {"app_rw": {"SELECT"}, "app_backup": {"SELECT"}},
}
EXPECTED_POLICIES = {  # §4.5
    "auth_events": (90, []),
    "subscription_access_log": (30, []),
    "node_metrics": (30, []),
    "traffic_lines": (14, ["UPDATE", "DELETE"]),
    "traffic_hourly": (90, ["DELETE"]),
}
MAINTENANCE_FUNCTIONS = ["ensure_partitions(integer)", "drop_expired_partitions()"]


async def test_catalog_matches_data_model(pg_dsn: str) -> None:
    """Таблицы группы, ключи партиционирования, реестр §4.5, триггер монотонности traffic_hourly."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        for table in SCHEMA_ACCOUNTING_TABLES:
            await assert_table_matches(
                conn,
                table,
                EXPECTED_COLUMNS[table],
                EXPECTED_CONSTRAINTS[table],
                EXPECTED_INDEXES[table],
            )
        for table, column in (("traffic_lines", "period_start"), ("traffic_hourly", "hour_start")):
            key = await conn.fetchval("select pg_get_partkeydef($1::regclass)", f"{SCHEMA}.{table}")
            assert key == f"RANGE ({column})", f"{table} партиционирована по суткам {column} (§4.5)"
        policies = {
            r["table_name"]: (r["retention_days"], list(r["revoke_from_app_rw"]))
            for r in await conn.fetch("select * from partition_policies")
        }
        assert policies == EXPECTED_POLICIES, "реестр партиций — сроки §4.5 и запреты §4.6"
        trigger = await conn.fetchrow(
            "select t.tgenabled::text as enabled, pg_get_triggerdef(t.oid) as def from "
            "pg_trigger t "
            "where t.tgrelid = $1::regclass and not t.tgisinternal",
            f"{SCHEMA}.traffic_hourly",
        )
        assert trigger is not None and trigger["enabled"] == "O", "триггер монотонности включён"
        assert trigger["def"] == (
            "CREATE TRIGGER traffic_hourly_only_grows BEFORE UPDATE ON "
            "control_plane.traffic_hourly "
            "FOR EACH ROW EXECUTE FUNCTION traffic_hourly_only_grows()"
        )
        typed = await conn.fetchrow(
            "select numeric_precision, numeric_scale from information_schema.columns "
            "where table_schema = $1 and table_name = 'reconciliation_runs' "
            "and column_name = 'delta_pct'",
            SCHEMA,
        )
        assert typed is not None and tuple(typed) == (6, 3), "delta_pct numeric(6,3)"
    finally:
        await conn.close()


@pytest.mark.parametrize("table", SCHEMA_ACCOUNTING_TABLES)
async def test_app_rw_privileges(pg_dsn: str, table: str) -> None:
    """TC-UNIT-01: app_rw — DML с запретами §4.6 (traffic_lines, traffic_hourly, реестр)."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        expected = EXPECTED_GRANTS_BY_TABLE.get(table, EXPECTED_TABLE_GRANTS)
        assert await table_grants(conn, table) == expected
    finally:
        await conn.close()


@pytest.mark.parametrize("function", MAINTENANCE_FUNCTIONS)
async def test_maintenance_functions(pg_dsn: str, function: str) -> None:
    """ensure_partitions / drop_expired_partitions: SECURITY DEFINER от app_owner с закреплённым
    search_path; EXECUTE только у app_rw (не у app_backup и не у PUBLIC)."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        row = await conn.fetchrow(
            "select pg_get_userbyid(proowner) as owner, prosecdef, proconfig, "
            "prorettype::regtype::text "
            "as returns from pg_proc where oid = $1::regprocedure",
            f"{SCHEMA}.{function}",
        )
        assert row is not None
        assert row["owner"] == "app_owner"
        assert row["prosecdef"] is True, "SECURITY DEFINER (§4.6)"
        assert list(row["proconfig"]) == [f"search_path={SCHEMA}, pg_temp", "TimeZone=UTC"], (
            "search_path и timezone закреплены"
        )
        assert row["returns"] == "integer"
        privileges = {
            role: await conn.fetchval(
                "select has_function_privilege($1, $2, 'EXECUTE')", role, f"{SCHEMA}.{function}"
            )
            for role in ("app_rw", "app_backup")
        }
        assert privileges == {"app_rw": True, "app_backup": False}
        public_execute = await conn.fetchval(
            "select coalesce(bool_or(a.grantee = 0), false) from pg_proc p, "
            "lateral aclexplode(p.proacl) a where p.oid = $1::regprocedure and a.privilege_type "
            "= 'EXECUTE'",
            f"{SCHEMA}.{function}",
        )
        assert public_execute is False, "EXECUTE у PUBLIC отозван (0001)"
    finally:
        await conn.close()
