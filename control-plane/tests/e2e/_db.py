"""Мелкая интроспекция базы для сквозных тестов миграций."""

from __future__ import annotations

import itertools
import uuid

import asyncpg


async def existing_tables(pg_dsn: str) -> set[str]:
    """Таблицы схемы control_plane, видимые роли подключения (включая партиционированные)."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        rows = await conn.fetch(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'control_plane' and c.relkind in ('r', 'p')"
        )
        return {r["relname"] for r in rows}
    finally:
        await conn.close()


_NODE_COUNTER = itertools.count(1)


async def insert_node(
    conn: asyncpg.Connection, code: str, billing_group_id: uuid.UUID
) -> uuid.UUID:
    """Вставить ноду с обязательными полями §4.2.3; вернуть id. Адреса — из документационного
    диапазона 203.0.113.0/24, по счётчику: детерминированно и без коллизий внутри процесса."""
    octet = next(_NODE_COUNTER) % 250 + 1
    node_id: uuid.UUID = await conn.fetchval(
        "insert into nodes (code, name, country, city, provider, public_ipv4, billing_group_id, "
        "bandwidth_mbps, max_conn_per_ip) values ($1, $1, 'JP', 'Tokyo', 'probe', $2, $3, 1000, 8) "
        "returning id",
        code,
        f"203.0.113.{octet}",
        billing_group_id,
    )
    return node_id
