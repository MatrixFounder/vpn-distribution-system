"""Задача 001.05, стражи схемы «тарифы, группы, коэффициенты» по каталогам PostgreSQL.

``test_catalog_matches_data_model`` сверяет колонки, ограничения (PK/UNIQUE/FK/CHECK/EXCLUDE) и
индексы семи таблиц с data-model.md §4.2.2 и §4.4 плюс отклонения, объявленные в задаче
(умолчания, CHECK порядка границ интервалов, индексы обратного поиска). ``test_app_rw_privileges``
(TC-UNIT-01): права — ровно по §4.6. Без базы тесты падают, не пропускаются.
"""

from __future__ import annotations

import asyncpg
import pytest

from ._introspect import EXPECTED_TABLE_GRANTS, Column, assert_table_matches, table_grants

MULTIPLIER_CHECK = (
    "CHECK ((((multiplier_milli >= 0) AND (multiplier_milli <= 10000)) "
    "AND ((multiplier_milli % 100) = 0)))"
)
OVERRIDE_CHECK = (
    "CHECK ((((multiplier_override_milli >= 0) AND (multiplier_override_milli <= 10000)) "
    "AND ((multiplier_override_milli % 100) = 0)))"
)
VALID_RANGE_CHECK = "CHECK (((valid_to IS NULL) OR (valid_to > valid_from)))"

EXPECTED_COLUMNS: dict[str, list[Column]] = {
    "plans": [
        ("id", "uuid", False, "uuidv7()"),
        ("name", "text", False, None),
        ("price_amount", "numeric", True, None),  # numeric(12,2), справочно (О-2)
        ("price_currency", "bpchar", True, None),  # char(3), ОВ-19
        ("duration_days", "int4", False, None),
        ("traffic_limit_bytes", "int8", True, None),  # NULL = Unlimited
        ("device_limit", "int4", True, None),
        ("status", "plan_status", False, "'active'::plan_status"),
        ("created_at", "timestamptz", False, "now()"),
        ("updated_at", "timestamptz", False, "now()"),
    ],
    "plan_protocols": [
        ("plan_id", "uuid", False, None),
        ("profile", "inbound_profile", False, None),
    ],
    "access_groups": [
        ("id", "uuid", False, "uuidv7()"),
        ("name", "text", False, None),
        ("description", "text", False, "''::text"),
    ],
    "plan_access_groups": [
        ("plan_id", "uuid", False, None),
        ("access_group_id", "uuid", False, None),
    ],
    "billing_groups": [
        ("id", "uuid", False, "uuidv7()"),
        ("name", "text", False, None),
    ],
    "billing_group_multipliers": [
        ("id", "uuid", False, "uuidv7()"),
        ("billing_group_id", "uuid", False, None),
        ("multiplier_milli", "int4", False, None),
        ("valid_from", "timestamptz", False, None),
        ("valid_to", "timestamptz", True, None),  # NULL = действует
    ],
    "node_billing_assignments": [
        ("id", "uuid", False, "uuidv7()"),
        ("node_id", "uuid", False, None),  # FK → nodes добавлен миграцией 060
        ("billing_group_id", "uuid", False, None),
        ("multiplier_override_milli", "int4", True, None),
        ("valid_from", "timestamptz", False, None),
        ("valid_to", "timestamptz", True, None),
    ],
}

EXPECTED_CONSTRAINTS: dict[str, set[tuple[str, str]]] = {
    "plans": {
        ("p", "PRIMARY KEY (id)"),
        ("u", "UNIQUE (name)"),
        ("c", "CHECK ((duration_days > 0))"),
    },
    "plan_protocols": {
        ("p", "PRIMARY KEY (plan_id, profile)"),
        ("f", "FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE CASCADE"),
    },
    "access_groups": {("p", "PRIMARY KEY (id)"), ("u", "UNIQUE (name)")},
    "plan_access_groups": {
        ("p", "PRIMARY KEY (plan_id, access_group_id)"),
        ("f", "FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE CASCADE"),
        ("f", "FOREIGN KEY (access_group_id) REFERENCES access_groups(id) ON DELETE CASCADE"),
    },
    "billing_groups": {("p", "PRIMARY KEY (id)"), ("u", "UNIQUE (name)")},
    "billing_group_multipliers": {
        ("p", "PRIMARY KEY (id)"),
        ("f", "FOREIGN KEY (billing_group_id) REFERENCES billing_groups(id)"),
        ("c", MULTIPLIER_CHECK),
        ("c", VALID_RANGE_CHECK),
        (
            "x",
            "EXCLUDE USING gist (billing_group_id WITH =, tstzrange(valid_from, valid_to) WITH &&)",
        ),
    },
    "node_billing_assignments": {
        ("p", "PRIMARY KEY (id)"),
        ("f", "FOREIGN KEY (billing_group_id) REFERENCES billing_groups(id)"),
        ("f", "FOREIGN KEY (node_id) REFERENCES nodes(id)"),  # добавлен миграцией 060
        ("c", OVERRIDE_CHECK),
        ("c", VALID_RANGE_CHECK),
        ("x", "EXCLUDE USING gist (node_id WITH =, tstzrange(valid_from, valid_to) WITH &&)"),
    },
}

