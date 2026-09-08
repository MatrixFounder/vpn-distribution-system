"""Интроспекция схемы по системным каталогам PostgreSQL — общий код стражей групп схемы.

Спецификация группы — колонки (имя, тип ``udt_name``, допускает NULL, умолчание) в порядке
объявления, ограничения ``pg_constraint`` (тип, ``pg_get_constraintdef``) и индексы
(``pg_indexes.indexdef``). Права ролей — по ``has_table_privilege``: ``information_schema`` из
сессии ``app_rw`` чужих прав не показывает.
"""

from __future__ import annotations

import asyncpg

Column = tuple[str, str, bool, str | None]

SCHEMA = "control_plane"  # все объекты Control Plane — в своей схеме, не в public

TABLE_PRIVILEGES = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]
EXPECTED_TABLE_GRANTS: dict[str, set[str]] = {
    "app_rw": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "app_backup": {"SELECT"},
}
SCHEMA_PRIVILEGES = ["USAGE", "CREATE"]
# Несущий уровень после переезда из public: без USAGE роль не видит ни одной таблицы, с CREATE
# роль приложения могла бы создавать объекты (нарушение «только DML», §4.6).
EXPECTED_SCHEMA_GRANTS: dict[str, set[str]] = {
    "app_rw": {"USAGE"},
    "app_backup": {"USAGE"},
    "app_migrate": {"USAGE", "CREATE"},
}


async def fetch_columns(conn: asyncpg.Connection, table: str) -> list[Column]:
    """Колонки таблицы схемы приложения в порядке объявления."""
    rows = await conn.fetch(
        "select column_name, udt_name, is_nullable, column_default "
        "from information_schema.columns where table_schema = $2 "
        "and table_name = $1 order by ordinal_position",
        table,
        SCHEMA,
    )
    return [
        (r["column_name"], r["udt_name"], r["is_nullable"] == "YES", r["column_default"])
        for r in rows
    ]


async def fetch_constraints(conn: asyncpg.Connection, table: str) -> set[tuple[str, str]]:
    """PK, UNIQUE, FK, CHECK, EXCLUDE таблицы; NOT NULL живёт в колонках."""
    rows = await conn.fetch(
        "select contype::text as kind, pg_get_constraintdef(oid) as def "
        "from pg_constraint where conrelid = ($2 || '.' || $1)::regclass "
        "and contype in ('p', 'u', 'f', 'c', 'x')",
        table,
        SCHEMA,
    )
    return {(r["kind"], r["def"]) for r in rows}


async def fetch_indexes(conn: asyncpg.Connection, table: str) -> set[str]:
    """Определения индексов таблицы (включая предикаты и порядок сортировки)."""
    rows = await conn.fetch(
        "select indexdef from pg_indexes where schemaname = $2 and tablename = $1", table, SCHEMA
    )
    return {r["indexdef"] for r in rows}


async def assert_table_matches(
    conn: asyncpg.Connection,
    table: str,
    columns: list[Column],
    constraints: set[tuple[str, str]],
    indexes: set[str],
) -> None:
    """Сверить таблицу со спецификацией; сообщение называет таблицу и расходящийся аспект."""
    assert await fetch_columns(conn, table) == columns, f"{table}: колонки расходятся с §4.2"
    assert await fetch_constraints(conn, table) == constraints, (
        f"{table}: ограничения расходятся с §4.2"
    )
    assert await fetch_indexes(conn, table) == indexes, f"{table}: индексы расходятся с §4.2"


async def schema_grants(conn: asyncpg.Connection) -> dict[str, set[str]]:
    """Привилегии ролей app_* на схему приложения по has_schema_privilege."""
    return {
        role: {
            priv
            for priv in SCHEMA_PRIVILEGES
            if await conn.fetchval("select has_schema_privilege($1, $2, $3)", role, SCHEMA, priv)
        }
        for role in EXPECTED_SCHEMA_GRANTS
    }


async def table_grants(conn: asyncpg.Connection, table: str) -> dict[str, set[str]]:
    """Привилегии ролей app_rw/app_backup на таблицу по has_table_privilege."""
    return {
        role: {
            priv
            for priv in TABLE_PRIVILEGES
            if await conn.fetchval(
                "select has_table_privilege($1, $2, $3)", role, f"{SCHEMA}.{table}", priv
            )
        }
        for role in EXPECTED_TABLE_GRANTS
    }
