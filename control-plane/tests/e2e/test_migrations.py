"""Сквозная проверка задачи 001.03 (TC-E2E-01): миграция 0001 применяется, откатывается и
применяется снова командой ``python -m app.cli migrate`` под ролью ``app_migrate``.

Нужна база стенда с созданными ролями (``migrations/bootstrap/roles.sql``): ``MIGRATE_DSN`` —
подключение ``app_migrate`` (с паролем или ``MIGRATE_PASSWORD_FILE``), ``PG_DSN`` — ``app_rw``
для проверок. Без базы тест падает с ошибкой подключения, а не пропускается.
"""

from __future__ import annotations

import asyncpg
import psycopg
from app.cli import migrate_dsn

from ._cli import MIGRATIONS_DIR, migration_count, run_cli
from ._spec import EXPECTED_ENUMS

EXPECTED_EXTENSIONS = {"btree_gist", "citext"}
EXPECTED_ROLES = {"app_owner", "app_rw", "app_migrate", "app_backup"}
# Служебные таблицы yoyo — единственные объекты, которыми в control_plane владеет app_migrate.
YOYO_TABLES = {"_yoyo_migration", "_yoyo_log", "_yoyo_version", "yoyo_lock"}


def _acl_by_scope(rows: list[asyncpg.Record]) -> dict[tuple[str, str], set[str]]:
    """Записи pg_default_acl по (тип объекта, область): область — имя схемы или «global»."""
    return {(r["kind"], r["scope"]): set(r["acl"]) for r in rows}


