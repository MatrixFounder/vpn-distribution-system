"""Команды обслуживания Control Plane (`python -m app.cli …`).

- ``migrate`` — применить миграции yoyo из ``control-plane/migrations`` под ролью ``app_migrate``
  (docs/architectures/data-model.md §4.6, deployment.md §10.2); ``--rollback`` откатывает последнюю
  применённую, ``--rollback-all`` — все; ``--break-lock`` снимает замок yoyo, оставшийся от
  клиента, умершего посреди миграции (после него ``migrate`` падает по таймауту замка).
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
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

EX_USAGE = 64
EX_NOT_IMPLEMENTED = 69


def read_secret(path: str | os.PathLike[str]) -> str:
    """Прочитать файл секрета (Docker secret): UTF-8, без завершающего перевода строки."""
    return Path(path).read_text(encoding="utf-8").rstrip("\r\n")


def migrate_dsn(dsn: str | None = None, password_file: str | None = None) -> str:
    """Собрать DSN для yoyo: схема ``postgresql+psycopg``, пароль из файла, если в URL его нет.

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
    netloc = parts.netloc
    if password_file is None:
        password_file = os.environ.get("MIGRATE_PASSWORD_FILE")
    if parts.password is None and password_file:
        host = parts.hostname or ""
        if ":" in host:  # IPv6 — urlsplit снимает скобки, URL требует их вернуть
            host = f"[{host}]"
        hostport = f"{host}:{parts.port}" if parts.port else host
        netloc = f"{parts.username or ''}:{quote(read_secret(password_file), safe='')}@{hostport}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))


def run_migrate(
    dsn: str, *, rollback: bool = False, rollback_all: bool = False, break_lock: bool = False
) -> int:
    """Применить миграции из ``MIGRATIONS_DIR``, либо откатить последнюю (``rollback``) или все
    (``rollback_all``); ``break_lock`` перед этим снимает зависший замок yoyo. Код завершения."""
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
        )
    if args.command == "admin" and args.admin_command == "create":
        print(
            "admin create: реализуется задачей 001.47 (первый Super Admin, §10.4)", file=sys.stderr
        )
        return EX_NOT_IMPLEMENTED
    return EX_USAGE


if __name__ == "__main__":
    sys.exit(main())
