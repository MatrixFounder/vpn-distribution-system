"""Webhook уведомлений (задача 001.51 — интерфейс и заглушка; доставка — 001.79).

``WebhookSender.send(url, event)`` доставляет событие §4.17 получателю ``settings.webhooks``
(элемент ``{url, secret}``, сид — 001.79) запросом HTTPS POST: тело подписывается HMAC-SHA256
секретом получателя (security.md §1, секрет в ``settings``), запрос несёт идентификатор события —
получатель различает повторную доставку (§4.17). Подпись, заголовок ``X-Event-Id``, повторы с
экспоненциальной задержкой, строки ``webhook_deliveries`` и запрет частных адресов (SSRF) — 001.79.

Заглушка отказывает ``NotImplementedError``: «успешная» доставка, которой не было, засчиталась бы
за отправленное уведомление.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class WebhookSender:
    """Доставка события получателю webhook (001.79)."""

    async def send(self, url: str, event: Mapping[str, Any]) -> None:
        """Доставить событие ``event`` по адресу ``url``."""
        raise NotImplementedError("WebhookSender.send: заглушка 001.51, доставка — 001.79")
