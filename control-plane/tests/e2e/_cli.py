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


def migration_ids() -> list[str]:
    """Идентификаторы миграций источника в порядке применения (линейные зависимости → по имени)."""
    return sorted(
        p.stem for p in MIGRATIONS_DIR.glob("*.sql") if not p.name.endswith(".rollback.sql")
    )


def steps_to_remove(migration_id: str) -> int:
    """Сколько откатов на шаг снимают ``migration_id`` при полностью применённом источнике:
    сама миграция плюс все более поздние."""
    ids = migration_ids()
    assert migration_id in ids, f"{migration_id} нет в {MIGRATIONS_DIR}"
    return len(ids) - ids.index(migration_id)


def rollback_through(env: dict[str, str], migration_id: str) -> int:
    """Откатить ровно столько шагов, чтобы снять ``migration_id`` (и всё, что новее); вернуть
    число шагов. Каждый шаг должен завершиться кодом 0 и снять ровно одну миграцию."""
    steps = steps_to_remove(migration_id)
    for _ in range(steps):
        result = run_cli(env, "migrate", "--rollback")
        assert result.returncode == 0, result.stderr
        assert "откачено миграций — 1" in result.stdout, result.stdout
    return steps
