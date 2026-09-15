"""Суточная агрегация (постановка §5.9 «Агрегация и хранение»; data-model.md §4.2.5
``traffic_daily``; R-22, Н-21).

Задача 001.33: сигнатура с фиксированным результатом. ``traffic_daily`` из ``traffic_hourly`` за
сутки, идемпотентный повтор (``INSERT … ON CONFLICT … DO UPDATE``, без дублей), пересчёт после
позднего отчёта по ключу задачи ``aggregate:{day}`` (§5.4) — 001.37.
"""

from __future__ import annotations

import datetime as dt

import asyncpg


async def aggregate_day(conn: asyncpg.Connection, day: dt.date) -> int:
    """Собрать суточные агрегаты «пользователь × нода» за ``day`` (UTC, как партиции §4.5) и
    вернуть число записанных строк. Заглушка: ничего не пишет, 0."""
    return 0
