"""Тарифы (постановка §4.10; data-model.md §4.2.2 ``plans``, ``plan_protocols``,
``plan_access_groups``; UC-09 шаг 3, A4; R-30).

Задача 001.19 — логика поверх схемы 001.05. Состав тарифа (группы доступа и профили) — две
таблицы связи, и он заменяется целиком, а не дополняется: ``PATCH`` без ключа состав не трогает,
``PATCH`` с ключом делает состав равным переданному. «Добавить одну группу» отдельной операцией
не вводится — панель присылает список.

Удаление (UC-09 A4): тариф, на который ссылается хоть один период подписки, не удаляется —
переводится в ``archived``. Ссылка из ``subscription_periods.plan_id`` — внешний ключ без
каскада, и удаление такого тарифа всё равно отказало бы ошибкой базы; сверх этого статистика
§4.12 и §5.10 называет тариф периода, и тариф, удалённый из-под закрытого периода, оставил бы её
без имени. Поэтому «активные подписки» понимаются шире буквы A4 — любой период, а не только
действующий; расширение объявлено в описании задачи. Неиспользованный тариф удаляется.

Цена справочная (О-2): подписка выдаётся Redeem-кодом, оплата не принимается, поэтому цена ничего
не решает и валидируется только формой.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Annotated, Any, Literal

import asyncpg
from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.db.pool import transaction
from app.errors import ApiError

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


# Колонки самой таблицы ``plans``; состав (группы, профили) живёт в таблицах связи.
_COLUMNS = (
    "name",
    "price_amount",
    "price_currency",
    "duration_days",
    "traffic_limit_bytes",
    "device_limit",
)
_SELECT = (
    "select p.id, p.name, p.price_amount, p.price_currency, p.duration_days, "
    "p.traffic_limit_bytes, p.device_limit, p.status, p.created_at, p.updated_at, "
    "coalesce((select array_agg(g.access_group_id order by g.access_group_id) "
    "from plan_access_groups g where g.plan_id = p.id), '{}') as access_group_ids, "
    "coalesce((select array_agg(t.profile::text order by t.profile) "
    "from plan_protocols t where t.plan_id = p.id), '{}') as profiles from plans p"
)


def _plan(row: asyncpg.Record) -> Plan:
    return Plan(**dict(row))


class PlanService:
    """Тарифы поверх пула asyncpg (001.19)."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def list(self) -> list[Plan]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(f"{_SELECT} order by p.name")
        return [_plan(row) for row in rows]

    async def create(self, data: PlanIn) -> Plan:
        """Создать тариф (UC-09 шаг 3) вместе с составом. Имя уникально — занятое даёт 409 от
        индекса, а не от чтения перед записью. Несуществующая группа доступа — 422 от внешнего
        ключа: состав тарифа ссылается на группы, а не заводит их."""
        async with transaction(self._pool) as conn:
            plan_id = await conn.fetchval(
                "insert into plans (name, price_amount, price_currency, duration_days, "
                "traffic_limit_bytes, device_limit) values ($1, $2, $3, $4, $5, $6) "
                "on conflict (name) do nothing returning id",
                *(getattr(data, name) for name in _COLUMNS),
            )
            if plan_id is None:
                raise ApiError("conflict", "тариф с таким именем уже есть", status=409)
            await self._set_composition(conn, plan_id, data.access_group_ids, data.profiles)
            row = await conn.fetchrow(f"{_SELECT} where p.id = $1", plan_id)
        return _plan(row)

    async def update(self, plan_id: uuid.UUID, patch: PlanPatch) -> Plan:
        """Изменить переданные поля (в том числе сброс в ``null``). Переданный состав заменяет
        прежний целиком; непереданный не трогается."""
        changes = patch.model_dump(exclude_unset=True)
        groups = changes.pop("access_group_ids", None)
        profiles = changes.pop("profiles", None)
        async with transaction(self._pool) as conn:
            if not await conn.fetchval("select true from plans where id = $1 for update", plan_id):
                raise ApiError("not_found", "тариф не найден", status=404)
            if changes:
                sets = ", ".join(f"{name} = ${i}" for i, name in enumerate(changes, start=2))
                try:
                    await conn.execute(
                        f"update plans set {sets}, updated_at = now() where id = $1",  # noqa: S608
                        plan_id,  # имена колонок — ключи модели, не значения запроса
                        *changes.values(),
                    )
                except asyncpg.UniqueViolationError as exc:
                    raise ApiError("conflict", "тариф с таким именем уже есть", status=409) from exc
            if groups is not None or profiles is not None:
                await self._set_composition(conn, plan_id, groups, profiles)
                if not changes:  # состав тоже изменение тарифа
                    await conn.execute("update plans set updated_at = now() where id = $1", plan_id)
            row = await conn.fetchrow(f"{_SELECT} where p.id = $1", plan_id)
        return _plan(row)

    async def archive(self, plan_id: uuid.UUID) -> None:
        """Удалить тариф; тариф, на который хоть что-то ссылается, вместо этого переводится в
        ``archived`` (UC-09 A4). Существующие подписки продолжают действовать: архивный статус
        закрывает выдачу новых, а не отбирает выданное. Ответ один и тот же (204): панель просит
        убрать тариф из выдачи, и он убран — каким способом, зависит от того, назван ли он
        историей."""
        async with transaction(self._pool) as conn:
            if not await conn.fetchval("select true from plans where id = $1 for update", plan_id):
                raise ApiError("not_found", "тариф не найден", status=404)
            try:
                # Используется ли тариф, решает сама база: на него ссылаются периоды подписки
                # (§4.2.4), коды с ограничением тарифом и заказы, и перечислять эти таблицы в
                # коде значило бы заводить второй список внешних ключей, который разойдётся с
                # первым. Вложенная транзакция — это SAVEPOINT: нарушение внешнего ключа
                # прерывает транзакцию целиком, и без точки сохранения следующий оператор упал
                # бы «transaction is aborted», а не заархивировал тариф.
                async with conn.transaction():
                    await conn.execute("delete from plans where id = $1", plan_id)
            except asyncpg.ForeignKeyViolationError:
                await conn.execute(
                    "update plans set status = 'archived', updated_at = now() where id = $1",
                    plan_id,
                )

    @staticmethod
    async def _set_composition(
        conn: asyncpg.Connection,
        plan_id: uuid.UUID,
        groups: Sequence[uuid.UUID] | None,
        profiles: Sequence[str] | None,
    ) -> None:
        """Заменить состав тарифа: связи удаляются и записываются заново. Повтор в переданном
        списке не ошибка запроса — первичный ключ таблицы связи его и так схлопывает, поэтому
        список приводится к множеству до вставки."""
        if groups is not None:
            await conn.execute("delete from plan_access_groups where plan_id = $1", plan_id)
            try:
                await conn.executemany(
                    "insert into plan_access_groups (plan_id, access_group_id) values ($1, $2)",
                    [(plan_id, group_id) for group_id in dict.fromkeys(groups)],
                )
            except asyncpg.ForeignKeyViolationError as exc:
                raise ApiError(
                    "unknown_access_group", "группа доступа не найдена", status=422
                ) from exc
        if profiles is not None:
            await conn.execute("delete from plan_protocols where plan_id = $1", plan_id)
            await conn.executemany(
                "insert into plan_protocols (plan_id, profile) values ($1, $2)",
                [(plan_id, profile) for profile in dict.fromkeys(profiles)],
            )
