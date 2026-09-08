"""Мелкая интроспекция базы для сквозных тестов миграций."""

from __future__ import annotations

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
