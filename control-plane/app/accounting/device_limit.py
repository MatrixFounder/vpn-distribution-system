"""Лимит уникальных адресов (постановка §4.13 «Механизм MVP», первый контур; data-model.md
§4.2.5 ``user_online_ips``, ``user_blocked_ips``; UC-08; R-27, Н-13).

Задача 001.33: ``DeviceLimitService`` с заглушкой. Подсчёт адресов за окно ОВ-23
(``settings.device_window``), сохранение свежих в пределах ``Device limit`` тарифа, запись
остальных в ``user_blocked_ips`` и доставка ``blocked_ips`` строкой состава на все ноды
пользователя, автоматическое снятие при вытеснении по времени активности — 001.38; применение
на ноде через ``RoutingService`` — 001.58. Лимит оценочный (§4.13: CDN без
``trusted_x_forwarded_for``, общий адрес трансляции в мобильных сетях). Превышение лимита
обнаруживается только по адресам сверх него — отчёт обязан нести все наблюдаемые адреса, а не
первые ``Device limit``.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Collection
from typing import Any

import asyncpg

Blocked = dict[uuid.UUID, list[ipaddress.IPv4Address | ipaddress.IPv6Address]]


class DeviceLimitService:
    """Лимит адресов поверх пула asyncpg. Заглушка 001.33: база не читается и не пишется."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def evaluate(
        self, conn: asyncpg.Connection, node_id: uuid.UUID, user_ids: Collection[uuid.UUID]
    ) -> Blocked:
        """Пересчитать блокируемые адреса пользователей, чьи адреса принёс отчёт: вернуть по
        пользователю адреса сверх лимита — самые старые по ``last_seen``. Один вызов на отчёт
        и на подключении приёма (отклонение от сигнатуры задачи ``evaluate(user_id)``): окно
        читается вместе со строками ``user_online_ips``, которые этот же отчёт только что
        записал и ещё не закоммитил, а пользователей в отчёте — до ``REPORT_MAX_LINES``.
        Вызывает ``AccountingService.accept_report`` (001.77), реализация — 001.38; заглушка:
        блокировать нечего."""
        return {}
