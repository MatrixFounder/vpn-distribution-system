"""Выбор языка ответа (R-51, задача 001.65). Заглушка 001.10: всегда ``en``."""

from __future__ import annotations

from typing import Any

SUPPORTED_LANGUAGES = ("en", "ru")


def negotiate(user: Any | None, accept_language: str | None) -> str:
    """Язык ответа: настройка пользователя, иначе ``Accept-Language``, иначе ``en`` (001.65).
    До 001.65 — всегда ``en``."""
    return "en"
