"""Лимиты трафика и пороги (постановка §4.11; UC-05; R-24, Н-14).

Задача 001.33: ``LimitsService`` с заглушкой. Сравнение расхода периода с лимитом, однократные
за период уведомления порогов 80 и 95 % (события ``traffic.80``, ``traffic.95`` — 001.52),
100 % → ``suspended_quota`` и отзыв через поток состава в бюджет §5.4, ``Unlimited`` без
порогов, восстановление при продлении и бонусе — 001.35 (пороги — из ``settings``).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any

import asyncpg


class LimitsService:
    """Лимиты периода подписки поверх пула asyncpg. Заглушка 001.33: база не читается."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def check(self, conn: asyncpg.Connection, user_ids: Collection[uuid.UUID]) -> None:
        """Сверить расход текущего периода с лимитом тарифа (§4.11) у пользователей, которых
        коснулся принятый отчёт. Один вызов на отчёт и на подключении обработчика (отклонение
        от сигнатуры задачи ``check(user_id)``): задача ``limits.check`` критичной очереди
        ставится приёмом отчёта одна на отчёт с полезной нагрузкой ``{"node_id", "user_ids"}``
        и ключом ``limits:{node_id}:{counter_epoch}:{report_seq}`` — по задаче на пользователя
        при ``REPORT_MAX_LINES`` строк критичная очередь получала бы тысячи задач в минуту
        (бюджет ожидания — половина Н-13, §5.4). Обработчик — ``jobs/handlers/limits.py``;
        заглушка ничего не делает, логика — 001.35."""
