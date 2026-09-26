"""Запуск `python -m app.cli` из тестов: общий помощник сквозных проверок миграций."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ._db import owner_connection

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


def start_cli(env: dict[str, str], *args: str) -> subprocess.Popen[str]:
    """Запустить ``python -m app.cli`` в фоне (тест гонки ждёт его на блокировке базы)."""
    return subprocess.Popen(  # noqa: S603 — аргументы фиксированы тестом
        [sys.executable, "-m", "app.cli", *args],
        cwd=CONTROL_PLANE_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
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


# Миграция 130 откатывается только на пустой node_identities (её откат стёр бы серийные номера
# листов). Identity тестов — у нод с кодами этих префиксов: сквозные тесты (_catalog.PREFIX) и
# пробы стенда (tests/stand/) — убитый прогон их не убирает. Тесты миграций и пробу стенда
# одновременно не запускают: откат через 130 снял бы identity идущей пробы.
IDENTITY_MIGRATION = "130_node_identity_enrolled_from"
TEST_NODE_CODES = ("t19-%", "t25-%")


def clear_test_identities(env: dict[str, str]) -> None:
    """Снять identity тестовых нод перед откатом через 130; другие identity — не тестовые, и
    откат обязан отказать: сообщение называет их число, а не падает на NOT NULL повтора."""
    with owner_connection(env) as conn:
        conn.execute(
            "delete from node_identities where node_id in "
            "(select id from nodes where code like any(%s))",
            [list(TEST_NODE_CODES)],
        )
        row = conn.execute("select count(*) from node_identities").fetchone()
    assert row is not None and row[0] == 0, (
        f"в node_identities {row} нетестовых строк: откат 130 их стёр бы — сначала удалите их"
    )


def rollback_through(env: dict[str, str], migration_id: str) -> int:
    """Откатить ровно столько шагов, чтобы снять ``migration_id`` (и всё, что новее); вернуть
    число шагов. Каждый шаг должен завершиться кодом 0 и снять ровно одну миграцию. Откат через
    130 — после уборки identity тестовых нод (``clear_test_identities``)."""
    steps = steps_to_remove(migration_id)
    if steps >= steps_to_remove(IDENTITY_MIGRATION):
        clear_test_identities(env)
    for _ in range(steps):
        result = run_cli(env, "migrate", "--rollback")
        assert result.returncode == 0, result.stderr
        assert "откачено миграций — 1" in result.stdout, result.stdout
    return steps