async def db_state(pg_dsn: str) -> dict[str, object]:
    """Снимок объектов базы глазами ``app_rw``: расширения, перечисления, владельцы, роли, ACL."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        extension_rows = await conn.fetch(
            "select extname, pg_get_userbyid(extowner) as owner from pg_extension "
            "where extname = any($1)",
            list(EXPECTED_EXTENSIONS),
        )
        extensions = {r["extname"] for r in extension_rows}
        enum_rows = await conn.fetch(
            "select t.typname, pg_get_userbyid(t.typowner) as owner, "
            "array(select enumlabel::text from pg_enum e where e.enumtypid = t.oid "
            "order by e.enumsortorder) as labels from pg_type t "
            "join pg_namespace n on n.oid = t.typnamespace "
            "where t.typtype = 'e' and n.nspname = 'control_plane'"
        )
        # Владельцы объектов схемы control_plane (таблицы, последовательности, типы, функции), кроме
        # членов расширений: у trusted-расширений они принадлежат bootstrap-суперпользователю.
        owner_rows = await conn.fetch(
            "select c.relname as name, pg_get_userbyid(c.relowner) as owner from pg_class c "
            "join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'control_plane' "
            "and c.relkind in ('r', 'p', 'S', 'v', 'm') and not exists (select 1 from pg_depend d "
            "where d.classid = 'pg_class'::regclass and d.objid = c.oid and d.deptype = 'e') "
            "union all select p.proname, pg_get_userbyid(p.proowner) from pg_proc p "
            "join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'control_plane' "
            "and not exists (select 1 from pg_depend d where d.classid = 'pg_proc'::regclass "
            "and d.objid = p.oid and d.deptype = 'e') "
            "union all select t.typname, pg_get_userbyid(t.typowner) from pg_type t "
            "join pg_namespace n on n.oid = t.typnamespace where n.nspname = 'control_plane' "
            "and t.typtype = 'e' and not exists (select 1 from pg_depend d "
            "where d.classid = 'pg_type'::regclass and d.objid = t.oid and d.deptype = 'e')"
        )
        # Умолчания app_owner: глобальные (namespace 0 — только они могут отозвать встроенное
        # право PUBLIC) и для схемы control_plane. Встроенные умолчания здесь не хранятся.
        default_acl = await conn.fetch(
            "select defaclobjtype::text as kind, defaclacl::text[] as acl, "
            "case when defaclnamespace = 0 then 'global' else defaclnamespace::regnamespace::text "
            "end as scope from pg_default_acl d "
            "join pg_roles r on r.oid = d.defaclrole where r.rolname = 'app_owner'"
        )
        roles = await conn.fetch(
            "select rolname, rolsuper, rolcanlogin from pg_roles where rolname like 'app\\_%'"
        )
        owned_by_rw = await conn.fetchval(
            "select (select count(*) from pg_class c join pg_roles r on r.oid = c.relowner "
            "where r.rolname = 'app_rw') + (select count(*) from pg_type t join pg_roles r "
            "on r.oid = t.typowner where r.rolname = 'app_rw')"
        )
        return {
            "extensions": extensions,
            "extension_owners": {r["owner"] for r in extension_rows},
            "enums": {r["typname"] for r in enum_rows},
            "enum_labels": {r["typname"]: list(r["labels"]) for r in enum_rows},
            "enum_owners": {r["owner"] for r in enum_rows},
            "foreign_owned": {
                (r["name"], r["owner"])
                for r in owner_rows
                if r["owner"] != "app_owner" and r["name"] not in YOYO_TABLES
            },
            "default_acl": _acl_by_scope(default_acl),
            "roles": {r["rolname"] for r in roles},
            "superusers": {r["rolname"] for r in roles if r["rolsuper"]},
            "login_roles": {r["rolname"] for r in roles if r["rolcanlogin"]},
            "owned_by_rw": owned_by_rw,
        }
    finally:
        await conn.close()


def assert_public_cannot_execute_owner_functions(migrate_env: dict[str, str]) -> None:
    """Операционный страж L-1: функция, созданная app_owner (как в миграциях), недоступна
    app_backup и доступна app_rw. Пробная функция живёт только внутри откатываемой транзакции."""
    dsn = migrate_dsn(migrate_env["MIGRATE_DSN"], migrate_env.get("MIGRATE_PASSWORD_FILE"))
    with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://", 1)) as conn:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SET LOCAL ROLE app_owner")
            cur.execute("CREATE FUNCTION _acl_probe() RETURNS int LANGUAGE sql AS 'select 1'")
            cur.execute(
                "SELECT has_function_privilege('app_backup', '_acl_probe()', 'EXECUTE'), "
                "has_function_privilege('app_rw', '_acl_probe()', 'EXECUTE'), "
                "(SELECT proacl::text[] FROM pg_proc WHERE proname = '_acl_probe')"
            )
            row = cur.fetchone()
            assert row is not None
            backup_can, rw_can, proacl = row
            assert backup_can is False, f"app_backup выполняет функцию app_owner: acl={proacl}"
            assert rw_can is True, f"app_rw не может выполнить функцию app_owner: acl={proacl}"
            assert not any(a.startswith("=X") for a in proacl), proacl
            raise psycopg.Rollback  # пробная функция не переживает транзакцию


async def test_migration_0001_apply_rollback_apply(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """TC-E2E-01: apply → объекты есть; rollback → их нет, роли остались; apply → снова есть."""
    applied = run_cli(migrate_env, "migrate")
    assert applied.returncode == 0, applied.stderr
    assert_migrated_state(await db_state(pg_dsn), migrate_env)

    rolled_back = run_cli(migrate_env, "migrate", "--rollback-all")
    assert rolled_back.returncode == 0, rolled_back.stderr
    state = await db_state(pg_dsn)
    assert state["extensions"] == set()
    assert state["enums"] == set()
    assert state["default_acl"] == {}, "откат возвращает умолчания PostgreSQL"
    assert state["roles"] == EXPECTED_ROLES, "роли создаёт bootstrap, откат их не трогает"

    # Повторное применение проверяется тем же набором: первый `migrate` на мигрированной базе
    # ничего не применяет, и только здесь текущие файлы миграций реально выполняются.
    reapplied = run_cli(migrate_env, "migrate")
    assert reapplied.returncode == 0, reapplied.stderr
    assert f"применено — {migration_count()}" in reapplied.stdout
    assert_migrated_state(await db_state(pg_dsn), migrate_env)


def assert_migrated_state(state: dict[str, object], migrate_env: dict[str, str]) -> None:
    """Инварианты мигрированной базы: объекты, владельцы, роли, умолчания привилегий."""
    assert state["extensions"] == EXPECTED_EXTENSIONS
    assert state["extension_owners"] == {"app_owner"}, "владелец расширений — app_owner (§4.6)"
    assert state["enums"] == set(EXPECTED_ENUMS)
    assert state["enum_labels"] == EXPECTED_ENUMS, "значения перечислений — побуквенно по §4.2"
    assert state["enum_owners"] == {"app_owner"}, "владелец типов — app_owner (§4.6)"
    assert state["foreign_owned"] == set(), (
        "в control_plane всё принадлежит app_owner, кроме служебных таблиц yoyo: "
        "миграция без SET LOCAL ROLE app_owner"
    )
    assert state["roles"] == EXPECTED_ROLES
    assert state["superusers"] == set(), "роли приложения не суперпользователи"
    assert state["login_roles"] == EXPECTED_ROLES - {"app_owner"}
    assert state["owned_by_rw"] == 0, "app_rw не владеет объектами (AC 001.03)"
    acl = state["default_acl"]
    assert isinstance(acl, dict)
    assert acl.get(("r", "control_plane")) == {"app_rw=arwd/app_owner", "app_backup=r/app_owner"}, (
        "будущие таблицы app_owner: app_rw — DML, app_backup — только SELECT"
    )
    assert acl.get(("S", "control_plane")) == {"app_rw=rU/app_owner"}
    assert acl.get(("f", "control_plane")) == {"app_rw=X/app_owner"}
    # Отзыв EXECUTE у PUBLIC виден только как глобальная запись без «=X» и с одним app_owner;
    # без REVOKE записи нет вовсе (встроенное умолчание в pg_default_acl не хранится).
    assert acl.get(("f", "global")) == {"app_owner=X/app_owner"}, (
        "EXECUTE у PUBLIC на функции app_owner отозван глобальной записью pg_default_acl"
    )
    assert_public_cannot_execute_owner_functions(migrate_env)


def test_migrate_is_idempotent(migrate_env: dict[str, str]) -> None:
    """Второй запуск подряд применяет 0 и завершается кодом 0 (старт api, §10.2); порядок тестов
    значения не имеет: первый запуск внутри теста приводит базу к «всё применено»."""
    first = run_cli(migrate_env, "migrate")
    assert first.returncode == 0, first.stderr
    second = run_cli(migrate_env, "migrate")
    assert second.returncode == 0, second.stderr
    assert "применено — 0" in second.stdout


def test_every_migration_sets_owner_role() -> None:
    """Статический страж соглашения §4.6: каждая миграция и её откат начинаются с
    ``SET LOCAL ROLE app_owner`` и ``SET LOCAL search_path TO control_plane`` — иначе объекты
    достанутся app_migrate или окажутся в public."""
    python_migrations = sorted(MIGRATIONS_DIR.glob("*.py"))
    assert not python_migrations, f"миграции только SQL (страж владения): {python_migrations}"
    files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.parent == MIGRATIONS_DIR)
    assert files, "миграций нет"
    for path in files:
        statements = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]
        assert statements[:2] == [
            "SET LOCAL ROLE app_owner;",
            "SET LOCAL search_path TO control_plane;",
        ], f"{path.name}: миграция начинается с SET LOCAL ROLE app_owner; SET LOCAL search_path"


def test_admin_create_is_a_stub(migrate_env: dict[str, str]) -> None:
    """``admin create`` до 001.47 честно отказывает кодом 69, а не притворяется выполненным."""
    result = run_cli(migrate_env, "admin", "create")
    assert result.returncode == 69
    assert "001.47" in result.stderr


def test_migrate_break_lock(migrate_env: dict[str, str]) -> None:
    """Замок yoyo, оставшийся от клиента, умершего посреди миграции: ``migrate`` завершается
    кодом 1 по таймауту замка с подсказкой, ``migrate --break-lock`` снимает замок, применяет
    миграции и оставляет таблицу замка пустой. Строка замка вставляется тестом от app_migrate."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    dsn = migrate_dsn(migrate_env["MIGRATE_DSN"], migrate_env.get("MIGRATE_PASSWORD_FILE"))
    with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://", 1)) as conn:
        conn.execute("INSERT INTO yoyo_lock (locked, ctime, pid) VALUES (1, now(), 0)")
    try:
        blocked = run_cli(migrate_env, "migrate")  # таймаут замка yoyo — 10 с
        assert blocked.returncode == 1, blocked.stderr
        assert "заблокирована" in blocked.stderr and "--break-lock" in blocked.stderr, (
            blocked.stderr
        )
        freed = run_cli(migrate_env, "migrate", "--break-lock")
        assert freed.returncode == 0, freed.stderr
        assert "замок yoyo снят" in freed.stderr and "применено — 0" in freed.stdout, freed
        with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://", 1)) as conn:
            assert conn.execute("SELECT count(*) FROM yoyo_lock").fetchone() == (0,), (
                "после --break-lock и штатного выхода замок снят"
            )
    finally:
        with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://", 1)) as conn:
            conn.execute("DELETE FROM yoyo_lock")
