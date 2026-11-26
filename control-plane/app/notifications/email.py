"""Почта уведомлений и служебных писем (задача 001.51 — интерфейс и заглушка; доставка — 001.78).

``EmailSender.send(to, template, lang, ctx)`` отправляет письмо по шаблону на языке получателя и
возвращает идентификатор строки ``email_deliveries`` (``DeliveryId``). Шаблоны — файлы
``templates/{ru,en}/{type}.txt`` (Jinja2) для семи пользовательских событий §4.17: у
операторских событий почты нет (§4.17, канал — webhook и алерты §17.4). Тексты шаблонов,
``template_for``, SMTP-реле, повторы и пометка недоставляемого адреса
``users.email_undeliverable_at`` — 001.78.

Заглушка отказывает ``NotImplementedError``: «успешная» отправка письма, которого не было,
засчиталась бы за доставленное уведомление.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NewType

DeliveryId = NewType("DeliveryId", uuid.UUID)  # email_deliveries.id
TEMPLATES = Path(__file__).with_name("templates")
LANGUAGES = ("ru", "en")


class EmailSender:
    """Отправка письма по шаблону (001.78)."""

    async def send(self, to: str, template: str, lang: str, ctx: Mapping[str, Any]) -> DeliveryId:
        """Отправить письмо ``template`` на языке ``lang`` адресату ``to``."""
        raise NotImplementedError("EmailSender.send: заглушка 001.51, доставка почты — 001.78")
