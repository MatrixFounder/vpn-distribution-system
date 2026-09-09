"""Тарифы (постановка §4.10; data-model.md §4.2.2 ``plans``, ``plan_protocols``,
``plan_access_groups``; UC-09 шаг 3, A4). Задача 001.18 — схемы и заглушка ``PlanService``
с фиксированными значениями; логика (CRUD в базе, архивирование вместо удаления при активных
подписках, аудит) — 001.19."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

InboundProfile = Literal["vless_raw_vision", "vless_xhttp", "trojan_reality"]
PlanStatus = Literal["active", "archived"]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]  # ISO 4217, ОВ-19


class PlanIn(BaseModel):
    """Поля тарифа §4.10: имя, справочная цена (О-2), срок, лимит трафика (``None`` —
    Unlimited), лимит устройств, группы доступа, профили."""

    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    price_amount: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    price_currency: Currency | None = None
    duration_days: int = Field(gt=0)
    traffic_limit_bytes: int | None = Field(default=None, ge=0)
    device_limit: int | None = Field(default=None, ge=1)
    access_group_ids: list[uuid.UUID] = Field(default_factory=list)
    profiles: list[InboundProfile] = Field(min_length=1)


class PlanPatch(BaseModel):
    """Частичное изменение тарифа: любое подмножество полей ``PlanIn``. Поле, переданное как
    ``null``, сбрасывается (``traffic_limit_bytes: null`` — Unlimited, цена без значения);
    непереданное поле не меняется (``model_fields_set``)."""

    name: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None = None
    price_amount: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    price_currency: Currency | None = None
    duration_days: int | None = Field(default=None, gt=0)
    traffic_limit_bytes: int | None = Field(default=None, ge=0)
    device_limit: int | None = Field(default=None, ge=1)
    access_group_ids: list[uuid.UUID] | None = None
    profiles: list[InboundProfile] | None = Field(default=None, min_length=1)

    @field_validator("name", "duration_days", "access_group_ids", "profiles")
    @classmethod
    def _not_null(cls, value: object) -> object:
        """Поля NOT NULL в базе можно не передавать, но нельзя передать как ``null``."""
        if value is None:
            raise ValueError("поле не может быть null")
        return value


class Plan(PlanIn):
    id: uuid.UUID
    status: PlanStatus
    created_at: dt.datetime
    updated_at: dt.datetime


STUB_PLAN_ID = uuid.UUID("00000000-0000-7000-8000-0000000000c1")
STUB_ACCESS_GROUP_ID = uuid.UUID("00000000-0000-7000-8000-0000000000f1")
STUB_PLAN = Plan(
    id=STUB_PLAN_ID,
    name="Standard 100 GB",
    price_amount=Decimal("9.99"),
    price_currency="EUR",
    duration_days=30,
    traffic_limit_bytes=100 * 1024**3,
    device_limit=3,
    access_group_ids=[STUB_ACCESS_GROUP_ID],
    profiles=["vless_raw_vision", "trojan_reality"],
    status="active",
    created_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
    updated_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
)


class PlanService:
    """Тарифы поверх пула asyncpg. Заглушка 001.18: база не читается и не пишется."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def list(self) -> list[Plan]:
        return [STUB_PLAN]

    async def create(self, data: PlanIn) -> Plan:
        """Создать тариф; заглушка — переданные поля под фиксированным идентификатором."""
        return Plan(
            **data.model_dump(),
            id=STUB_PLAN_ID,
            status="active",
            created_at=STUB_PLAN.created_at,
            updated_at=STUB_PLAN.updated_at,
        )

    async def update(self, plan_id: uuid.UUID, patch: PlanPatch) -> Plan:
        """Изменить переданные поля (в том числе сброс в ``null``); заглушка — `STUB_PLAN` с
        подстановкой под ``plan_id``."""
        changes = {"id": plan_id, **patch.model_dump(exclude_unset=True)}
        return STUB_PLAN.model_copy(update=changes)

    async def archive(self, plan_id: uuid.UUID) -> None:
        """Удаление тарифа: при активных подписках — архивный статус (UC-09 A4); заглушка."""
        return None
