"""Группы (постановка §4.8 «Server Groups», §4.9 «Traffic Multiplier»; data-model.md §4.2.2
``access_groups``, ``billing_groups``, ``billing_group_multipliers``, ``node_billing_assignments``;
UC-09 шаги 1–2, 6, A1…A3; R-18, R-23).

Задача 001.19 — логика поверх схемы 001.05. Два вида групп живут здесь вместе, потому что так их
видит панель, но правила у них разные: группа доступа — метка, её изменение состав тарифа не
трогает (AC UC-09), а тарифицируемая группа несёт деньги и потому датирована.

Датированность (§4.9, B-1): и коэффициент группы, и назначение ноды хранятся интервалами
``[valid_from, valid_to)``. Новый интервал не «перезаписывает» прежний — прежний закрывается тем
же моментом, которым открывается новый, поэтому в любой момент времени действует ровно один.
Непересечение держит ``EXCLUDE USING gist`` таблиц, а не проверка «прочитать и записать»: две
одновременные операции дали бы обе зелёный ответ на чтении и разошлись бы на записи. Нарушение
этого ограничения переводится в 409 (UC-09 A1 «отклонено ограничением целостности»), а не в 500.

Момент границы берётся из ``now()`` PostgreSQL внутри транзакции: в одной транзакции это
``transaction_timestamp()``, одно и то же значение во всех операторах, поэтому закрытый и
открытый интервалы стыкуются без зазора и без перекрытия. Своё время Python сюда не приходит —
часы приложения и базы разные.

Смена коэффициента закрывает текущий час учёта (UC-09 A3): накопленные значения остаются с
прежним коэффициентом, новые идут в строку с новым. Закрытие делается на подключении и в
транзакции самой смены — ``AccountingService.close_hour(conn, node_id, at)`` принимает
подключение вызывающего именно для этого (отклонение от постановки объявлено в 001.33);
реализацию закрытия вводит 001.77, сейчас это заглушка.

Саму службу учёта домен не импортирует: правило слоёв (``accounting/.AGENTS.md``) — учёт зависит
от домена, обратного ребра нет, иначе замкнулся бы цикл ``accounting.quota → domain.composition
→ accounting``. Нужен один метод, и он описан здесь протоколом ``HourCloser``; настоящую службу
передаёт вызывающий (маршрут панели). Значения по умолчанию у параметра нет намеренно: служба,
собранная без закрытия часа, молча теряла бы A3, а это ровно тот зелёный «успех», которого не
было.

Audit Log (§4.16, UC-09 шаг 7) требует записывать прежнее и новое значение коэффициента.
Писать некуда: ``app/security/audit.py::record`` вводит 001.46, и 001.19 от неё не зависит.
Обе операции истории возвращают прежнее и новое значение вызывающему, чтобы маршруту было что
передать в журнал, когда заглушка появится (передано в примечания 001.46).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any, NamedTuple, Protocol

import asyncpg
from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.db.pool import transaction
from app.domain.multiplier import MULTIPLIER_MILLI_MAX, MULTIPLIER_STEP_MILLI
from app.errors import ApiError

Name = Annotated[str, StringConstraints(min_length=1, max_length=100)]
# Коэффициент показывается и принимается как десятичная дробь, хранится в тысячных (§4.2.2).
MILLI_PER_UNIT = 1000


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


class Assignment(NamedTuple):
    """Итог назначения ноды тарифицируемой группе: что действовало и что стало. Прежние значения
    нужны Audit Log (§4.16 требует ``old_value``), а получить их после записи уже нельзя."""

    node_id: uuid.UUID
    billing_group_id: uuid.UUID
    override_milli: int | None
    previous_billing_group_id: uuid.UUID | None
    previous_override_milli: int | None
    valid_from: dt.datetime


def validate_multiplier(milli: int) -> int:
    """Коэффициент в тысячных: 0…10000 кратно 100 (§4.9 «0.0–10.0 с шагом 0.1»; UC-09 A2).
    Те же границы стоят ``CHECK`` на четырёх колонках §4.2.2 и §4.2.5 — здесь они проверяются
    раньше, чтобы ответом было 422 с причиной, а не 500 от нарушения ``CHECK``: значение
    приходит из запроса администратора (переопределение ноды), а не из кода."""
    if not 0 <= milli <= MULTIPLIER_MILLI_MAX:
        raise ApiError(
            "invalid_multiplier",
            f"коэффициент вне диапазона 0…{MULTIPLIER_MILLI_MAX} тысячных",
            status=422,
            details={"multiplier_milli": milli},
        )
    if milli % MULTIPLIER_STEP_MILLI:
        raise ApiError(
            "invalid_multiplier",
            f"коэффициент не кратен шагу {MULTIPLIER_STEP_MILLI} тысячных",
            status=422,
            details={"multiplier_milli": milli},
        )
    return milli


def to_milli(value: Decimal) -> int:
    """Десятичная дробь → тысячные с проверкой: ``2.0`` → ``2000``. Через целое, а не через
    ``float``: §4.9 запрещает плавающую точку в списании, и вход тоже разбирается точно."""
    scaled = value.scaleb(3)
    if scaled != scaled.to_integral_value():
        raise ApiError(
            "invalid_multiplier",
            "коэффициент задаётся с шагом 0.1",
            status=422,
            details={"multiplier": str(value)},
        )
    return validate_multiplier(int(scaled))


def from_milli(milli: int) -> Decimal:
    """Тысячные → десятичная дробь для ответа: ``2000`` → ``2.0`` (один знак после запятой —
    шаг 0.1)."""
    return (Decimal(milli) / MILLI_PER_UNIT).quantize(Decimal("0.1"))


def _conflict(message: str) -> ApiError:
    return ApiError("conflict", message, status=409)


def _not_found(what: str) -> ApiError:
    return ApiError("not_found", f"{what} не найдена", status=404)


class HourCloser(Protocol):
    """Что домену нужно от учёта: закрыть накопление часа ноды на подключении вызывающего.
    Протокол, а не импорт службы, — по правилу слоёв (см. докстринг модуля)."""

    async def close_hour(
        self, conn: asyncpg.Connection, node_id: uuid.UUID, at: dt.datetime
    ) -> None: ...


class GroupService:
    """Группы поверх пула asyncpg (001.19). ``accounting`` передаётся вызывающим и обязателен:
    операции истории закрывают им час учёта (UC-09 A3)."""

    def __init__(self, pool: Any, accounting: HourCloser) -> None:
        self._pool = pool
        self._accounting = accounting

    # --- группы доступа: метка, тарифы не трогает ---------------------------------------------

    async def list_access(self) -> list[AccessGroup]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("select id, name, description from access_groups order by name")
        return [AccessGroup(**dict(row)) for row in rows]

    async def create_access(self, data: AccessGroupIn) -> AccessGroup:
        """Создать группу доступа (UC-09 шаг 2). Имя уникально — занятое даёт 409 от индекса,
        а не от чтения перед записью: два одновременных создания одного имени иначе дали бы два
        успеха."""
        async with self._pool.acquire() as conn:
            group_id = await conn.fetchval(
                "insert into access_groups (name, description) values ($1, $2) "
                "on conflict (name) do nothing returning id",
                data.name,
                data.description,
            )
        if group_id is None:
            raise _conflict("группа доступа с таким именем уже есть")
        return AccessGroup(id=group_id, **data.model_dump())

    async def update_access(self, group_id: uuid.UUID, patch: AccessGroupPatch) -> AccessGroup:
        changes = patch.model_dump(exclude_unset=True)
        async with self._pool.acquire() as conn:
            if changes:
                sets = ", ".join(f"{name} = ${i}" for i, name in enumerate(changes, start=2))
                try:
                    await conn.execute(
                        f"update access_groups set {sets} where id = $1",  # noqa: S608 — имена
                        group_id,  # колонок из ключей модели, не из запроса
                        *changes.values(),
                    )
                except asyncpg.UniqueViolationError as exc:
                    raise _conflict("группа доступа с таким именем уже есть") from exc
            row = await conn.fetchrow(
                "select id, name, description from access_groups where id = $1", group_id
            )
        if row is None:
            raise _not_found("группа доступа")
        return AccessGroup(**dict(row))

    async def delete_access(self, group_id: uuid.UUID) -> None:
        """Удалить группу доступа. Связи с тарифами и нодами уносит ``ON DELETE CASCADE``
        (§4.2.2): группа перестаёт существовать, а не остаётся висеть в составе тарифа."""
        async with self._pool.acquire() as conn:
            deleted = await conn.execute("delete from access_groups where id = $1", group_id)
        if deleted.endswith(" 0"):
            raise _not_found("группа доступа")

    # --- тарифицируемые группы: несут деньги, датированы ---------------------------------------

    async def list_billing(self) -> list[BillingGroup]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "select g.id, g.name, m.multiplier_milli from billing_groups g "
                "left join billing_group_multipliers m on m.billing_group_id = g.id "
                "and m.valid_from <= now() and (m.valid_to is null or m.valid_to > now()) "
                "order by g.name"
            )
        return [
            BillingGroup(
                id=row["id"],
                name=row["name"],
                current_multiplier=(
                    None if row["multiplier_milli"] is None else from_milli(row["multiplier_milli"])
                ),
            )
            for row in rows
        ]

    async def create_billing(self, data: BillingGroupIn) -> BillingGroup:
        """Создать тарифицируемую группу (UC-09 шаг 1). Коэффициент не задаётся здесь: он
        датирован и заводится отдельным интервалом, поэтому новая группа — без действующего
        значения, и до первого интервала нода берёт 1.0 с уровня ``default`` (§4.9)."""
        async with self._pool.acquire() as conn:
            group_id = await conn.fetchval(
                "insert into billing_groups (name) values ($1) "
                "on conflict (name) do nothing returning id",
                data.name,
            )
        if group_id is None:
            raise _conflict("тарифицируемая группа с таким именем уже есть")
        return BillingGroup(id=group_id, name=data.name, current_multiplier=None)

    async def update_billing(self, group_id: uuid.UUID, patch: BillingGroupPatch) -> BillingGroup:
        changes = patch.model_dump(exclude_unset=True)
        async with self._pool.acquire() as conn:
            if changes:
                sets = ", ".join(f"{name} = ${i}" for i, name in enumerate(changes, start=2))
                try:
                    await conn.execute(
                        f"update billing_groups set {sets} where id = $1",  # noqa: S608 — имена
                        group_id,  # колонок из ключей модели, не из запроса
                        *changes.values(),
                    )
                except asyncpg.UniqueViolationError as exc:
                    raise _conflict("тарифицируемая группа с таким именем уже есть") from exc
            row = await conn.fetchrow(
                "select g.id, g.name, m.multiplier_milli from billing_groups g "
                "left join billing_group_multipliers m on m.billing_group_id = g.id "
                "and m.valid_from <= now() and (m.valid_to is null or m.valid_to > now()) "
                "where g.id = $1",
                group_id,
            )
        if row is None:
            raise _not_found("тарифицируемая группа")
        return BillingGroup(
            id=row["id"],
            name=row["name"],
            current_multiplier=(
                None if row["multiplier_milli"] is None else from_milli(row["multiplier_milli"])
            ),
        )

    async def delete_billing(self, group_id: uuid.UUID) -> None:
        """Удалить тарифицируемую группу. Группа, назначенная хоть одной ноде, не удаляется:
        ``nodes.billing_group_id`` — ``NOT NULL`` без каскада (R-18), и удаление оставило бы ноду
        без группы. Отказ — 409, а не 500 от внешнего ключа."""
        async with self._pool.acquire() as conn:
            try:
                deleted = await conn.execute("delete from billing_groups where id = $1", group_id)
            except asyncpg.ForeignKeyViolationError as exc:
                raise _conflict("группа назначена ноде: сначала перенесите ноды") from exc
        if deleted.endswith(" 0"):
            raise _not_found("тарифицируемая группа")

    # --- история коэффициента и назначения -----------------------------------------------------

    async def add_multiplier(self, group_id: uuid.UUID, data: MultiplierIn) -> Multiplier:
        """Маршрут панели: десятичный коэффициент запроса → тысячные хранения (§4.2.2)."""
        return await self.set_group_multiplier(
            group_id, to_milli(data.multiplier), valid_from=data.valid_from
        )

    async def set_group_multiplier(
        self, group_id: uuid.UUID, milli: int, *, valid_from: dt.datetime | None = None
    ) -> Multiplier:
        """Новый интервал коэффициента группы (§4.9; UC-09 шаг 1, A2, A3): действующий интервал
        закрывается моментом, которым открывается новый, поэтому история не рвётся и не
        перекрывается. Для каждой ноды группы, которая берёт коэффициент отсюда (то есть без
        собственного переопределения), час учёта закрывается в этой же транзакции.

        ``valid_from`` в прошлом, попавшее в уже закрытый интервал, отклоняется ограничением
        ``EXCLUDE`` — 409: задним числом списания не переписываются (§4.9 «Датированность»)."""
        validate_multiplier(milli)
        async with transaction(self._pool) as conn:
            at = await self._boundary(conn, valid_from)
            if not await conn.fetchval("select true from billing_groups where id = $1", group_id):
                raise _not_found("тарифицируемая группа")
            await conn.execute(
                "update billing_group_multipliers set valid_to = $2 "
                "where billing_group_id = $1 and valid_to is null",
                group_id,
                at,
            )
            try:
                row = await conn.fetchrow(
                    "insert into billing_group_multipliers "
                    "(billing_group_id, multiplier_milli, valid_from) values ($1, $2, $3) "
                    "returning id, billing_group_id, multiplier_milli, valid_from, valid_to",
                    group_id,
                    milli,
                    at,
                )
            except asyncpg.ExclusionViolationError as exc:
                raise _conflict("интервал коэффициента пересекается с уже записанным") from exc
            await self._close_hours(conn, group_id, at)
        return Multiplier(
            id=row["id"],
            billing_group_id=row["billing_group_id"],
            multiplier=from_milli(row["multiplier_milli"]),
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
        )

    async def set_billing_group(
        self,
        node_id: uuid.UUID,
        group_id: uuid.UUID,
        override_milli: int | None = None,
        *,
        valid_from: dt.datetime | None = None,
    ) -> Assignment:
        """Назначить ноде тарифицируемую группу и, необязательно, собственный коэффициент
        (UC-09 шаг 6). Действующее назначение закрывается, открывается новое, денормализация
        ``nodes.billing_group_id``/``nodes.multiplier_milli`` приводится к нему, час учёта
        закрывается — всё в одной транзакции.

        Ровно одна группа на ноду (R-18) держится не проверкой, а ``EXCLUDE`` по
        ``(node_id, [valid_from, valid_to))``: две одновременные попытки назначить разные группы
        обе прошли бы чтение, но записать смогла бы одна — вторая получает 409 (UC-09 A1), и её
        транзакция откатывается целиком, поэтому ``nodes.billing_group_id`` остаётся прежним.

        Строка ноды намеренно не блокируется ``FOR UPDATE``: блокировка выстроила бы
        одновременные назначения в очередь и превратила бы их в «последний выиграл» — вторая
        группа записалась бы поверх первой без единого отказа, а A1 требует отклонения. Арбитром
        остаётся ограничение целостности; последовательная смена группы (штатный перенос ноды)
        при этом проходит, потому что действующий интервал к тому моменту уже закрыт."""
        if override_milli is not None:
            validate_multiplier(override_milli)
        async with transaction(self._pool) as conn:
            at = await self._boundary(conn, valid_from)
            previous = await conn.fetchrow(
                "select billing_group_id, multiplier_milli from nodes where id = $1", node_id
            )
            if previous is None:
                raise _not_found("нода")
            if not await conn.fetchval("select true from billing_groups where id = $1", group_id):
                raise _not_found("тарифицируемая группа")
            await conn.execute(
                "update node_billing_assignments set valid_to = $2 "
                "where node_id = $1 and valid_to is null",
                node_id,
                at,
            )
            try:
                await conn.execute(
                    "insert into node_billing_assignments "
                    "(node_id, billing_group_id, multiplier_override_milli, valid_from) "
                    "values ($1, $2, $3, $4)",
                    node_id,
                    group_id,
                    override_milli,
                    at,
                )
            except asyncpg.ExclusionViolationError as exc:
                raise _conflict("у ноды уже есть действующая тарифицируемая группа") from exc
            await conn.execute(
                "update nodes set billing_group_id = $2, multiplier_milli = $3 where id = $1",
                node_id,
                group_id,
                override_milli,
            )
            await self._accounting.close_hour(conn, node_id, at)
        return Assignment(
            node_id=node_id,
            billing_group_id=group_id,
            override_milli=override_milli,
            previous_billing_group_id=previous["billing_group_id"],
            previous_override_milli=previous["multiplier_milli"],
            valid_from=at,
        )

    @staticmethod
    async def _boundary(conn: asyncpg.Connection, valid_from: dt.datetime | None) -> dt.datetime:
        """Граница интервалов: переданный момент или ``now()`` транзакции. Значение берётся из
        базы один раз и подставляется в оба оператора — закрытый и открытый интервалы обязаны
        стыковаться ровно, а часы приложения и базы расходятся."""
        at: dt.datetime = await conn.fetchval("select coalesce($1::timestamptz, now())", valid_from)
        return at

    async def _close_hours(
        self, conn: asyncpg.Connection, group_id: uuid.UUID, at: dt.datetime
    ) -> None:
        """Закрыть час учёта у нод, чей коэффициент задаёт эта группа (UC-09 A3). Нода с
        собственным переопределением сменой группового значения не затронута — её коэффициент
        берётся с уровня ``node`` (§4.9), и закрывать ей час не за что."""
        rows = await conn.fetch(
            "select id from nodes where billing_group_id = $1 and multiplier_milli is null",
            group_id,
        )
        for row in rows:
            await self._accounting.close_hour(conn, row["id"], at)
