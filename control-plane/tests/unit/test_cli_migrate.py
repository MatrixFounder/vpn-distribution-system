"""Ключевой объект миграции для самопроверки ``migrate`` (WI-3, L-3): каждая миграция каталога
сверяется с таблицей ожидаемых ключей. Новая миграция без записи здесь — отказ: схемная миграция,
ключ которой ``key_object`` не распознаёт, иначе выпала бы из самопроверки молча, как 110–150 до
L-3."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.cli import MIGRATIONS_DIR, key_object

# id миграции → (вид, имя со схемой) ключевого объекта; None — миграция только с данными.
EXPECTED_KEYS: dict[str, tuple[str, str] | None] = {
    "0001_extensions_roles_enums": ("type", "control_plane.user_status"),
    "040_schema_identity": ("table", "control_plane.users"),
    "050_schema_catalog": ("table", "control_plane.plans"),
    "060_schema_nodes": ("table", "control_plane.nodes"),
    "070_schema_subscriptions": ("table", "control_plane.subscription_periods"),
    "080_schema_accounting": ("table", "control_plane.traffic_reports"),
    "090_schema_ops": ("table", "control_plane.events"),
    "100_settings_auth": None,
    "110_jobs_retry": ("column", "control_plane.jobs.claimed_at"),
    "120_settings_email_tokens": None,
    "130_node_identity_enrolled_from": ("column", "control_plane.node_identities.enrolled_from"),
    "131_bootstrap_token_annulled_at": ("column", "control_plane.bootstrap_tokens.annulled_at"),
    # ADD COLUMN traffic_reports.parts_total стоит раньше, но таблица — ключ первой очереди.
    "140_report_acceptance": ("table", "control_plane.report_intervals"),
    "150_node_agent_section_version": ("column", "control_plane.nodes.agent_section_version"),
}

_DDL = re.compile(r"^\s*(CREATE|ALTER|DROP)\b", re.IGNORECASE | re.MULTILINE)


def _apply_files() -> dict[str, Path]:
    return {p.stem: p for p in MIGRATIONS_DIR.glob("*.sql") if not p.stem.endswith(".rollback")}


def test_every_migration_has_an_expected_key() -> None:
    assert set(_apply_files()) == set(EXPECTED_KEYS)


@pytest.mark.parametrize("migration_id", sorted(EXPECTED_KEYS))
def test_key_object_of_each_migration(migration_id: str) -> None:
    sql = _apply_files()[migration_id].read_text(encoding="utf-8")
    assert key_object(sql) == EXPECTED_KEYS[migration_id]


@pytest.mark.parametrize(
    "migration_id", sorted(m for m, key in EXPECTED_KEYS.items() if key is None)
)
def test_a_migration_without_key_holds_only_data(migration_id: str) -> None:
    """Ключ None допустим только у миграции без DDL: её объекты самопроверка не ищет."""
    sql = _apply_files()[migration_id].read_text(encoding="utf-8")
    assert _DDL.findall(sql) == []


def test_a_qualified_table_keeps_its_schema_for_the_column() -> None:
    sql = "SET LOCAL search_path TO control_plane;\nALTER TABLE other.t ADD COLUMN c int;\n"
    assert key_object(sql) == ("column", "other.t.c")
