"""Сквозные проверки задачи 001.08 (миграция 080 «учёт трафика, гранты, адреса, функции
обслуживания партиций»).

TC-E2E-01: применение, откат ровно до 080 и повторное применение; откат удаляет только объекты 080
(включая функции, реестр и триггер). TC-E2E-02: ограничения §4.2.5/§4.4 и запреты §4.6 под app_rw —
PK отчётов (R-21), CHECK коэффициента и байтов, traffic_lines без UPDATE/DELETE, traffic_hourly без
DELETE и с триггером «только рост», реестр только чтение. Функции обслуживания: ensure_partitions
создаёт суточные партиции всех таблиц реестра с нужными правами, идемпотентна, ограничивает
days_ahead; drop_expired_partitions удаляет просроченные ровно по границе срока хранения; обе
функции считают сутки в UTC независимо от timezone сессии вызывающего. Партиции, созданные
тестом, удаляются.
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
from ._db import existing_tables, insert_node, insert_plan, insert_user
from ._spec import ACCOUNTING_TABLES, NODES_TABLES, SUBSCRIPTIONS_TABLES

PROBE_DAY = dt.date(2000, 1, 1)  # вне окна планировщика
PARTITIONED = [
    "auth_events",
    "subscription_access_log",
    "node_metrics",
    "traffic_lines",
    "traffic_hourly",
]


def owner_connection(migrate_env: dict[str, str]) -> psycopg.Connection:
    """Подключение app_migrate с SET ROLE app_owner для операций владельца."""
    dsn = migrate_dsn(migrate_env["MIGRATE_DSN"], migrate_env.get("MIGRATE_PASSWORD_FILE"))
    conn = psycopg.connect(
        dsn.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True
    )
    conn.execute("SET ROLE app_owner")
    conn.execute("SET search_path TO control_plane")
    return conn


async def partitions(conn: asyncpg.Connection) -> dict[str, str]:
    """Партиции схемы control_plane: имя → ACL (текст) — только таблицы, без индексов."""
    rows = await conn.fetch(
        "select c.relname, c.relacl::text as acl from pg_inherits i "
        "join pg_class c on c.oid = i.inhrelid join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'control_plane' and c.relkind = 'r' order by 1"
    )
    return {r["relname"]: r["acl"] for r in rows}


async def utc_today(conn: asyncpg.Connection) -> dt.date:
    """Текущие сутки базы в UTC — сутки, которыми оперируют функции обслуживания."""
    day: dt.date = await conn.fetchval("select (now() at time zone 'UTC')::date")
    return day


def create_partition(owner: psycopg.Connection, table: str, day: dt.date) -> str:
    """Создать суточную партицию таблицы под владельцем; вернуть её имя."""
    name = f"{table}_p{day:%Y%m%d}"
    owner.execute(
        sql.SQL("CREATE TABLE {} PARTITION OF {} FOR VALUES FROM ({}) TO ({})").format(
            sql.Identifier(name),
            sql.Identifier(table),
            sql.Literal(day.isoformat()),
            sql.Literal((day + dt.timedelta(days=1)).isoformat()),
        )
    )
    return name


async def assert_retention_boundary(
    conn: asyncpg.Connection, migrate_env: dict[str, str], today: dt.date
) -> None:
    """Граница срока хранения traffic_lines (14 суток, §4.5) на сутках today (UTC): партиция
    суток cutoff-1 уходит, суток cutoff остаётся — ровно retention_days последних полных суток;
    древняя партиция (2000-01-01) уходит вместе с первой; повторный вызов ничего не удаляет."""
    cutoff = today - dt.timedelta(days=14)
    with owner_connection(migrate_env) as owner:
        ancient = create_partition(owner, "traffic_lines", PROBE_DAY)
        expired = create_partition(owner, "traffic_lines", cutoff - dt.timedelta(days=1))
        kept = create_partition(owner, "traffic_lines", cutoff)
    assert await conn.fetchval("select drop_expired_partitions()") == 2
    remaining = await partitions(conn)
    assert ancient not in remaining
    assert expired not in remaining, "сутки cutoff-1 просрочены"
    assert kept in remaining, "сутки cutoff — последние из retention_days полных суток — остаются"
    assert await conn.fetchval("select drop_expired_partitions()") == 0


async def drop_all_partitions(migrate_env: dict[str, str], pg_dsn: str) -> None:
    """Убрать все партиции таблиц реестра (владелец), чтобы стенд остался как до теста."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        names = list(await partitions(conn))
    finally:
        await conn.close()
    with owner_connection(migrate_env) as owner:
        for name in names:
            owner.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier("control_plane", name)))


