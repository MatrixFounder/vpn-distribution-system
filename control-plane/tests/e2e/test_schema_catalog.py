"""Сквозные проверки задачи 001.05 (миграция 050 «тарифы, группы, коэффициенты»).

TC-E2E-01: применение, откат ровно до 050 и повторное применение; откат удаляет только объекты
050. TC-E2E-02:
ограничения §4.2.2/§4.4 под ролью app_rw — UNIQUE, CHECK (duration_days, multiplier_milli
0…10000 кратно 100, порядок границ интервала), EXCLUDE USING gist на интервалах двух таблиц истории,
FK и каскады. Все вставки — в откатываемой транзакции, стенд остаётся без строк.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable

import asyncpg
import pytest

from ._cli import rollback_through, run_cli
from ._db import existing_tables
from ._spec import CATALOG_TABLES, IDENTITY_TABLES

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def day(n: int) -> dt.datetime:
    """Момент T0 + n суток — границы интервалов истории."""
    return T0 + dt.timedelta(days=n)


async def test_migration_050_apply_rollback_reapply(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """TC-E2E-01: после apply таблицы группы есть; откат ровно до 050 снимает только её объекты
    (таблицы 040 остаются); повторное применение возвращает группу."""
    applied = run_cli(migrate_env, "migrate")
    assert applied.returncode == 0, applied.stderr
    assert CATALOG_TABLES <= await existing_tables(pg_dsn)

    rollback_through(migrate_env, "050_schema_catalog")
    tables = await existing_tables(pg_dsn)
    assert not (CATALOG_TABLES & tables), tables
    assert IDENTITY_TABLES <= tables, "откат 050 не трогает 040 (050 от 040 не зависит)"

    reapplied = run_cli(migrate_env, "migrate")
    assert reapplied.returncode == 0, reapplied.stderr
    assert CATALOG_TABLES <= await existing_tables(pg_dsn)


async def test_constraints_reject_bad_rows(pg_dsn: str, migrate_env: dict[str, str]) -> None:
    """TC-E2E-02: UNIQUE, CHECK, EXCLUDE, FK и каскады отклоняют или уносят строки под app_rw."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    conn = await asyncpg.connect(pg_dsn)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            await check_catalog_constraints(conn)
        finally:
            await tx.rollback()
    finally:
        await conn.close()


async def check_catalog_constraints(conn: asyncpg.Connection) -> None:
    """Тело TC-E2E-02 внутри открытой транзакции."""

    async def rejected(exc: type[Exception], query: str, *args: object) -> None:
        with pytest.raises(exc):
            async with conn.transaction():  # точка сохранения, откатывается исключением
                await conn.execute(query, *args)

    plan_id = await conn.fetchval(
        "insert into plans (name, duration_days, traffic_limit_bytes) values ($1, 30, $2) "
        "returning id",
        "Basic",
        10 * 1024**3,
    )
    await rejected(
        asyncpg.UniqueViolationError,
        "insert into plans (name, duration_days) values ($1, 30)",
        "Basic",
    )
    await rejected(
        asyncpg.CheckViolationError,
        "insert into plans (name, duration_days) values ($1, 0)",  # duration_days > 0
        "Zero",
    )
    await rejected(
        asyncpg.InvalidTextRepresentationError,
        "insert into plans (name, duration_days, status) values ($1, 30, $2::plan_status)",
        "Odd",
        "paused",
    )
    unlimited = await conn.fetchval(
        "insert into plans (name, duration_days) values ($1, 365) returning traffic_limit_bytes",
        "Unlimited",
    )
    assert unlimited is None, "NULL = Unlimited (§4.2.2)"

    await conn.execute(
        "insert into plan_protocols (plan_id, profile) values ($1, 'vless_raw_vision')", plan_id
    )
    await rejected(
        asyncpg.UniqueViolationError,  # PK (plan_id, profile)
        "insert into plan_protocols (plan_id, profile) values ($1, 'vless_raw_vision')",
        plan_id,
    )
    await rejected(
        asyncpg.InvalidTextRepresentationError,
        "insert into plan_protocols (plan_id, profile) values ($1, $2::inbound_profile)",
        plan_id,
        "wireguard",
    )
    await rejected(
        asyncpg.ForeignKeyViolationError,
        "insert into plan_protocols (plan_id, profile) values ($1, 'vless_xhttp')",
        uuid.uuid4(),
    )

    group_id = await conn.fetchval(
        "insert into access_groups (name) values ($1) returning id", "eu-standard"
    )
    await rejected(
        asyncpg.UniqueViolationError,
        "insert into access_groups (name) values ($1)",
        "eu-standard",
    )
    await conn.execute(
        "insert into plan_access_groups (plan_id, access_group_id) values ($1, $2)",
        plan_id,
        group_id,
    )
    await rejected(
        asyncpg.ForeignKeyViolationError,
        "insert into plan_access_groups (plan_id, access_group_id) values ($1, $2)",
        plan_id,
        uuid.uuid4(),
    )

    billing_id = await conn.fetchval(
        "insert into billing_groups (name) values ($1) returning id", "tier-1"
    )
    await check_multiplier_history(conn, rejected, billing_id)
    await check_assignment_history(conn, rejected, billing_id)

    # Удаление тарифа уносит его профили и группы доступа (ON DELETE CASCADE).
    await conn.execute("delete from plans where id = $1", plan_id)
    left = await conn.fetchval(
        "select (select count(*) from plan_protocols where plan_id = $1) "
        "+ (select count(*) from plan_access_groups where plan_id = $1)",
        plan_id,
    )
    assert left == 0
    # Группу с историей коэффициентов удалить нельзя (FK без каскада — история бессрочна, §4.5).
    await rejected(
        asyncpg.ForeignKeyViolationError,
        "delete from billing_groups where id = $1",
        billing_id,
    )


