"""Сквозные проверки задачи 001.06 (миграция 060 «парк нод, inbound, состояние и команды»).

TC-E2E-01: применение, откат ровно до 060 и повторное применение; откат снимает только объекты 060
и внешний ключ истории назначений, таблицы 050/040 остаются. TC-E2E-02: ограничения §4.2.3/§4.4
под ролью app_rw — UNIQUE (code, cert_fingerprint, node_id+profile, node_id+port,
user_id+node_id), CHECK (порт, пороги, коэффициент), NOT NULL billing_group_id, FK и каскады,
node_metrics без партиции. Все вставки — в откатываемой транзакции.
"""

from __future__ import annotations

import datetime as dt
import uuid

import asyncpg
import psycopg
import pytest
from app.cli import migrate_dsn
from psycopg import sql

from ._cli import rollback_through, run_cli
from ._db import existing_tables, insert_node
from ._spec import CATALOG_TABLES, IDENTITY_TABLES, NODES_TABLES

PROBE_TS = dt.datetime(2000, 1, 1, 12, tzinfo=dt.UTC)  # вне окна планировщика партиций


async def test_migration_060_apply_rollback_reapply(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """TC-E2E-01: после apply таблицы группы есть; откат ровно до 060 снимает их и FK на nodes
    (050 и 040 остаются); повторное применение возвращает группу."""
    applied = run_cli(migrate_env, "migrate")
    assert applied.returncode == 0, applied.stderr
    assert NODES_TABLES <= await existing_tables(pg_dsn)

    rollback_through(migrate_env, "060_schema_nodes")
    tables = await existing_tables(pg_dsn)
    assert not (NODES_TABLES & tables), tables
    assert CATALOG_TABLES <= tables and IDENTITY_TABLES <= tables, "откат 060 не трогает 050 и 040"
    conn = await asyncpg.connect(pg_dsn)
    try:
        fk = await conn.fetchval(
            "select count(*) from pg_constraint "
            "where conname = 'node_billing_assignments_node_id_fkey'"
        )
        assert fk == 0, "откат снимает отложенный FK истории назначений"
    finally:
        await conn.close()

    reapplied = run_cli(migrate_env, "migrate")
    assert reapplied.returncode == 0, reapplied.stderr
    assert NODES_TABLES <= await existing_tables(pg_dsn)


async def test_constraints_reject_bad_rows(pg_dsn: str, migrate_env: dict[str, str]) -> None:
    """TC-E2E-02: UNIQUE, CHECK, NOT NULL, FK и каскады под app_rw."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    conn = await asyncpg.connect(pg_dsn)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            await check_node_constraints(conn)
        finally:
            await tx.rollback()
    finally:
        await conn.close()


async def check_node_constraints(conn: asyncpg.Connection) -> None:
    """Тело TC-E2E-02 внутри открытой транзакции."""

    async def rejected(exc: type[Exception], query: str, *args: object) -> None:
        with pytest.raises(exc):
            async with conn.transaction():
                await conn.execute(query, *args)

    billing_id = await conn.fetchval(
        "insert into billing_groups (name) values ($1) returning id", "tier-probe"
    )
    node_id = await insert_node(conn, "DE-Berlin-01", billing_id)
    await rejected(
        asyncpg.UniqueViolationError,
        "insert into nodes (code, name, country, city, "
        "provider, public_ipv4, billing_group_id, bandwidth_mbps, max_conn_per_ip) "
        "values ('DE-Berlin-01', 'dup', 'DE', 'Berlin', 'p', '203.0.113.9', $1, 100, 4)",
        billing_id,
    )
    await rejected(
        asyncpg.NotNullViolationError,
        "insert into nodes (code, name, country, city, "
        "provider, public_ipv4, bandwidth_mbps, max_conn_per_ip) values ('NL-01', 'n', "
        "'NL', 'Ams', 'p', '203.0.113.10', 100, 4)",
    )  # billing_group_id NOT NULL (R-18)
    await rejected(
        asyncpg.ForeignKeyViolationError,
        "insert into nodes (code, name, country, "
        "city, provider, public_ipv4, billing_group_id, bandwidth_mbps, max_conn_per_ip) "
        "values ('NL-02', 'n', 'NL', 'Ams', 'p', '203.0.113.11', $1, 100, 4)",
        uuid.uuid4(),
    )
    threshold_updates = {  # имена колонок фиксированы тестом, значения нарушают CHECK
        "bandwidth_mbps": "update nodes set bandwidth_mbps = $1 where id = $2",
        "max_conn_per_ip": "update nodes set max_conn_per_ip = $1 where id = $2",
        "multiplier_milli": "update nodes set multiplier_milli = $1 where id = $2",
    }
    for column, value in (("bandwidth_mbps", 0), ("max_conn_per_ip", 0), ("multiplier_milli", 150)):
        await rejected(asyncpg.CheckViolationError, threshold_updates[column], value, node_id)
    await conn.execute("update nodes set multiplier_milli = 500 where id = $1", node_id)
    await rejected(
        asyncpg.InvalidTextRepresentationError,
        "update nodes set status = $1::node_status where id = $2",
        "sleeping",
        node_id,
    )

    inbound = (
        "insert into inbounds (node_id, profile, port, tag, reality_public_key, reality_target, "
        "reality_min_client_ver, client_fingerprint) values ($1, $2, $3, $4, 'pk', "
        "'www.example.com', "
        "'1.8.0', 'chrome') returning id"
    )
    inbound_id = await conn.fetchval(inbound, node_id, "vless_raw_vision", 443, "vision")
    await rejected(asyncpg.UniqueViolationError, inbound, node_id, "vless_raw_vision", 8443, "dup")
    await rejected(asyncpg.UniqueViolationError, inbound, node_id, "vless_xhttp", 443, "dup-port")
    await rejected(asyncpg.CheckViolationError, inbound, node_id, "vless_xhttp", 70000, "port")
    await rejected(asyncpg.CheckViolationError, inbound, node_id, "trojan_reality", 0, "port0")
    await conn.execute(
        "insert into inbound_secrets (inbound_id, private_key_enc) values ($1, $2)",
        inbound_id,
        b"enc",
    )
    await rejected(
        asyncpg.ForeignKeyViolationError,
        "insert into inbound_secrets (inbound_id, private_key_enc) values ($1, $2)",
        uuid.uuid4(),
        b"enc",
    )
    # Секрет живёт только вместе с inbound (ON DELETE CASCADE); нода с inbound не удаляется.
    await rejected(asyncpg.ForeignKeyViolationError, "delete from nodes where id = $1", node_id)
    await conn.execute("delete from inbounds where id = $1", inbound_id)
    assert (
        await conn.fetchval(
            "select count(*) from inbound_secrets where inbound_id = $1", inbound_id
        )
        == 0
    )

    identity = (
        "insert into node_identities (node_id, cert_fingerprint, token_hash, generation, "
        "expires_at) values ($1, $2, 'tok', 1, now() + interval '90 days')"
    )
    await conn.execute(identity, node_id, "fp-1")
    await rejected(asyncpg.UniqueViolationError, identity, node_id, "fp-1")
    await rejected(asyncpg.ForeignKeyViolationError, identity, uuid.uuid4(), "fp-2")

    user_id = await conn.fetchval(
        "insert into users (email, password_hash, aup_version, aup_accepted_at) "
        "values ('node-probe@example.com', 'x', '2026-09', now()) returning id"
    )
    credential = (
        "insert into node_user_credentials (user_id, node_id, xray_email, vless_uuid_enc, "
        "trojan_password_enc) values ($1, $2, $3, $4, $4)"
    )
    await conn.execute(credential, user_id, node_id, f"u{user_id}", b"enc")
    await rejected(asyncpg.UniqueViolationError, credential, user_id, node_id, "dup", b"enc")
    await rejected(asyncpg.ForeignKeyViolationError, credential, uuid.uuid4(), node_id, "x", b"e")
    state = (
        "insert into node_user_state (node_id, user_id, state, updated_seq) values ($1, $2, $3, 1)"
    )
    await conn.execute(state, node_id, user_id, "active")
    await rejected(asyncpg.UniqueViolationError, state, node_id, user_id, "expired")  # PK
    await rejected(
        asyncpg.InvalidTextRepresentationError,
        "insert into node_user_state (node_id, user_id, state, updated_seq) values ($1, $2, "
        "$3::user_node_state, 2)",
        node_id,
        uuid.uuid4(),
        "paused",
    )

    group_id = await conn.fetchval(
        "insert into access_groups (name) values ('probe-group') returning id"
    )
    await conn.execute(
        "insert into node_access_groups (node_id, access_group_id) values ($1, $2)",
        node_id,
        group_id,
    )
    await conn.execute("delete from access_groups where id = $1", group_id)
    assert (
        await conn.fetchval("select count(*) from node_access_groups where node_id = $1", node_id)
        == 0
    )

    command = (
        "insert into commands (node_id, type, expires_at) "
        "values ($1, $2, now() + interval '1 hour')"
    )
    await conn.execute(command, node_id, "restart_xray")
    await rejected(
        asyncpg.InvalidTextRepresentationError,
        command.replace("$2", "$2::command_type"),
        node_id,
        "reboot",
    )
    await rejected(asyncpg.ForeignKeyViolationError, command, uuid.uuid4(), "restart_xray")

    await conn.execute(
        "insert into node_country_availability (node_id, country, available) values ($1, 'RU', "
        "true)",
        node_id,
    )
    await rejected(
        asyncpg.UniqueViolationError,
        "insert into node_country_availability (node_id, country, available) values ($1, 'RU', "
        "false)",
        node_id,
    )
    await conn.execute(
        "insert into node_config_versions (node_id, config_version, config_json, checksum) "
        "values ($1, 1, '{}', 'sha')",
        node_id,
    )
    await rejected(
        asyncpg.UniqueViolationError,
        "insert into node_config_versions (node_id, config_version, config_json, checksum) "
        "values ($1, 1, '{}', 'sha2')",
        node_id,
    )
    history_id = await conn.fetchval(
        "insert into node_ip_history (node_id, public_ipv4, valid_from) "
        "values ($1, '203.0.113.20', now()) returning id",
        node_id,
    )
    assert isinstance(history_id, int) and history_id > 0, "identity-столбец под app_rw"
    await rejected(  # valid_to раньше valid_from — CHECK, как у историй 050
        asyncpg.CheckViolationError,
        "insert into node_ip_history (node_id, public_ipv4, valid_from, valid_to) "
        "values ($1, '203.0.113.21', now(), now() - interval '1 day')",
        node_id,
    )
    await rejected(  # партиций node_metrics до планировщика 001.08 нет
        asyncpg.CheckViolationError,
        "insert into node_metrics (node_id, ts, cpu_pct, mem_pct, disk_pct, net_rx_bytes, "
        "net_tx_bytes, online_ips, connections) values ($1, $2, 1, 1, 1, 0, 0, 0, 0)",
        node_id,
        PROBE_TS,
    )


async def test_node_metrics_insert_as_app_rw(pg_dsn: str, migrate_env: dict[str, str]) -> None:
    """С партицией на дату app_rw вставляет метрики; PK (node_id, ts) отклоняет дубль. Партицию
    создаёт и удаляет app_owner (через MIGRATE_DSN); дата — 2000-01-01, вне окна планировщика."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    dsn = migrate_dsn(migrate_env["MIGRATE_DSN"], migrate_env.get("MIGRATE_PASSWORD_FILE"))
    with psycopg.connect(
        dsn.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True
    ) as owner:
        owner.execute("SET ROLE app_owner")
        owner.execute("SET search_path TO control_plane")
        owner.execute("DROP TABLE IF EXISTS node_metrics_probe")
        day = PROBE_TS.replace(hour=0)
        owner.execute(
            sql.SQL(
                "CREATE TABLE node_metrics_probe PARTITION OF node_metrics "
                "FOR VALUES FROM ({start}) TO ({stop})"
            ).format(
                start=sql.Literal(day.isoformat()),
                stop=sql.Literal((day + dt.timedelta(days=1)).isoformat()),
            )
        )
        try:
            conn = await asyncpg.connect(pg_dsn)
            try:
                tx = conn.transaction()
                await tx.start()
                try:
                    billing_id = await conn.fetchval(
                        "insert into billing_groups (name) values ('metrics-probe') returning id"
                    )
                    node_id = await insert_node(conn, "SG-01", billing_id)
                    insert = (
                        "insert into node_metrics (node_id, ts, cpu_pct, mem_pct, disk_pct, "
                        "net_rx_bytes, net_tx_bytes, online_ips, connections) "
                        "values ($1, $2, 12.5, 40.0, 55.5, 1024, 2048, 3, 7)"
                    )
                    await conn.execute(insert, node_id, PROBE_TS)
                    with pytest.raises(asyncpg.UniqueViolationError):  # PK (node_id, ts)
                        async with conn.transaction():
                            await conn.execute(insert, node_id, PROBE_TS)
                    stored = await conn.fetchval(
                        "select count(*) from node_metrics where node_id = $1", node_id
                    )
                    assert stored == 1
                finally:
                    await tx.rollback()
            finally:
                await conn.close()
        finally:
            owner.execute("DROP TABLE node_metrics_probe")
