"""Запуск `python -m app.cli` из тестов: общий помощник сквозных проверок миграций."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CONTROL_PLANE_DIR = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = CONTROL_PLANE_DIR / "migrations"


def migration_count() -> int:
    """Число миграций в источнике (apply-файлы без пары .rollback.sql)."""
    return sum(1 for p in MIGRATIONS_DIR.glob("*.sql") if not p.name.endswith(".rollback.sql"))


def run_cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Запустить ``python -m app.cli`` из каталога control-plane и вернуть результат."""
    return subprocess.run(  # noqa: S603 — аргументы фиксированы тестом
        [sys.executable, "-m", "app.cli", *args],
        cwd=CONTROL_PLANE_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
