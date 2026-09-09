"""Группы (постановка §4.8, §4.9; data-model.md §4.2.2 ``access_groups``, ``billing_groups``,
``billing_group_multipliers``; UC-09 шаги 1–2, 6, A2). Задача 001.18 — схемы и заглушка
``GroupService``; логика (CRUD, интервалы коэффициента с закрытием периода агрегации A3,
назначение ноды — 001.25/001.19) — позже."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints, field_validator

Name = Annotated[str, StringConstraints(min_length=1, max_length=100)]


class AccessGroupIn(BaseModel):
    name: Name
    description: Annotated[str, StringConstraints(max_length=1000)] = ""


def _not_null(value: object) -> object:
    """Поля NOT NULL в базе можно не передавать, но нельзя передать как ``null``."""
    if value is None:
        raise ValueError("поле не может быть null")
    return value


class AccessGroupPatch(BaseModel):
    """Частичное изменение: непереданное поле не меняется (``model_fields_set``)."""

    name: Name | None = None
    description: Annotated[str, StringConstraints(max_length=1000)] | None = None

    _reject_null = field_validator("name", "description")(_not_null)


class AccessGroup(AccessGroupIn):
    id: uuid.UUID


class BillingGroupIn(BaseModel):
    name: Name


class BillingGroupPatch(BaseModel):
    name: Name | None = None

    _reject_null = field_validator("name")(_not_null)


class MultiplierIn(BaseModel):
    """Новый интервал коэффициента (§4.9, UC-09 A2): 0.0…10.0 с шагом 0.1, действует с
    ``valid_from`` (по умолчанию — с текущего момента) до следующего интервала."""

    multiplier: Decimal = Field(ge=0, le=10)
    valid_from: dt.datetime | None = None

    @field_validator("multiplier")
    @classmethod
    def _step_of_tenth(cls, value: Decimal) -> Decimal:
        if (value * 10) % 1 != 0:
            raise ValueError("коэффициент задаётся с шагом 0.1")
        return value


class Multiplier(BaseModel):
    id: uuid.UUID
    billing_group_id: uuid.UUID
    multiplier: Decimal
    valid_from: dt.datetime
    valid_to: dt.datetime | None


class BillingGroup(BillingGroupIn):
    id: uuid.UUID
    current_multiplier: Decimal | None


STUB_ACCESS_GROUP = AccessGroup(
    id=uuid.UUID("00000000-0000-7000-8000-0000000000f1"), name="Europe", description="EU nodes"
)
STUB_BILLING_GROUP = BillingGroup(
    id=uuid.UUID("00000000-0000-7000-8000-0000000000f2"),
    name="standard",
    current_multiplier=Decimal("1.0"),
)
STUB_MULTIPLIER_ID = uuid.UUID("00000000-0000-7000-8000-0000000000f3")


class GroupService:
    """Группы поверх пула asyncpg. Заглушка 001.18: база не читается и не пишется."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def list_access(self) -> list[AccessGroup]:
        return [STUB_ACCESS_GROUP]

    async def create_access(self, data: AccessGroupIn) -> AccessGroup:
        return AccessGroup(id=STUB_ACCESS_GROUP.id, **data.model_dump())

    async def update_access(self, group_id: uuid.UUID, patch: AccessGroupPatch) -> AccessGroup:
        return STUB_ACCESS_GROUP.model_copy(
            update={"id": group_id, **patch.model_dump(exclude_unset=True)}
        )

    async def delete_access(self, group_id: uuid.UUID) -> None:
        return None

    async def list_billing(self) -> list[BillingGroup]:
        return [STUB_BILLING_GROUP]

    async def create_billing(self, data: BillingGroupIn) -> BillingGroup:
        return BillingGroup(id=STUB_BILLING_GROUP.id, current_multiplier=None, **data.model_dump())

    async def update_billing(self, group_id: uuid.UUID, patch: BillingGroupPatch) -> BillingGroup:
        return STUB_BILLING_GROUP.model_copy(
            update={"id": group_id, **patch.model_dump(exclude_unset=True)}
        )

    async def delete_billing(self, group_id: uuid.UUID) -> None:
        return None

    async def add_multiplier(self, group_id: uuid.UUID, data: MultiplierIn) -> Multiplier:
        """Новый интервал коэффициента; предыдущий закрывается ``valid_from`` нового (логика и
        UC-09 A3 — позже). Заглушка — интервал с переданным значением."""
        return Multiplier(
            id=STUB_MULTIPLIER_ID,
            billing_group_id=group_id,
            multiplier=data.multiplier,
            valid_from=data.valid_from or dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
            valid_to=None,
        )
