"""Сквозные проверки задачи 001.07 (миграция 070 «подписки, баланс, токены, коды»).

TC-E2E-01: применение, откат ровно до 070 и повторное применение; откат удаляет только объекты 070.
TC-E2E-02: ограничения §4.2.4/§4.4 под ролью app_rw — частичный UNIQUE активного токена (R-14),
запрет UPDATE/DELETE на balance_entries (§4.6), CHECK периодов, кодов и adjustment, FK и каскад
subscriptions; subscription_access_log без партиции и во временной партиции. Вставки — в
откатываемой транзакции, стенд остаётся без строк.
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
from ._db import existing_tables, insert_plan, insert_user
from ._spec import CATALOG_TABLES, IDENTITY_TABLES, SUBSCRIPTIONS_TABLES

PROBE_TS = dt.datetime(2000, 1, 1, 12, tzinfo=dt.UTC)  # вне окна планировщика партиций


async def test_migration_070_apply_rollback_reapply(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """TC-E2E-01: после apply таблицы группы есть; откат ровно до 070 снимает только их;
    повторное применение возвращает группу."""
    applied = run_cli(migrate_env, "migrate")
    assert applied.returncode == 0, applied.stderr
    assert SUBSCRIPTIONS_TABLES <= await existing_tables(pg_dsn)

    rollback_through(migrate_env, "070_schema_subscriptions")
    tables = await existing_tables(pg_dsn)
    assert not (SUBSCRIPTIONS_TABLES & tables), tables
    assert CATALOG_TABLES <= tables and IDENTITY_TABLES <= tables, "откат 070 не трогает 050/040"
    conn = await asyncpg.connect(pg_dsn)
    try:
        leftover = await conn.fetchval(
            "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'control_plane' and c.relkind = 'S' "
            "and c.relname in ('balance_entries_id_seq', 'subscription_access_log_id_seq')"
        )
        assert leftover == 0, "последовательности уходят вместе с таблицами"
    finally:
        await conn.close()

    reapplied = run_cli(migrate_env, "migrate")
    assert reapplied.returncode == 0, reapplied.stderr
    assert SUBSCRIPTIONS_TABLES <= await existing_tables(pg_dsn)


async def test_constraints_reject_bad_rows(pg_dsn: str, migrate_env: dict[str, str]) -> None:
    """TC-E2E-02: ограничения и права под app_rw."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    conn = await asyncpg.connect(pg_dsn)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            await check_subscription_constraints(conn)
        finally:
            await tx.rollback()
    finally:
        await conn.close()