Rejected = Callable[..., Awaitable[None]]


async def check_multiplier_history(
    conn: asyncpg.Connection, rejected: Rejected, billing_id: uuid.UUID
) -> None:
    """billing_group_multipliers: CHECK коэффициента, порядок границ, EXCLUDE интервалов."""
    insert = (
        "insert into billing_group_multipliers "
        "(billing_group_id, multiplier_milli, valid_from, valid_to) values ($1, $2, $3, $4)"
    )
    for bad in (-100, 50, 150, 10100):  # вне 0…10000 или не кратно 100
        await rejected(asyncpg.CheckViolationError, insert, billing_id, bad, day(0), day(10))
    await rejected(asyncpg.CheckViolationError, insert, billing_id, 1000, day(10), day(10))
    await rejected(asyncpg.CheckViolationError, insert, billing_id, 1000, day(10), day(5))
    await rejected(asyncpg.ForeignKeyViolationError, insert, uuid.uuid4(), 1000, day(0), day(10))

    await conn.execute(insert, billing_id, 0, day(0), day(10))  # граница 0.0
    await conn.execute(insert, billing_id, 10000, day(10), day(20))  # смежный, граница 10.0
    await rejected(asyncpg.ExclusionViolationError, insert, billing_id, 1000, day(5), day(15))
    await rejected(asyncpg.ExclusionViolationError, insert, billing_id, 1000, day(15), None)
    await conn.execute(insert, billing_id, 1500, day(20), None)  # действующий, открытый
    await rejected(asyncpg.ExclusionViolationError, insert, billing_id, 1000, day(100), None)
    other_id = await conn.fetchval(
        "insert into billing_groups (name) values ($1) returning id", "tier-2"
    )
    await conn.execute(insert, other_id, 1000, day(0), None)  # другая группа не мешает


async def check_assignment_history(
    conn: asyncpg.Connection, rejected: Rejected, billing_id: uuid.UUID
) -> None:
    """node_billing_assignments: EXCLUDE по ноде, CHECK переопределения, FK группы."""
    node_id = uuid.uuid4()  # до 060 FK на nodes нет — любой uuid
    insert = (
        "insert into node_billing_assignments "
        "(node_id, billing_group_id, multiplier_override_milli, valid_from, valid_to) "
        "values ($1, $2, $3, $4, $5)"
    )
    await conn.execute(insert, node_id, billing_id, None, day(0), day(10))
    await conn.execute(insert, node_id, billing_id, 200, day(10), None)
    await rejected(
        asyncpg.ExclusionViolationError, insert, node_id, billing_id, None, day(5), day(15)
    )
    await rejected(
        asyncpg.ExclusionViolationError, insert, node_id, billing_id, None, day(30), None
    )
    await rejected(asyncpg.CheckViolationError, insert, uuid.uuid4(), billing_id, 250, day(0), None)
    await rejected(
        asyncpg.CheckViolationError, insert, uuid.uuid4(), billing_id, 10100, day(0), None
    )
    await rejected(
        asyncpg.ForeignKeyViolationError, insert, uuid.uuid4(), uuid.uuid4(), None, day(0), None
    )
    await conn.execute(insert, uuid.uuid4(), billing_id, 10000, day(0), None)  # другая нода
