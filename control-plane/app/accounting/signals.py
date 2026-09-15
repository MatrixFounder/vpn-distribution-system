"""Признаки перепродажи доступа (постановка §4.13, третий контур — наблюдение; R-29,
приоритет S).

Задача 001.33: схема признаков и ``resale_signals`` с фиксированными значениями. Объём из
``traffic_hourly``, частота новых соединений из счётчиков агента (001.58, conntrack — поле
``conn_stats[]`` отчёта добавит 001.39), доля активных часов — 001.39; в карточку пользователя
панели — 001.50. Адреса назначения не собираются (§16.3). Решение принимает администратор.
"""

from __future__ import annotations

import uuid

import asyncpg
from pydantic import BaseModel, ConfigDict, Field


class Signals(BaseModel):
    """Три признака §4.13 за окно наблюдения: объём в гигабайтах (10⁹ байт — единица имени
    поля; ГиБ-представление ``stats.GIB`` здесь не используется, согласование в карточке —
    001.39), новых соединений в час, доля часов с активностью (круглосуточность). Модель
    неизменяемая: заглушка отдаёт один экземпляр всем вызывающим."""

    model_config = ConfigDict(frozen=True)

    volume_gb: float = Field(ge=0)
    new_conn_rate: float = Field(ge=0, description="новых соединений в час")
    active_hours_share: float = Field(ge=0, le=1, description="доля часов окна с трафиком")


STUB_SIGNALS = Signals(volume_gb=12.5, new_conn_rate=3.0, active_hours_share=0.25)


# Окно признаков: неделя по умолчанию (§4.13), не дольше квартала — иначе маршрут панели,
# передавший ``days`` как есть, читал бы ``traffic_hourly`` без верхней границы (001.39).
SIGNAL_WINDOW_MAX_DAYS = 90


async def resale_signals(conn: asyncpg.Connection, user_id: uuid.UUID, days: int = 7) -> Signals:
    """Признаки пользователя за ``days`` последних суток (1…``SIGNAL_WINDOW_MAX_DAYS``).
    Заглушка: фиксированные значения независимо от ``conn`` и ``user_id``; граница окна —
    настоящая."""
    if not 1 <= days <= SIGNAL_WINDOW_MAX_DAYS:
        raise ValueError(f"окно признаков — от 1 до {SIGNAL_WINDOW_MAX_DAYS} суток")
    return STUB_SIGNALS