EXPECTED_INDEXES: dict[str, set[str]] = {
    "plans": {
        "CREATE UNIQUE INDEX plans_pkey ON control_plane.plans USING btree (id)",
        "CREATE UNIQUE INDEX plans_name_key ON control_plane.plans USING btree (name)",
    },
    "plan_protocols": {
        "CREATE UNIQUE INDEX plan_protocols_pkey ON control_plane.plan_protocols "
        "USING btree (plan_id, profile)",
    },
    "access_groups": {
        "CREATE UNIQUE INDEX access_groups_pkey ON control_plane.access_groups USING btree (id)",
        "CREATE UNIQUE INDEX access_groups_name_key ON control_plane.access_groups USING btree "
        "(name)",
    },
    "plan_access_groups": {
        "CREATE UNIQUE INDEX plan_access_groups_pkey ON control_plane.plan_access_groups "
        "USING btree (plan_id, access_group_id)",
        "CREATE INDEX plan_access_groups_access_group_id_idx ON control_plane.plan_access_groups "
        "USING btree (access_group_id)",
    },
    "billing_groups": {
        "CREATE UNIQUE INDEX billing_groups_pkey ON control_plane.billing_groups USING btree (id)",
        "CREATE UNIQUE INDEX billing_groups_name_key ON control_plane.billing_groups USING btree "
        "(name)",
    },
    "billing_group_multipliers": {
        "CREATE UNIQUE INDEX billing_group_multipliers_pkey ON "
        "control_plane.billing_group_multipliers "
        "USING btree (id)",
        "CREATE INDEX billing_group_multipliers_billing_group_id_tstzrange_excl "
        "ON control_plane.billing_group_multipliers USING gist "
        "(billing_group_id, tstzrange(valid_from, valid_to))",
    },
    "node_billing_assignments": {
        "CREATE UNIQUE INDEX node_billing_assignments_pkey ON "
        "control_plane.node_billing_assignments "
        "USING btree (id)",
        "CREATE INDEX node_billing_assignments_node_id_tstzrange_excl "
        "ON control_plane.node_billing_assignments USING gist (node_id, tstzrange(valid_from, "
        "valid_to))",
        "CREATE INDEX node_billing_assignments_billing_group_id_idx "
        "ON control_plane.node_billing_assignments USING btree (billing_group_id)",
    },
}

SCHEMA_CATALOG_TABLES = list(EXPECTED_COLUMNS)


async def test_catalog_matches_data_model(pg_dsn: str) -> None:
    """Семь таблиц группы: колонки, ограничения, индексы по §4.2.2/§4.4; numeric(12,2), char(3)."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        for table in SCHEMA_CATALOG_TABLES:
            await assert_table_matches(
                conn,
                table,
                EXPECTED_COLUMNS[table],
                EXPECTED_CONSTRAINTS[table],
                EXPECTED_INDEXES[table],
            )
        typed = await conn.fetchrow(
            "select numeric_precision, numeric_scale, "
            "(select character_maximum_length from information_schema.columns "
            " where table_schema = 'control_plane' and table_name = 'plans' and column_name = "
            "'price_currency') as currency_len "
            "from information_schema.columns where table_schema = 'control_plane' "
            "and table_name = 'plans' and column_name = 'price_amount'"
        )
        assert typed is not None and tuple(typed) == (12, 2, 3), "numeric(12,2), char(3) (§4.2.2)"
    finally:
        await conn.close()


@pytest.mark.parametrize("table", SCHEMA_CATALOG_TABLES)
async def test_app_rw_privileges(pg_dsn: str, table: str) -> None:
    """TC-UNIT-01: app_rw — ровно DML, app_backup — только SELECT на каждой таблице группы."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        assert await table_grants(conn, table) == EXPECTED_TABLE_GRANTS
    finally:
        await conn.close()
