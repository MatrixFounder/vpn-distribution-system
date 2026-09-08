"""Стражи bootstrap-слоя (``migrations/bootstrap/roles.sql``): схема ``control_plane`` и её права —
несущий уровень модели прав §4.6 после переезда объектов из ``public``.

Проверяется: владелец схемы ``app_owner``; ``USAGE`` у ``app_rw``/``app_backup``, ``USAGE, CREATE``
у ``app_migrate`` и ничего сверх; ``search_path = control_plane`` у трёх ролей в базе; ``public``
без объектов приложения. Без базы тест падает, не пропускается.
"""

from __future__ import annotations

import asyncpg

from ._introspect import EXPECTED_SCHEMA_GRANTS, SCHEMA, schema_grants

APP_ROLES_WITH_SEARCH_PATH = {"app_rw", "app_migrate", "app_backup"}


async def test_schema_owner_and_privileges(pg_dsn: str) -> None:
    """Схема принадлежит app_owner; права на неё — ровно по §4.6."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        owner = await conn.fetchval(
            "select pg_get_userbyid(nspowner) from pg_namespace where nspname = $1", SCHEMA
        )
        assert owner == "app_owner"
        assert await schema_grants(conn) == EXPECTED_SCHEMA_GRANTS
        # app_migrate наследует права владельца базы (владелец public — pg_database_owner);
        # его удерживают search_path и статический страж миграций. Роли DML/чтения — никогда.
        for role in ("app_rw", "app_backup"):
            can_create = await conn.fetchval(
                "select has_schema_privilege($1, 'public', 'CREATE')", role
            )
            assert can_create is False, f"{role} не создаёт объекты в public"
    finally:
        await conn.close()


async def test_roles_search_path(pg_dsn: str) -> None:
    """search_path = control_plane задан трём ролям приложения в базе (pg_db_role_setting)."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        rows = await conn.fetch(
            "select r.rolname, s.setconfig from pg_db_role_setting s "
            "join pg_roles r on r.oid = s.setrole join pg_database d on d.oid = s.setdatabase "
            "where d.datname = current_database() and r.rolname like 'app\\_%'"
        )
        settings = {r["rolname"]: set(r["setconfig"]) for r in rows}
        assert set(settings) == APP_ROLES_WITH_SEARCH_PATH, settings
        for role, config in settings.items():
            assert f"search_path={SCHEMA}" in config, f"{role}: {config}"
        assert await conn.fetchval("select current_schema()") == SCHEMA
    finally:
        await conn.close()


async def test_public_has_no_application_objects(pg_dsn: str) -> None:
    """В public нет таблиц, последовательностей, типов и функций приложения."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        counts = await conn.fetchrow(
            "select (select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            " where n.nspname = 'public' and c.relkind in ('r', 'p', 'S', 'v', 'm')) as relations, "
            "(select count(*) from pg_type t join pg_namespace n on n.oid = t.typnamespace "
            " where n.nspname = 'public' and t.typtype = 'e') as enums, "
            "(select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
            " where n.nspname = 'public') as functions, "
            "(select count(*) from pg_extension where extnamespace = 'public'::regnamespace) "
            " as extensions"
        )
        assert counts is not None and tuple(counts) == (0, 0, 0, 0), dict(counts)
    finally:
        await conn.close()