async def check_subscription_constraints(conn: asyncpg.Connection) -> None:
    """Тело TC-E2E-02 внутри открытой транзакции."""

    async def rejected(exc: type[Exception], query: str, *args: object) -> None:
        with pytest.raises(exc):
            async with conn.transaction():
                await conn.execute(query, *args)

    user_id = await insert_user(conn, "sub-probe@example.com")
    plan_id = await insert_plan(conn, "Sub-Basic")
    period = (
        "insert into subscription_periods (user_id, plan_id, period_start, period_end, source) "
        "values ($1, $2, $3, $4, 'redeem') returning id"
    )
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    period_id = await conn.fetchval(period, user_id, plan_id, start, start + dt.timedelta(days=30))
    await rejected(asyncpg.CheckViolationError, period, user_id, plan_id, start, start)
    await rejected(
        asyncpg.ForeignKeyViolationError,
        period,
        uuid.uuid4(),
        plan_id,
        start,
        start + dt.timedelta(days=1),
    )
    await rejected(
        asyncpg.InvalidTextRepresentationError,
        "insert into subscription_periods (user_id, plan_id, period_start, period_end, source) "
        "values ($1, $2, $3, $4, $5::period_source)",
        user_id,
        plan_id,
        start,
        start + dt.timedelta(days=1),
        "gift",
    )

    await conn.execute(
        "insert into subscriptions (user_id, state, current_period_id) values ($1, 'active', $2)",
        user_id,
        period_id,
    )
    await rejected(  # PK user_id: одна строка состояния на пользователя
        asyncpg.UniqueViolationError,
        "insert into subscriptions (user_id) values ($1)",
        user_id,
    )
    await rejected(
        asyncpg.ForeignKeyViolationError,
        "update subscriptions set current_period_id = $1 where user_id = $2",
        uuid.uuid4(),
        user_id,
    )

    entry = (
        "insert into balance_entries (period_id, source, delta_billable_bytes, ref_key, reason) "
        "values ($1, $2, $3, $4, $5) returning id"
    )
    entry_id = await conn.fetchval(entry, period_id, "report", 1024, "n1:e1:1", None)
    assert isinstance(entry_id, int) and entry_id > 0, "identity под app_rw"
    await rejected(
        asyncpg.CheckViolationError, entry, period_id, "adjustment", -1024, "adj-1", None
    )
    await conn.execute(entry, period_id, "adjustment", -1024, "adj-1", "возврат по обращению")
    await rejected(asyncpg.ForeignKeyViolationError, entry, uuid.uuid4(), "bonus", 1, "c", None)
    # §4.6: журнал баланса — только вставка от приложения.
    await rejected(
        asyncpg.InsufficientPrivilegeError,
        "update balance_entries set delta_billable_bytes = 0 where id = $1",
        entry_id,
    )
    await rejected(
        asyncpg.InsufficientPrivilegeError, "delete from balance_entries where id = $1", entry_id
    )

    add_link_sql = (
        "insert into subscription_tokens (user_id, token_hash, revoked_at) values ($1, $2, $3)"
    )
    await conn.execute(add_link_sql, user_id, "t-1", None)
    await rejected(
        asyncpg.UniqueViolationError, add_link_sql, user_id, "t-2", None
    )  # второй активный
    await rejected(
        asyncpg.UniqueViolationError, add_link_sql, user_id, "t-1", dt.datetime.now(dt.UTC)
    )
    await conn.execute(
        add_link_sql, user_id, "t-old", dt.datetime.now(dt.UTC)
    )  # отозванный — можно
    await conn.execute("update subscription_tokens set revoked_at = now() where token_hash = 't-1'")
    await conn.execute(add_link_sql, user_id, "t-3", None)  # после отзыва — новый активный

    admin_id = await conn.fetchval(
        "insert into admin_users (email, password_hash, role) "
        "values ('codes-probe@example.com', 'x', 'admin') returning id"
    )
    code = (
        "insert into codes (kind, code_hash, code_enc, plan_id, max_uses, max_uses_per_user, "
        "uses_count, traffic_bonus_bytes, duration_bonus_days, created_by) "
        "values ('redeem', $1, $2, $3, $4, $5, $6, $7, $8, $9) returning id"
    )
    code_id = await conn.fetchval(code, "c-1", b"enc", plan_id, 10, 1, 0, 0, 0, admin_id)
    await rejected(
        asyncpg.UniqueViolationError, code, "c-1", b"enc", plan_id, 1, 1, 0, 0, 0, admin_id
    )
    for bad in (
        (0, 1, 0, 0, 0),
        (1, 0, 0, 0, 0),
        (1, 1, -1, 0, 0),
        (1, 1, 0, -1, 0),
        (1, 1, 0, 0, -1),
    ):
        await rejected(
            asyncpg.CheckViolationError, code, f"c-bad-{bad}", b"enc", plan_id, *bad, admin_id
        )
    await rejected(
        asyncpg.ForeignKeyViolationError, code, "c-2", b"enc", uuid.uuid4(), 1, 1, 0, 0, 0, admin_id
    )
    await rejected(
        asyncpg.ForeignKeyViolationError, code, "c-3", b"enc", None, 1, 1, 0, 0, 0, uuid.uuid4()
    )
    await rejected(
        asyncpg.InvalidTextRepresentationError,
        "insert into codes (kind, code_hash, code_enc, created_by) "
        "values ($1::code_kind, 'c-4', 'e', $2)",
        "gift",
        admin_id,
    )
    redemption = "insert into code_redemptions (code_id, user_id, period_id) values ($1, $2, $3)"
    await conn.execute(redemption, code_id, user_id, period_id)
    await rejected(asyncpg.ForeignKeyViolationError, redemption, uuid.uuid4(), user_id, period_id)
    await rejected(asyncpg.NotNullViolationError, redemption, code_id, user_id, None)

    order_id = await conn.fetchval(
        "insert into orders (user_id, plan_id, amount, currency, status) "
        "values ($1, $2, 9.99, 'EUR', 'new') returning id",
        user_id,
        plan_id,
    )
    await rejected(
        asyncpg.CheckViolationError,
        "insert into orders (user_id, plan_id, amount, currency, status) "
        "values ($1, $2, -1, 'EUR', 'new')",
        user_id,
        plan_id,
    )
    await conn.execute(
        "insert into payments (order_id, provider, provider_ref, amount, currency, status) "
        "values ($1, 'stub', 'ref-1', 9.99, 'EUR', 'pending')",
        order_id,
    )
    await rejected(
        asyncpg.ForeignKeyViolationError,
        "insert into payments (order_id, provider, provider_ref, amount, currency, status) "
        "values ($1, 'stub', 'ref-2', 9.99, 'EUR', 'pending')",
        uuid.uuid4(),
    )

    await rejected(  # партиций журнала обращений до 001.08 нет
        asyncpg.CheckViolationError,
        "insert into subscription_access_log (user_id, ts, domain, format, source_ip) "
        "values ($1, $2, 'sub1.example.com', 'v2rayng', '198.51.100.7')",
        user_id,
        PROBE_TS,
    )
    # Пользователь с периодами не удаляется (NO ACTION); пользователь только со строкой состояния
    # удаляется, и строка subscriptions уходит каскадом.
    await rejected(asyncpg.ForeignKeyViolationError, "delete from users where id = $1", user_id)
    other_user = await insert_user(conn, "sub-cascade@example.com")
    await conn.execute("insert into subscriptions (user_id) values ($1)", other_user)
    await conn.execute("delete from users where id = $1", other_user)
    left = await conn.fetchval("select count(*) from subscriptions where user_id = $1", other_user)
    assert left == 0, "subscriptions.user_id — ON DELETE CASCADE"


