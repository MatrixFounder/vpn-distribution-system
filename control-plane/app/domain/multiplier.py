"""Разрешение коэффициента трафика по дате и целочисленное списание (постановка §4.9;
data-model.md §4.2.2 ``billing_group_multipliers``, ``node_billing_assignments``,
``nodes.multiplier_override_milli``; UC-04 шаг 7, A7; R-23).

Задача 001.33: сигнатуры с фиксированными значениями. Разрешение «нода → тарифицируемая группа
→ 1.0» на момент ``period_start`` отчёта (не на момент приёма: буфер досылает отчёты возрастом
до 24 часов, а ретроактивное изменение списаний запрещено) и ``floor(raw × multiplier)`` в
целых — 001.23 (``tdd-strict``).

Представление: целое число тысячных (``1000`` — 1.0), диапазон 0…10000 с шагом 100 (``CHECK``
таблиц §4.2.2 и §4.2.5) — ``Resolved`` держит это как инвариант; ``0`` — трафик ноды не
списывается, ``raw_bytes`` записывается.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Literal

import asyncpg

from app.domain.nodes import STUB_BILLING_GROUP_ID

# Три уровня §4.9 «Модель и разрешение», по убыванию приоритета.
Source = Literal["node", "group", "default"]
DEFAULT_MULTIPLIER_MILLI = 1000  # 1.0 — системное значение по умолчанию (§4.9)
MULTIPLIER_MILLI_MAX = 10000  # 10.0
MULTIPLIER_STEP_MILLI = 100  # шаг 0.1


@dataclass(frozen=True, slots=True)
class Resolved:
    """Коэффициент, действовавший на момент запроса: значение в тысячных, тарифицируемая группа
    ноды на тот момент (обе величины фиксируются в записи статистики, §5.9) и уровень, с
    которого значение взято. Значение вне диапазона или не кратное шагу — ошибка программы, а
    не данные: ``CHECK`` таблиц его всё равно не примет."""

    multiplier_milli: int
    billing_group_id: uuid.UUID
    source: Source

    def __post_init__(self) -> None:
        if not 0 <= self.multiplier_milli <= MULTIPLIER_MILLI_MAX:
            raise ValueError(f"коэффициент вне диапазона 0…{MULTIPLIER_MILLI_MAX} тысячных")
        if self.multiplier_milli % MULTIPLIER_STEP_MILLI:
            raise ValueError(f"коэффициент не кратен шагу {MULTIPLIER_STEP_MILLI} тысячных")


async def resolve(conn: asyncpg.Connection, node_id: uuid.UUID, at: dt.datetime) -> Resolved:
    """Коэффициент ноды на момент ``at``: переопределение ноды, иначе коэффициент её
    тарифицируемой группы, иначе 1.0 (``source = default`` — и до ввода ноды в эксплуатацию).
    Вызывается один раз на отчёт: границы периода принадлежат отчёту, а не строке, и все
    строки получают одно значение. Заглушка: 1.0 из ``default`` для фиксированной группы,
    база не читается."""
    return Resolved(
        multiplier_milli=DEFAULT_MULTIPLIER_MILLI,
        billing_group_id=STUB_BILLING_GROUP_ID,
        source="default",
    )


def billable(raw: int, milli: int) -> int:
    """Списываемый объём ``floor(raw × multiplier)`` в целых (§4.9: без чисел с плавающей
    точкой). Заглушка: коэффициент 1.0 — ``raw``, ``milli`` не читается; ``raw * milli // 1000``
    вводит 001.23 вместе с таблицей случаев. Произведение при ``milli`` до 10000 и ``raw`` у
    ширины ``bigint`` выходит за колонку — настоящая граница строки задаётся порогом прироста
    по полосе ноды (§5.9, 001.34), а не шириной типа."""
    return raw
