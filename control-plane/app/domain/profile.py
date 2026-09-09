"""Профиль пользователя кабинета (постановка §4.2, §17.8; UC-14, UC-16): язык, часовой пояс,
согласие на канал объявлений, удаление аккаунта.

Задача 001.15 — схема ``Profile`` и заглушка ``ProfileService`` с фиксированными значениями
(``STUB_PROFILE``): ``get`` и ``update`` возвращают одну и ту же запись под идентификатором
вызывающего, ``delete_account`` ничего не делает. Логика: ``update`` — 001.16 (язык из
``{ru, en}``, пояс IANA, согласие), ``delete_account`` — 001.17 (UC-14: аннулирование токена,
отзыв credentials, стирание PII с сохранением ``user_id``-суррогата).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

Language = Literal["en", "ru"]


class Profile(BaseModel):
    """Профиль, каким его видит кабинет (``users`` без секретов)."""

    id: uuid.UUID
    email: str
    language: Language
    timezone: str = Field(description="часовой пояс IANA (R-52)")
    announce_consent: bool
    aup_version: str
    created_at: dt.datetime


STUB_EMAIL = "user@example.com"
STUB_PROFILE = Profile(
    id=uuid.UUID("00000000-0000-7000-8000-0000000000e1"),
    email=STUB_EMAIL,
    language="en",
    timezone="UTC",
    announce_consent=False,
    aup_version="2026-09",
    created_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
)


class ProfileService:
    """Профиль поверх пула asyncpg. Заглушка 001.15: база не читается и не пишется."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def get(self, user_id: uuid.UUID) -> Profile:
        """Профиль пользователя; заглушка — ``STUB_PROFILE`` под ``user_id`` вызывающего."""
        return STUB_PROFILE.model_copy(update={"id": user_id})

    async def update(
        self,
        user_id: uuid.UUID,
        *,
        language: Language | None = None,
        timezone: str | None = None,
        announce_consent: bool | None = None,
    ) -> Profile:
        """Изменить язык, пояс и согласие (переданные поля); вернуть профиль. Заглушка —
        ``STUB_PROFILE`` с подставленными значениями, без записи в базу (логика — 001.16)."""
        changes: dict[str, Any] = {"id": user_id}
        if language is not None:
            changes["language"] = language
        if timezone is not None:
            changes["timezone"] = timezone
        if announce_consent is not None:
            changes["announce_consent"] = announce_consent
        return STUB_PROFILE.model_copy(update=changes)

    async def delete_account(self, user_id: uuid.UUID) -> None:
        """Удалить аккаунт (UC-14). Заглушка — ничего не делает; логика — 001.17."""
        return None
