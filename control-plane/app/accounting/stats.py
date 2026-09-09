"""Статистика трафика пользователя (постановка §4.12; interfaces.md §5.1 ``/me/traffic``).

Задача 001.15 — схема ``TrafficStats`` и заглушка ``user_traffic`` с фиксированными числами
(пример §4.12: нода с коэффициентом 2.0 — передано 10 ГБ, списано 20 ГБ). Задача 001.16
заменяет заглушку агрегацией ``traffic_hourly`` за период по ноде и стране, остатком
``лимит − used_billable_bytes`` и числом активных адресов из ``user_online_ips``.

Байты — целые (``bigint`` базы); коэффициент — десятичная строка вида ``"2.0"`` (в базе кратно
0.01, §4.2.2), чтобы клиент не терял точность в float.
"""

from __future__ import annotations

import datetime as dt
import uuid

import asyncpg
from pydantic import BaseModel, Field

GIB = 1024**3


class NodeTraffic(BaseModel):
    """Строка разбивки по ноде: сырой трафик, коэффициент и списанный объём (§4.12)."""

    node_id: uuid.UUID
    name: str
    country: str = Field(description="ISO 3166-1 alpha-2 страны ноды")
    raw_uplink: int = Field(ge=0)
    raw_downlink: int = Field(ge=0)
    multiplier: str = Field(description="применённый коэффициент, десятичная строка")
    billable: int = Field(ge=0)


class CountryTraffic(BaseModel):
    """Строка разбивки по стране — суммы по нодам страны."""

    country: str
    raw_uplink: int = Field(ge=0)
    raw_downlink: int = Field(ge=0)
    billable: int = Field(ge=0)


class PeriodRef(BaseModel):
    """Период подписки, за который показана статистика."""

    id: uuid.UUID
    starts_at: dt.datetime
    ends_at: dt.datetime


class TrafficStats(BaseModel):
    """Состав §4.12 за период подписки: Raw Traffic раздельно и суммарно, Billable Traffic,
    остаток лимита (``None`` — безлимит), разбивка по ноде (с коэффициентом) и по стране, число
    активных адресов и лимит устройств (§4.13)."""

    period: PeriodRef
    raw_uplink: int = Field(ge=0)
    raw_downlink: int = Field(ge=0)
    raw_total: int = Field(ge=0)
    billable: int = Field(ge=0)
    limit: int | None = Field(default=None, ge=0, description="лимит тарифа; None — безлимит")
    remaining: int | None = Field(
        default=None, ge=0, description="лимит − billable; None — безлимит"
    )
    by_node: list[NodeTraffic]
    by_country: list[CountryTraffic]
    active_ips: int = Field(ge=0)
    device_limit: int | None = Field(default=None, ge=1, description="Device limit тарифа (§4.13)")


# Фиксированные значения заглушки (§4.12, пример с коэффициентом 2.0).
STUB_PERIOD_ID = uuid.UUID("00000000-0000-7000-8000-0000000000a1")
STUB_NODE_ID = uuid.UUID("00000000-0000-7000-8000-0000000000b1")
STUB_STATS = TrafficStats(
    period=PeriodRef(
        id=STUB_PERIOD_ID,
        starts_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        ends_at=dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
    ),
    raw_uplink=2 * GIB,
    raw_downlink=8 * GIB,
    raw_total=10 * GIB,
    billable=20 * GIB,
    limit=100 * GIB,
    remaining=80 * GIB,
    by_node=[
        NodeTraffic(
            node_id=STUB_NODE_ID,
            name="nl-1",
            country="NL",
            raw_uplink=2 * GIB,
            raw_downlink=8 * GIB,
            multiplier="2.0",
            billable=20 * GIB,
        )
    ],
    by_country=[
        CountryTraffic(country="NL", raw_uplink=2 * GIB, raw_downlink=8 * GIB, billable=20 * GIB)
    ],
    active_ips=1,
    device_limit=3,
)


async def user_traffic(
    conn: asyncpg.Connection, user_id: uuid.UUID, period_id: uuid.UUID | None
) -> TrafficStats:
    """Статистика пользователя за период (``None`` — текущий период) на подключении из пула.
    Заглушка 001.15: фиксированные числа примера §4.12 независимо от ``conn``, ``user_id`` и
    ``period_id``; агрегация ``traffic_hourly`` — 001.16."""
    return STUB_STATS
