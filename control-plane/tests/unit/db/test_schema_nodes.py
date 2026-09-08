"""Задача 001.06, стражи схемы «парк нод, inbound, состояние и команды» по каталогам PostgreSQL.

``test_catalog_matches_data_model`` сверяет колонки, ограничения (PK/UNIQUE/FK/CHECK) и индексы
тринадцати таблиц с data-model.md §4.2.3 и §4.4 плюс отклонения, объявленные в задаче (умолчания,
CHECK на пороги, индексы по FK, каскады только у node_access_groups и inbound_secrets), ключ
партиционирования ``node_metrics`` и внешний ключ ``node_billing_assignments.node_id → nodes``,
добавленный этой миграцией. ``test_app_rw_privileges`` (TC-UNIT-01) и права на последовательность
``node_ip_history_id_seq`` — по §4.6. Без базы тесты падают, не пропускаются.
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
    "nodes": [
        ("id", "uuid", False, "uuidv7()"),
        ("code", "text", False, None),
        ("name", "text", False, None),
        ("country", "bpchar", False, None),
        ("city", "text", False, None),
        ("provider", "text", False, None),
        ("public_ipv4", "inet", False, None),
        ("public_ipv6", "inet", True, None),
        ("fqdn", "text", True, None),
        ("status", "node_status", False, "'pending'::node_status"),
        ("status_changed_at", "timestamptz", False, "now()"),
        ("last_heartbeat_at", "timestamptz", True, None),
        ("missed_heartbeats", "int4", False, "0"),
        ("agent_version", "text", True, None),
        ("xray_version", "text", True, None),
        ("billing_group_id", "uuid", False, None),
        ("multiplier_milli", "int4", True, None),
        ("resync_required", "bool", False, "false"),
        ("ok_heartbeats", "int4", False, "0"),
        ("bandwidth_mbps", "int4", False, None),
        ("max_conn_per_ip", "int4", False, None),
        ("desired_config_version", "int4", False, "0"),
        ("applied_config_version", "int4", False, "0"),
        ("desired_users_seq", "int8", False, "0"),
        ("applied_users_seq", "int8", False, "0"),
        ("applied_at", "timestamptz", True, None),
        ("monthly_cost", "numeric", True, None),
        ("currency", "bpchar", True, None),
        ("provider_account", "text", True, None),
        ("cost_valid_from", "date", True, None),
        ("cost_valid_to", "date", True, None),
        ("traffic_included_bytes", "int8", True, None),
        ("traffic_overage_cost", "numeric", True, None),
        ("legal_profile", "jsonb", False, "'{}'::jsonb"),
        ("created_at", "timestamptz", False, "now()"),
        ("decommissioned_at", "timestamptz", True, None),
    ],
    "node_ip_history": [
        ("id", "int8", False, None),
        ("node_id", "uuid", False, None),
        ("public_ipv4", "inet", False, None),
        ("valid_from", "timestamptz", False, None),
        ("valid_to", "timestamptz", True, None),
    ],
    "node_access_groups": [
        ("node_id", "uuid", False, None),
        ("access_group_id", "uuid", False, None),
    ],
    "bootstrap_tokens": [
        ("id", "uuid", False, "uuidv7()"),
        ("node_id", "uuid", False, None),
        ("token_hash", "text", False, None),
        ("expires_at", "timestamptz", False, None),
        ("used_at", "timestamptz", True, None),
        ("created_by", "uuid", False, None),
    ],
    "node_identities": [
        ("id", "uuid", False, "uuidv7()"),
        ("node_id", "uuid", False, None),
        ("cert_fingerprint", "text", False, None),
        ("token_hash", "text", False, None),
        ("generation", "int4", False, None),
        ("issued_at", "timestamptz", False, "now()"),
        ("expires_at", "timestamptz", False, None),
        ("revoked_at", "timestamptz", True, None),
    ],
    "inbounds": [
        ("id", "uuid", False, "uuidv7()"),
        ("node_id", "uuid", False, None),
        ("profile", "inbound_profile", False, None),
        ("port", "int4", False, None),
        ("tag", "text", False, None),
        ("enabled", "bool", False, "true"),
        ("reality_public_key", "text", False, None),
        ("reality_short_ids", "_text", False, "'{}'::text[]"),
        ("reality_target", "text", False, None),
        ("reality_server_names", "_text", False, "'{}'::text[]"),
        ("reality_min_client_ver", "text", False, None),
        ("reality_max_client_ver", "text", True, None),
        ("reality_xver", "int4", False, "0"),
        ("reality_limit_fb_up", "int4", False, "0"),
        ("reality_limit_fb_down", "int4", False, "0"),
        ("client_fingerprint", "text", False, None),
        ("client_spider_x", "text", False, "''::text"),
        ("trusted_x_forwarded_for", "_inet", False, "'{}'::inet[]"),
        ("params", "jsonb", False, "'{}'::jsonb"),
        ("error_state", "text", True, None),
        ("error_since", "timestamptz", True, None),
    ],
    "inbound_secrets": [
        ("inbound_id", "uuid", False, None),
        ("private_key_enc", "bytea", False, None),
        ("key_version", "int4", False, "1"),
        ("rotated_at", "timestamptz", False, "now()"),
    ],
    "node_config_versions": [
        ("node_id", "uuid", False, None),
        ("config_version", "int4", False, None),
        ("config_json", "jsonb", False, None),
        ("checksum", "text", False, None),
        ("created_at", "timestamptz", False, "now()"),
    ],
    "node_user_credentials": [
        ("id", "uuid", False, "uuidv7()"),
        ("user_id", "uuid", False, None),
        ("node_id", "uuid", False, None),
        ("xray_email", "text", False, None),
        ("vless_uuid_enc", "bytea", False, None),
        ("trojan_password_enc", "bytea", False, None),
        ("version", "int4", False, "1"),
        ("rotated_at", "timestamptz", False, "now()"),
    ],
    "node_user_state": [
        ("node_id", "uuid", False, None),
        ("user_id", "uuid", False, None),
        ("state", "user_node_state", False, None),
        ("quota_grant_bytes", "int8", False, "0"),
        ("blocked_ips", "_inet", False, "'{}'::inet[]"),
        ("updated_seq", "int8", False, None),
        ("updated_at", "timestamptz", False, "now()"),
    ],
    "commands": [
        ("id", "uuid", False, "uuidv7()"),
        ("node_id", "uuid", False, None),
        ("type", "command_type", False, None),
        ("payload", "jsonb", False, "'{}'::jsonb"),
        ("issued_at", "timestamptz", False, "now()"),
        ("expires_at", "timestamptz", False, None),
        ("status", "command_status", False, "'issued'::command_status"),
        ("result", "jsonb", True, None),
    ],
    "node_metrics": [
        ("node_id", "uuid", False, None),
        ("ts", "timestamptz", False, None),
        ("cpu_pct", "float4", False, None),
        ("mem_pct", "float4", False, None),
        ("disk_pct", "float4", False, None),
        ("net_rx_bytes", "int8", False, None),
        ("net_tx_bytes", "int8", False, None),
        ("online_ips", "int4", False, None),
        ("connections", "int4", False, None),
    ],
    "node_country_availability": [
        ("node_id", "uuid", False, None),
        ("country", "bpchar", False, None),
        ("available", "bool", False, None),
        ("checked_at", "timestamptz", False, "now()"),
    ],
}

EXPECTED_CONSTRAINTS: dict[str, set[tuple[str, str]]] = {
    "nodes": {
        ("c", "CHECK ((bandwidth_mbps > 0))"),
        ("c", "CHECK ((max_conn_per_ip > 0))"),
        (
            "c",
            "CHECK ((((multiplier_milli >= 0) AND (multiplier_milli <= 10000)) AND "
            "((multiplier_milli % 100) = 0)))",
        ),
        ("f", "FOREIGN KEY (billing_group_id) REFERENCES billing_groups(id)"),
        ("p", "PRIMARY KEY (id)"),
        ("u", "UNIQUE (code)"),
    },
    "node_ip_history": {
        ("c", "CHECK (((valid_to IS NULL) OR (valid_to > valid_from)))"),
        ("f", "FOREIGN KEY (node_id) REFERENCES nodes(id)"),
        ("p", "PRIMARY KEY (id)"),
    },
    "node_access_groups": {
        ("f", "FOREIGN KEY (access_group_id) REFERENCES access_groups(id) ON DELETE CASCADE"),
        ("f", "FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE"),
        ("p", "PRIMARY KEY (node_id, access_group_id)"),
    },
    "bootstrap_tokens": {
        ("f", "FOREIGN KEY (created_by) REFERENCES admin_users(id)"),
        ("f", "FOREIGN KEY (node_id) REFERENCES nodes(id)"),
        ("p", "PRIMARY KEY (id)"),
        ("u", "UNIQUE (token_hash)"),
    },
    "node_identities": {
        ("f", "FOREIGN KEY (node_id) REFERENCES nodes(id)"),
        ("p", "PRIMARY KEY (id)"),
        ("u", "UNIQUE (cert_fingerprint)"),
    },
    "inbounds": {
        ("c", "CHECK (((port >= 1) AND (port <= 65535)))"),
        ("f", "FOREIGN KEY (node_id) REFERENCES nodes(id)"),
        ("p", "PRIMARY KEY (id)"),
        ("u", "UNIQUE (node_id, port)"),
        ("u", "UNIQUE (node_id, profile)"),
    },
    "inbound_secrets": {
        ("f", "FOREIGN KEY (inbound_id) REFERENCES inbounds(id) ON DELETE CASCADE"),
        ("p", "PRIMARY KEY (inbound_id)"),
    },
    "node_config_versions": {
        ("f", "FOREIGN KEY (node_id) REFERENCES nodes(id)"),
        ("p", "PRIMARY KEY (node_id, config_version)"),
    },
    "node_user_credentials": {
        ("f", "FOREIGN KEY (node_id) REFERENCES nodes(id)"),
        ("f", "FOREIGN KEY (user_id) REFERENCES users(id)"),
        ("p", "PRIMARY KEY (id)"),
        ("u", "UNIQUE (user_id, node_id)"),
    },
    "node_user_state": {
        ("f", "FOREIGN KEY (node_id) REFERENCES nodes(id)"),
        ("f", "FOREIGN KEY (user_id) REFERENCES users(id)"),
        ("p", "PRIMARY KEY (node_id, user_id)"),
    },
    "commands": {
        ("f", "FOREIGN KEY (node_id) REFERENCES nodes(id)"),
        ("p", "PRIMARY KEY (id)"),
    },
    "node_metrics": {
        ("p", "PRIMARY KEY (node_id, ts)"),
    },
    "node_country_availability": {
        ("f", "FOREIGN KEY (node_id) REFERENCES nodes(id)"),
        ("p", "PRIMARY KEY (node_id, country)"),
    },
}

EXPECTED_INDEXES: dict[str, set[str]] = {
    "nodes": {
        "CREATE INDEX nodes_billing_group_id_idx ON control_plane.nodes USING btree "
        "(billing_group_id)",
        "CREATE INDEX nodes_status_idx ON control_plane.nodes USING btree (status)",
        "CREATE UNIQUE INDEX nodes_code_key ON control_plane.nodes USING btree (code)",
        "CREATE UNIQUE INDEX nodes_pkey ON control_plane.nodes USING btree (id)",
    },
    "node_ip_history": {
        "CREATE INDEX node_ip_history_node_id_idx ON control_plane.node_ip_history USING btree "
        "(node_id)",
        "CREATE UNIQUE INDEX node_ip_history_pkey ON control_plane.node_ip_history USING btree "
        "(id)",
    },
    "node_access_groups": {
        "CREATE INDEX node_access_groups_access_group_id_idx ON control_plane.node_access_groups "
        "USING btree (access_group_id)",
        "CREATE UNIQUE INDEX node_access_groups_pkey ON control_plane.node_access_groups USING "
        "btree (node_id, access_group_id)",
    },
    "bootstrap_tokens": {
        "CREATE INDEX bootstrap_tokens_node_id_idx ON control_plane.bootstrap_tokens USING btree "
        "(node_id)",
        "CREATE UNIQUE INDEX bootstrap_tokens_pkey ON control_plane.bootstrap_tokens USING btree "
        "(id)",
        "CREATE UNIQUE INDEX bootstrap_tokens_token_hash_key ON control_plane.bootstrap_tokens "
        "USING btree (token_hash)",
    },
    "node_identities": {
        "CREATE INDEX node_identities_active_idx ON control_plane.node_identities USING btree "
        "(node_id) WHERE (revoked_at IS NULL)",
        "CREATE UNIQUE INDEX node_identities_cert_fingerprint_key ON "
        "control_plane.node_identities USING btree (cert_fingerprint)",
        "CREATE UNIQUE INDEX node_identities_pkey ON control_plane.node_identities USING btree "
        "(id)",
    },
    "inbounds": {
        "CREATE UNIQUE INDEX inbounds_node_id_port_key ON control_plane.inbounds USING btree "
        "(node_id, port)",
        "CREATE UNIQUE INDEX inbounds_node_id_profile_key ON control_plane.inbounds USING btree "
        "(node_id, profile)",
        "CREATE UNIQUE INDEX inbounds_pkey ON control_plane.inbounds USING btree (id)",
    },
    "inbound_secrets": {
        "CREATE UNIQUE INDEX inbound_secrets_pkey ON control_plane.inbound_secrets USING btree "
        "(inbound_id)",
    },
    "node_config_versions": {
        "CREATE UNIQUE INDEX node_config_versions_pkey ON control_plane.node_config_versions "
        "USING btree (node_id, config_version)",
    },
    "node_user_credentials": {
        "CREATE INDEX node_user_credentials_node_id_idx ON control_plane.node_user_credentials "
        "USING btree (node_id)",
        "CREATE UNIQUE INDEX node_user_credentials_pkey ON control_plane.node_user_credentials "
        "USING btree (id)",
        "CREATE UNIQUE INDEX node_user_credentials_user_id_node_id_key ON "
        "control_plane.node_user_credentials USING btree (user_id, node_id)",
    },
    "node_user_state": {
        "CREATE INDEX node_user_state_node_id_updated_seq_idx ON control_plane.node_user_state "
        "USING btree (node_id, updated_seq)",
        "CREATE UNIQUE INDEX node_user_state_pkey ON control_plane.node_user_state USING btree "
        "(node_id, user_id)",
    },
    "commands": {
        "CREATE INDEX commands_node_id_status_idx ON control_plane.commands USING btree "
        "(node_id, status)",
        "CREATE UNIQUE INDEX commands_pkey ON control_plane.commands USING btree (id)",
    },
    "node_metrics": {
        "CREATE UNIQUE INDEX node_metrics_pkey ON ONLY control_plane.node_metrics USING btree "
        "(node_id, ts)",
    },
    "node_country_availability": {
        "CREATE UNIQUE INDEX node_country_availability_pkey ON "
        "control_plane.node_country_availability USING btree (node_id, country)",
    },
}

SCHEMA_NODES_TABLES = list(EXPECTED_COLUMNS)
EXPECTED_SEQUENCE_GRANTS: dict[str, set[str]] = {"app_rw": {"USAGE", "SELECT"}, "app_backup": set()}


async def test_catalog_matches_data_model(pg_dsn: str) -> None:
    """Тринадцать таблиц группы, ключ партиционирования node_metrics и FK на nodes из 050."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        for table in SCHEMA_NODES_TABLES:
            await assert_table_matches(
                conn,
                table,
                EXPECTED_COLUMNS[table],
                EXPECTED_CONSTRAINTS[table],
                EXPECTED_INDEXES[table],
            )
        partition_key = await conn.fetchval(
            "select pg_get_partkeydef($1::regclass)", f"{SCHEMA}.node_metrics"
        )
        assert partition_key == "RANGE (ts)", "node_metrics партиционирована по суткам ts (§4.5)"
        deferred_fk = await conn.fetchval(
            "select pg_get_constraintdef(oid) from pg_constraint "
            "where conname = 'node_billing_assignments_node_id_fkey'"
        )
        assert deferred_fk == "FOREIGN KEY (node_id) REFERENCES nodes(id)", (
            "060 добавляет отложенный FK истории назначений (050)"
        )
        typed = await conn.fetch(
            "select column_name, numeric_precision, numeric_scale, character_maximum_length "
            "from information_schema.columns where table_schema = $1 and table_name = 'nodes' "
            "and column_name in ('monthly_cost', 'traffic_overage_cost', 'currency', 'country')",
            SCHEMA,
        )
        assert {tuple(r) for r in typed} == {
            ("monthly_cost", 12, 2, None),
            ("traffic_overage_cost", 12, 4, None),
            ("currency", None, None, 3),
            ("country", None, None, 2),
        }
    finally:
        await conn.close()


@pytest.mark.parametrize("table", SCHEMA_NODES_TABLES)
async def test_app_rw_privileges(pg_dsn: str, table: str) -> None:
    """TC-UNIT-01: app_rw — ровно DML, app_backup — только SELECT на каждой таблице группы."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        assert await table_grants(conn, table) == EXPECTED_TABLE_GRANTS
    finally:
        await conn.close()


async def test_sequence_privileges(pg_dsn: str) -> None:
    """node_ip_history_id_seq (identity): app_rw — USAGE и SELECT, app_backup — ничего."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        actual = {
            role: {
                priv
                for priv in ("USAGE", "SELECT", "UPDATE")
                if await conn.fetchval(
                    "select has_sequence_privilege($1, $2, $3)",
                    role,
                    f"{SCHEMA}.node_ip_history_id_seq",
                    priv,
                )
            }
            for role in EXPECTED_SEQUENCE_GRANTS
        }
        assert actual == EXPECTED_SEQUENCE_GRANTS
    finally:
        await conn.close()