async def test_access_log_insert_as_app_rw(pg_dsn: str, migrate_env: dict[str, str]) -> None:
    """С партицией на дату app_rw пишет обращение к подписке: id из последовательности,
    PK (ts, id). Партицию создаёт и удаляет app_owner; дата 2000-01-01 — вне окна планировщика."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    dsn = migrate_dsn(migrate_env["MIGRATE_DSN"], migrate_env.get("MIGRATE_PASSWORD_FILE"))
    with psycopg.connect(
        dsn.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True
    ) as owner:
        owner.execute("SET ROLE app_owner")
        owner.execute("SET search_path TO control_plane")
        owner.execute("DROP TABLE IF EXISTS subscription_access_log_probe")
        day = PROBE_TS.replace(hour=0)
        owner.execute(
            sql.SQL(
                "CREATE TABLE subscription_access_log_probe PARTITION OF subscription_access_log "
                "FOR VALUES FROM ({start}) TO ({stop})"
            ).format(
                start=sql.Literal(day.isoformat()),
                stop=sql.Literal((day + dt.timedelta(days=1)).isoformat()),
            )
        )
        try:
            conn = await asyncpg.connect(pg_dsn)
            try:
                insert = (
                    "insert into subscription_access_log (user_id, ts, domain, format, source_ip, "
                    "user_agent, country) values ($1, $2, 'sub1.example.com', 'clash', "
                    "'198.51.100.7', $3, 'DE') returning id"
                )
                first = await conn.fetchval(insert, uuid.uuid4(), PROBE_TS, "v2rayNG/1.9")
                second = await conn.fetchval(insert, uuid.uuid4(), PROBE_TS, None)
                assert isinstance(first, int) and second == first + 1
                with pytest.raises(asyncpg.UniqueViolationError):  # PK (ts, id)
                    async with conn.transaction():
                        await conn.execute(
                            "insert into subscription_access_log (id, user_id, ts, domain, format, "
                            "source_ip) values ($1, $2, $3, 'd', 'f', '198.51.100.8')",
                            first,
                            uuid.uuid4(),
                            PROBE_TS,
                        )
            finally:
                await conn.close()
        finally:
            owner.execute("DROP TABLE subscription_access_log_probe")
