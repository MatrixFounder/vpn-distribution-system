"""Три сверки учёта (постановка §5.9 «Сверка»; data-model.md §4.2.5 ``reconciliation_runs``,
``traffic_gaps``; R-21, Н-18).

Задача 001.33: сигнатуры и результаты с фиксированными значениями. Запись прогонов, разрывов и
алерты — 001.37; коридор межисточниковой сверки — ОВ-24.

Функции принимают подключение первым аргументом (как ``multiplier.resolve`` и
``stats.user_traffic``): обработчик очереди отдаёт им своё подключение и свои транзакции.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal

import asyncpg

# Перечисление ``reconciliation_kind`` §4.2.5.
Kind = Literal["arithmetic", "cross_source", "continuity"]


def _empty_scope() -> Mapping[str, Any]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ReconciliationRun:
    """Строка ``reconciliation_runs``: что сравнивалось, ожидаемое и фактическое, расхождение в
    процентах (``numeric(6,3)``) и исход. ``scope`` — неизменяемое отображение, как и сама
    запись."""

    kind: Kind
    scope: Mapping[str, Any] = field(default_factory=_empty_scope)
    expected: int = 0
    actual: int = 0
    delta_pct: Decimal = Decimal("0.000")
    status: str = "ok"


@dataclass(frozen=True, slots=True)
class TrafficGap:
    """Строка ``traffic_gaps``: интервал недоучёта ноды, причина, оценка объёма (Н-8; §5.9
    «Вывод ноды из эксплуатации»)."""

    node_id: uuid.UUID
    gap_start: dt.datetime
    gap_end: dt.datetime
    reason: str
    estimated_bytes: int | None = None


async def arithmetic(conn: asyncpg.Connection, day: dt.date) -> ReconciliationRun:
    """Арифметическая сверка за сутки: сумма принятых отчётов по нодам против изменения баланса
    пользователей за те же сутки; расхождение свыше Н-18 (0,5 %) — алерт. Заглушка: 0 %."""
    return ReconciliationRun(kind="arithmetic", scope=MappingProxyType({"day": day.isoformat()}))


async def cross_source(
    conn: asyncpg.Connection, node_id: uuid.UUID, day: dt.date, tolerance_pct: Decimal
) -> ReconciliationRun:
    """Межисточниковая сверка за сутки: ``raw_bytes`` пользователей ноды против счётчиков её
    интерфейса (``node_interface_hourly``); расхождение ожидаемо (служебный трафик, накладные
    расходы), алерт — сверх коридора ``tolerance_pct`` (ОВ-24). Заглушка: 0 %."""
    return ReconciliationRun(
        kind="cross_source",
        scope=MappingProxyType({"node_id": str(node_id), "day": day.isoformat()}),
    )


async def continuity(conn: asyncpg.Connection, node_id: uuid.UUID) -> list[TrafficGap]:
    """Непрерывность отчётов ноды: пропуск ``report_seq`` в пределах ``counter_epoch`` и
    неизвестная эпоха дают запись разрыва с оценкой объёма и алерт; сюда же попадает буфер,
    остановленный отказом ``node_mismatch`` или ``rejected_time`` (номера перестают расти).
    Заглушка: разрывов нет."""
    return []
