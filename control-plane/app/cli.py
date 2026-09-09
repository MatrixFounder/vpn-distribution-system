"""Команды обслуживания Control Plane (`python -m app.cli …`).

- ``migrate`` — применить миграции yoyo из ``control-plane/migrations`` под ролью ``app_migrate``
  (docs/architectures/data-model.md §4.6, deployment.md §10.2); ``--rollback`` откатывает последнюю
  применённую, ``--rollback-all`` — все; ``--break-lock`` снимает замок yoyo, оставшийся от
  клиента, умершего посреди миграции (после него ``migrate`` падает по таймауту замка).
  Перед применением и откатом — самопроверка учёта (WI-3): у каждой применённой по учёту yoyo
  миграции должен существовать её ключевой объект (первый ``CREATE TABLE``/``CREATE TYPE``
  файла); отметка без объектов — след клиента, умершего между откатом и снятием отметки, —
  завершает команду кодом ``EX_MARK_WITHOUT_OBJECTS`` (65) с подсказкой, а ``--unmark <id>``
  снимает такую отметку, после чего ``migrate`` применяет миграцию заново. Миграции без
  создаваемых объектов (только данные) самопроверкой не охватываются.
  Подключение: ``MIGRATE_DSN`` (``postgresql://app_migrate@host:5432/db`` — пароль из файла
  ``MIGRATE_PASSWORD_FILE``, либо полный URL с паролем). Миграции выполняются с блокировкой yoyo,
  поэтому несколько экземпляров ``api`` при старте не мешают друг другу.
- ``admin create`` — заглушка до задачи 001.47 (первый Super Admin, §10.4).

Роли базы (``app_owner``, ``app_rw``, ``app_migrate``, ``app_backup``) — кластерные объекты и
создаются не миграциями, а ``migrations/bootstrap/roles.sql`` при инициализации кластера.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from app.config import SecretError, dsn_with_password

if TYPE_CHECKING:
    from yoyo.migrations import Migration

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

EX_USAGE = 64
EX_MARK_WITHOUT_OBJECTS = 65  # EX_DATAERR: учёт yoyo расходится с содержимым базы
EX_NOT_IMPLEMENTED = 69

# Ключевой объект миграции — первый CREATE TABLE / CREATE TYPE её apply-файла; схема — из
# SET LOCAL search_path миграции (объекты создаются в ней, а не в search_path подключения).
_KEY_OBJECT = re.compile(
    r"^\s*CREATE\s+(TABLE|TYPE)\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w.]*)",
    re.IGNORECASE | re.MULTILINE,
)
_SEARCH_PATH = re.compile(r"^\s*SET\s+(?:LOCAL\s+)?search_path\s+TO\s+([A-Za-z_]\w*)", re.I | re.M)


def key_object(sql: str) -> tuple[str, str] | None:
    """(``table`` | ``type``, имя со схемой) первого создаваемого объекта миграции или ``None``
    для миграции без объектов (только данные)."""
    match = _KEY_OBJECT.search(sql)
    if match is None:
        return None
    kind, name = match.group(1).lower(), match.group(2)
    if "." not in name:
        schema = _SEARCH_PATH.search(sql)
        if schema is not None:
            name = f"{schema.group(1)}.{name}"
    return kind, name


def marks_without_objects(backend: Any, migrations: Any) -> list[tuple[str, str]]:
    """Применённые по учёту yoyo миграции, ключевой объект которых в базе отсутствует:
    ``[(id, объект)]``. Возникает, когда клиент умер между транзакцией отката и снятием отметки
    (yoyo делает их раздельно): следующая ``migrate`` считала бы миграцию применённой."""
    broken: list[tuple[str, str]] = []
    applied: list[Migration] = list(backend.to_rollback(migrations))
    with backend.connection.cursor() as cursor:
        for migration in applied:
            key = key_object(Path(migration.path).read_text(encoding="utf-8"))
            if key is None:
                continue
            kind, name = key
            lookup = "to_regclass" if kind == "table" else "to_regtype"
            cursor.execute(f"SELECT {lookup}(%s)", (name,))  # noqa: S608 — имя функции из двух констант
            row = cursor.fetchone()
            if row is None or row[0] is None:
                broken.append((migration.id, name))
    return broken


def migrate_dsn(dsn: str | None = None, password_file: str | None = None) -> str:
    """Собрать DSN для yoyo: схема ``postgresql+psycopg``, пароль из файла, если в URL его нет
    (``app.config.dsn_with_password``; пустой или недоступный файл — ошибка).

    ``dsn`` и ``password_file`` по умолчанию берутся из ``MIGRATE_DSN`` и ``MIGRATE_PASSWORD_FILE``.
    """
    if dsn is None:
        dsn = os.environ.get("MIGRATE_DSN", "")
    if not dsn:
        raise SystemExit("migrate: не задан MIGRATE_DSN")
    parts = urlsplit(dsn)
    if parts.scheme in ("postgresql", "postgres"):
        scheme = "postgresql+psycopg"
    elif parts.scheme == "postgresql+psycopg":
        scheme = parts.scheme
    else:
        raise SystemExit(f"migrate: неподдерживаемая схема DSN «{parts.scheme}»")
    if password_file is None:
        password_file = os.environ.get("MIGRATE_PASSWORD_FILE")
    try:
        with_password = dsn_with_password(dsn, password_file)
    except SecretError as exc:
        raise SystemExit(f"migrate: {exc}") from exc
    parts = urlsplit(with_password)
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


def run_migrate(
    dsn: str,
    *,
    rollback: bool = False,
    rollback_all: bool = False,
    break_lock: bool = False,
    unmark: str | None = None,
) -> int:
    """Применить миграции из ``MIGRATIONS_DIR``, либо откатить последнюю (``rollback``) или все
    (``rollback_all``); ``break_lock`` перед этим снимает зависший замок yoyo; ``unmark`` снимает
    отметку миграции, объектов которой в базе нет (самопроверка при этом не выполняется — это и
    есть ремонт). Код завершения."""
    from psycopg import OperationalError
    from yoyo import get_backend, read_migrations
    from yoyo.exceptions import LockTimeout

    try:
        backend = get_backend(dsn)
    except OperationalError as exc:
        # Одна строка вместо трассировки psycopg: причина видна в журнале старта api.
        print(f"migrate: нет подключения к базе — {exc}".rstrip(), file=sys.stderr)
        return 1
    migrations = read_migrations(str(MIGRATIONS_DIR))
    if break_lock:
        backend.break_lock()
        print("migrate: замок yoyo снят", file=sys.stderr)
    try:
        with backend.lock():
            if unmark is not None:
                selected = [m for m in migrations if m.id == unmark]
                if not selected:
                    print(f"migrate: миграции «{unmark}» нет в источнике", file=sys.stderr)
                    return EX_USAGE
                backend.unmark_migrations(selected)
                print(f"migrate: отметка снята — {unmark}; следующая migrate применит её заново")
                return 0
            broken = marks_without_objects(backend, migrations)
            if broken:
                for migration_id, name in broken:
                    print(
                        f"migrate: миграция {migration_id} отмечена применённой, но объекта {name} "
                        f"в базе нет — прерванный откат; ремонт: migrate --unmark {migration_id}, "
                        "затем migrate",
                        file=sys.stderr,
                    )
                return EX_MARK_WITHOUT_OBJECTS
            if rollback or rollback_all:
                selected = backend.to_rollback(migrations)
                if rollback:
                    selected = selected[:1]  # новейшая применённая
                backend.rollback_migrations(selected)
                print(f"migrate: откачено миграций — {len(selected)}")
            else:
                selected = backend.to_apply(migrations)
                backend.apply_migrations(selected)
                print(
                    f"migrate: применено — {len(selected)}, всего в источнике — {len(migrations)}"
                )
    except LockTimeout as exc:
        # Замок держит другой процесс (параллельный старт api — дождаться) либо он остался от
        # клиента, умершего посреди миграции: тогда migrate --break-lock.
        print(
            f"migrate: база заблокирована — {exc}; если процесса нет: --break-lock", file=sys.stderr
        )
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Разбор аргументов: ``migrate [--rollback | --rollback-all]``, ``admin create``."""
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    migrate = sub.add_parser("migrate", help="применить миграции yoyo под ролью app_migrate")
    direction = migrate.add_mutually_exclusive_group()
    direction.add_argument("--rollback", action="store_true", help="откатить последнюю миграцию")
    direction.add_argument(
        "--rollback-all", action="store_true", help="откатить все применённые миграции"
    )
    migrate.add_argument(
        "--break-lock",
        action="store_true",
        help="снять замок yoyo, оставшийся от прерванного запуска, перед выполнением",
    )
    direction.add_argument(
        "--unmark",
        metavar="MIGRATION_ID",
        help="снять отметку миграции, объектов которой нет (прерванный откат), без самопроверки",
    )
    admin = sub.add_parser("admin", help="управление административными учётными записями")
    admin_sub = admin.add_subparsers(dest="admin_command", required=True)
    admin_sub.add_parser("create", help="создать первого Super Admin (задача 001.47)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа CLI; сообщения — UTF-8 независимо от локали контейнера."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        return run_migrate(
            migrate_dsn(),
            rollback=args.rollback,
            rollback_all=args.rollback_all,
            break_lock=args.break_lock,
            unmark=args.unmark,
        )
    if args.command == "admin" and args.admin_command == "create":
        print(
            "admin create: реализуется задачей 001.47 (первый Super Admin, §10.4)", file=sys.stderr
        )
        return EX_NOT_IMPLEMENTED
    return EX_USAGE


if __name__ == "__main__":
    sys.exit(main())