async def test_migration_080_apply_rollback_reapply(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """TC-E2E-01: после apply таблицы, функции и триггер есть; откат ровно до 080 снимает только
    их (070/060 остаются); партиции таблиц других миграций, созданные ensure_partitions, откат не
    трогает; повторное применение возвращает группу."""
    applied = run_cli(migrate_env, "migrate")
    assert applied.returncode == 0, applied.stderr
    assert ACCOUNTING_TABLES <= await existing_tables(pg_dsn)
    try:
        await check_rollback_keeps_foreign_partitions(pg_dsn, migrate_env)
    finally:
        # Что бы ни упало посередине: группа снова применена и партиций на стенде нет.
        run_cli(migrate_env, "migrate")
        await drop_all_partitions(migrate_env, pg_dsn)


async def check_rollback_keeps_foreign_partitions(pg_dsn: str, migrate_env: dict[str, str]) -> None:
    """Тело TC-E2E-01 после применения: партиции текущих суток и строка в auth_events → откат
    ровно до 080 → объекты 080 сняты, чужие партиции и строка на месте → повторное применение."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        assert await conn.fetchval("select ensure_partitions(0)") == len(PARTITIONED)
        today = await utc_today(conn)
        foreign_names = {
            f"{table}_p{today:%Y%m%d}"
            for table in ("auth_events", "subscription_access_log", "node_metrics")
        }
        await conn.execute(
            "insert into auth_events (user_id, kind, ts, result) "
            "values ($1, 'login', $2, 'denied')",
            uuid.uuid4(),
            dt.datetime.combine(today, dt.time(12), tzinfo=dt.UTC),
        )
    finally:
        await conn.close()
    rollback_through(migrate_env, "080_schema_accounting")
    tables = await existing_tables(pg_dsn)
    assert not (ACCOUNTING_TABLES & tables), tables
    assert SUBSCRIPTIONS_TABLES <= tables and NODES_TABLES <= tables, "откат 080 не трогает 070/060"
    conn = await asyncpg.connect(pg_dsn)
    try:
        functions = await conn.fetchval(
            "select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
            "where n.nspname = 'control_plane' and p.proname in "
            "('ensure_partitions', 'drop_expired_partitions', 'traffic_hourly_only_grows')"
        )
        assert functions == 0, "функции обслуживания и триггерная функция удалены откатом"
        assert set(await partitions(conn)) == foreign_names, (
            "откат оставляет партиции чужих таблиц присоединёнными"
        )
        assert await conn.fetchval("select count(*) from auth_events") == 1, (
            "откат не трогает данные чужих таблиц"
        )
    finally:
        await conn.close()

    reapplied = run_cli(migrate_env, "migrate")
    assert reapplied.returncode == 0, reapplied.stderr
    assert ACCOUNTING_TABLES <= await existing_tables(pg_dsn)


async def test_constraints_and_privileges(pg_dsn: str, migrate_env: dict[str, str]) -> None:
    """TC-E2E-02: ограничения и запреты §4.6 под app_rw (в откатываемой транзакции)."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    conn = await asyncpg.connect(pg_dsn)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            await check_accounting_constraints(conn)
        finally:
            await tx.rollback()
    finally:
        await conn.close()


async def check_accounting_constraints(conn: asyncpg.Connection) -> None:
    """Тело TC-E2E-02 внутри открытой транзакции."""

    async def rejected(exc: type[Exception], query: str, *args: object) -> None:
        with pytest.raises(exc):
            async with conn.transaction():
                await conn.execute(query, *args)

    billing_id = await conn.fetchval(
        "insert into billing_groups (name) values ('acct-probe') returning id"
    )
    node_id = await insert_node(conn, "FI-Helsinki-01", billing_id)
    epoch = uuid.uuid4()
    t0 = dt.datetime(2026, 2, 1, tzinfo=dt.UTC)
    report = (
        "insert into traffic_reports (node_id, counter_epoch, report_seq, period_start, "
        "period_end, status, node_rx_bytes, node_tx_bytes) "
        "values ($1, $2, $3, $4, $5, 'accepted', $6, $7)"
    )
    await conn.execute(report, node_id, epoch, 1, t0, t0 + dt.timedelta(minutes=1), 10, 20)
    await rejected(
        asyncpg.UniqueViolationError,
        report,
        node_id,
        epoch,
        1,
        t0,
        t0 + dt.timedelta(minutes=1),
        1,
        1,
    )  # PK — идемпотентность отчётов (R-21)
    await rejected(asyncpg.CheckViolationError, report, node_id, epoch, 2, t0, t0, 1, 1)
    await rejected(
        asyncpg.CheckViolationError,
        report,
        node_id,
        epoch,
        3,
        t0,
        t0 + dt.timedelta(minutes=1),
        -1,
        1,
    )
    await rejected(
        asyncpg.ForeignKeyViolationError,
        report,
        uuid.uuid4(),
        epoch,
        4,
        t0,
        t0 + dt.timedelta(minutes=1),
        1,
        1,
    )

    # Таблицы без партиций на дату: строки отклоняются; запреты §4.6 действуют и без строк.
    line = (
        "insert into traffic_lines (node_id, counter_epoch, report_seq, user_id, period_start, "
        "period_end, raw_uplink_bytes, raw_downlink_bytes, billable_bytes, multiplier_milli, "
        "billing_group_id) values ($1, $2, 1, $3, $4, $5, 1, 1, 1, 1000, $6)"
    )
    probe_ts = dt.datetime.combine(PROBE_DAY, dt.time(12), tzinfo=dt.UTC)
    await rejected(
        asyncpg.CheckViolationError,
        line,
        node_id,
        epoch,
        uuid.uuid4(),
        probe_ts,
        probe_ts + dt.timedelta(minutes=1),
        billing_id,
    )
    await rejected(
        asyncpg.InsufficientPrivilegeError,
        "update traffic_lines set billable_bytes = 0 where false",
    )
    await rejected(asyncpg.InsufficientPrivilegeError, "delete from traffic_lines where false")
    await rejected(asyncpg.InsufficientPrivilegeError, "delete from traffic_hourly where false")
    await rejected(
        asyncpg.InsufficientPrivilegeError,
        "update partition_policies set retention_days = 1 where false",
    )
    await rejected(
        asyncpg.InsufficientPrivilegeError,
        "insert into partition_policies (table_name, retention_days) values ('x', 1)",
    )

    daily = (
        "insert into traffic_daily (user_id, node_id, day, raw_uplink_bytes, raw_downlink_bytes, "
        "billable_bytes) values ($1, $2, $3, $4, 1, 1)"
    )
    user_id = uuid.uuid4()
    await conn.execute(daily, user_id, node_id, PROBE_DAY, 1)
    await rejected(asyncpg.UniqueViolationError, daily, user_id, node_id, PROBE_DAY, 1)
    await rejected(
        asyncpg.CheckViolationError, daily, user_id, node_id, PROBE_DAY + dt.timedelta(days=1), -5
    )

    await conn.execute(
        "insert into node_interface_hourly (node_id, hour_start, rx_bytes, tx_bytes) "
        "values ($1, $2, 1, 1)",
        node_id,
        t0,
    )
    await rejected(
        asyncpg.UniqueViolationError,
        "insert into node_interface_hourly (node_id, hour_start, rx_bytes, tx_bytes) "
        "values ($1, $2, 1, 1)",
        node_id,
        t0,
    )
    await rejected(
        asyncpg.CheckViolationError,
        "insert into traffic_gaps (node_id, gap_start, gap_end, reason) values ($1, $2, $2, 'x')",
        node_id,
        t0,
    )
    await rejected(
        asyncpg.ForeignKeyViolationError,
        "insert into traffic_gaps (node_id, gap_start, gap_end, reason) values ($1, $2, $3, 'x')",
        uuid.uuid4(),
        t0,
        t0 + dt.timedelta(hours=1),
    )
    await rejected(
        asyncpg.InvalidTextRepresentationError,
        "insert into reconciliation_runs (kind, expected, actual, delta_pct, status) "
        "values ($1::reconciliation_kind, 1, 1, 0, 'ok')",
        "guess",
    )

    real_user = await insert_user(conn, "acct-probe@example.com")
    plan_id = await insert_plan(conn, "Acct-Basic")
    period_id = await conn.fetchval(
        "insert into subscription_periods (user_id, plan_id, period_start, period_end, source) "
        "values ($1, $2, $3, $4, 'admin') returning id",
        real_user,
        plan_id,
        t0,
        t0 + dt.timedelta(days=30),
    )
    grant = (
        "insert into quota_grants (user_id, node_id, period_id, grant_bytes, issued_seq) "
        "values ($1, $2, $3, $4, 1)"
    )
    await conn.execute(grant, real_user, node_id, period_id, 1024)
    await rejected(asyncpg.CheckViolationError, grant, real_user, node_id, period_id, -1)
    await rejected(asyncpg.ForeignKeyViolationError, grant, real_user, node_id, uuid.uuid4(), 1)
    await conn.execute(
        "insert into user_online_ips (user_id, node_id, ip) values ($1, $2, '198.51.100.1')",
        real_user,
        node_id,
    )
    await rejected(
        asyncpg.UniqueViolationError,
        "insert into user_online_ips (user_id, node_id, ip) values ($1, $2, '198.51.100.1')",
        real_user,
        node_id,
    )
    await conn.execute(
        "insert into user_blocked_ips (user_id, ip) values ($1, '198.51.100.2')", real_user
    )
    await rejected(
        asyncpg.UniqueViolationError,
        "insert into user_blocked_ips (user_id, ip) values ($1, '198.51.100.2')",
        real_user,
    )


async def test_ensure_and_drop_partitions(pg_dsn: str, migrate_env: dict[str, str]) -> None:
    """ensure_partitions(1) под app_rw создаёт по две суточные партиции на каждую из пяти таблиц
    реестра (владелец app_owner, запреты §4.6 на партициях traffic_*), повтор ничего не создаёт,
    days_ahead вне 0…366 отклоняется; drop_expired_partitions удаляет просроченные; на партиции
    traffic_hourly действует триггер «только рост». Созданные партиции удаляются в конце."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    conn = await asyncpg.connect(pg_dsn)
    try:
        assert await partitions(conn) == {}, "стенд без партиций до теста"
        created = await conn.fetchval("select ensure_partitions(1)")
        assert created == 2 * len(PARTITIONED)
        assert await conn.fetchval("select ensure_partitions(1)") == 0, "идемпотентность"
        with pytest.raises(asyncpg.RaiseError):
            await conn.fetchval("select ensure_partitions(400)")
        today = await utc_today(conn)  # сутки базы в UTC, не машины и не сессии
        parts = await partitions(conn)
        expected_names = {
            f"{table}_p{day:%Y%m%d}"
            for table in PARTITIONED
            for day in (today, today + dt.timedelta(days=1))
        }
        assert set(parts) == expected_names, parts
        owners = await conn.fetch(
            "select distinct pg_get_userbyid(c.relowner) as owner from pg_inherits i "
            "join pg_class c on c.oid = i.inhrelid where c.relkind = 'r'"
        )
        assert {r["owner"] for r in owners} == {"app_owner"}
        for name, acl in parts.items():
            if name.startswith("traffic_lines_p"):
                assert "app_rw=ar/" in acl, f"{name}: без UPDATE/DELETE у app_rw"
            elif name.startswith("traffic_hourly_p"):
                assert "app_rw=arw/" in acl, f"{name}: без DELETE у app_rw"
            else:
                assert "app_rw=arwd/" in acl, name

        # Триггер монотонности на партиции traffic_hourly под app_rw.
        node_id = uuid.uuid4()
        user_id = uuid.uuid4()
        hour = dt.datetime.combine(today, dt.time(10), tzinfo=dt.UTC)
        await conn.execute(
            "insert into traffic_hourly (user_id, node_id, hour_start, raw_uplink_bytes, "
            "raw_downlink_bytes, billable_bytes, multiplier_milli, billing_group_id) "
            "values ($1, $2, $3, 100, 100, 100, 1000, $4)",
            user_id,
            node_id,
            hour,
            uuid.uuid4(),
        )
        await conn.execute(
            "update traffic_hourly set billable_bytes = 150 where user_id = $1", user_id
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "update traffic_hourly set billable_bytes = 50 where user_id = $1", user_id
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("delete from traffic_hourly where user_id = $1", user_id)

        await assert_retention_boundary(conn, migrate_env, today)
        with pytest.raises(asyncpg.RaiseError):  # NULL — тоже вне диапазона
            await conn.fetchval("select ensure_partitions(NULL::int)")
    finally:
        await conn.close()
        await drop_all_partitions(migrate_env, pg_dsn)
    conn = await asyncpg.connect(pg_dsn)
    try:
        assert await partitions(conn) == {}, "после теста партиций нет"
    finally:
        await conn.close()


async def test_maintenance_uses_utc_regardless_of_session_timezone(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Сутки функций обслуживания — UTC (R-52), а не timezone сессии вызывающего: под UTC-12 и
    UTC+14 (в любой момент суток хотя бы в одной из зон локальная дата отличается от UTC)
    ensure_partitions(0) создаёт партиции суток UTC, а граница срока хранения не сдвигается."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    conn = await asyncpg.connect(pg_dsn)
    try:
        assert await partitions(conn) == {}, "стенд без партиций до теста"
        today = await utc_today(conn)
        for zone in ("Etc/GMT+12", "Etc/GMT-14"):  # POSIX-знак: GMT+12 — это UTC-12
            await conn.execute(f"SET timezone = '{zone}'")
            assert await conn.fetchval("select ensure_partitions(0)") == len(PARTITIONED)
            names = set(await partitions(conn))
            assert names == {f"{table}_p{today:%Y%m%d}" for table in PARTITIONED}, (zone, names)
            await assert_retention_boundary(conn, migrate_env, today)
            await drop_all_partitions(migrate_env, pg_dsn)
    finally:
        await conn.close()
        await drop_all_partitions(migrate_env, pg_dsn)
