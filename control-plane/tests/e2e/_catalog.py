"""Помощники сквозных тестов каталога (001.19): подключение для подготовки и сверки плюс уборка
всего, что тест завёл. Каталог живёт в общей базе стенда рядом с рабочими данными, поэтому
каждая строка теста заводится под префиксом ``PREFIX`` и по нему же удаляется — по имени, а не
по списку идентификаторов: тест, упавший посреди сценария, иначе оставил бы мусор, на который
споткнулся бы следующий прогон (имена тарифов и групп уникальны).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from app.domain.composition import CompositionService

PREFIX = "t19-"


def unique_name(label: str) -> str:
    """Уникальное имя под префиксом уборки: «t19-<метка>-<восемь знаков>»."""
    return f"{PREFIX}{label}-{uuid.uuid4().hex[:8]}"


async def cleanup(conn: asyncpg.Connection, owner: asyncpg.Connection | None = None) -> None:
    """Удалить всё, что тесты каталога завели под префиксом, в порядке внешних ключей.
    ``owner`` — подключение владельца для журнала баланса (см. ниже)."""
    like = f"{PREFIX}%"
    # Всё, что ссылается на ноду без каскада (001.25: токены, identity, история адресов;
    # группы доступа ноды уходят каскадом), — до самой ноды.
    for table in (
        "bootstrap_tokens",
        "node_identities",
        "node_ip_history",
        "node_billing_assignments",
    ):
        await conn.execute(
            f"delete from {table} where node_id in (select id from nodes where code like $1)",  # noqa: S608 — имя из списка
            like,
        )
    await conn.execute("delete from nodes where code like $1", like)
    await conn.execute(
        "delete from plan_access_groups where plan_id in (select id from plans where name like $1)",
        like,
    )
    await conn.execute(
        "delete from plan_protocols where plan_id in (select id from plans where name like $1)",
        like,
    )
    # Подписки ссылаются на периоды, периоды — на тарифы, и каскада между ними нет (§4.2.4:
    # история не уходит вместе с тарифом). Поэтому сначала снимаются строки состояния, затем
    # журнал баланса и периоды, и только потом сами тарифы.
    await conn.execute(
        "delete from subscriptions where current_period_id in "
        "(select p.id from subscription_periods p join plans pl on pl.id = p.plan_id "
        "where pl.name like $1)",
        like,
    )
    # ``balance_entries`` — журнал только на вставку: у роли приложения отозваны UPDATE и DELETE
    # (миграция 070, R-21/R-38), и убрать записи может лишь владелец. Без владельца уборка
    # оставляет тариф с его периодами — это честнее, чем ослаблять привилегию ради теста.
    if owner is not None:
        await owner.execute(
            "delete from balance_entries where period_id in "
            "(select p.id from subscription_periods p join plans pl on pl.id = p.plan_id "
            "where pl.name like $1)",
            like,
        )
        await owner.execute(
            "delete from subscription_periods where plan_id in "
            "(select id from plans where name like $1)",
            like,
        )
        await owner.execute("delete from plans where name like $1", like)
    else:
        await conn.execute(
            "delete from subscription_periods where plan_id in "
            "(select id from plans where name like $1)",
            like,
        )
        await conn.execute("delete from plans where name like $1", like)
    await conn.execute(
        "delete from billing_group_multipliers where billing_group_id in "
        "(select id from billing_groups where name like $1)",
        like,
    )
    await conn.execute("delete from billing_groups where name like $1", like)
    await conn.execute("delete from access_groups where name like $1", like)


@asynccontextmanager
async def catalog(
    pg_dsn: str, migrate_env: dict[str, str] | None = None
) -> AsyncIterator[asyncpg.Connection]:
    """Подключение для подготовки и сверки; уборка до и после — прогон не зависит от того, чем
    кончился предыдущий. ``migrate_env`` открывает второе подключение под владельцем: только оно
    может убрать журнал баланса (append-only у роли приложения), а без журнала не удаляются ни
    периоды, ни тарифы."""
    conn = await asyncpg.connect(pg_dsn)
    owner = None
    if migrate_env is not None:
        owner = await asyncpg.connect(_owner_dsn(migrate_env["MIGRATE_DSN"]))
        await owner.execute("set role app_owner")
    try:
        await cleanup(conn, owner)
        yield conn
    finally:
        await cleanup(conn, owner)
        if owner is not None:
            await owner.close()
        await conn.close()


def _owner_dsn(migrate_dsn: str) -> str:
    """DSN ``app_migrate`` из окружения миграций в форме, понятной asyncpg."""
    return migrate_dsn.replace("postgresql+psycopg://", "postgresql://", 1)


async def billing_group(conn: asyncpg.Connection, label: str) -> uuid.UUID:
    """Тарифицируемая группа без интервала коэффициента."""
    group_id: uuid.UUID = await conn.fetchval(
        "insert into billing_groups (name) values ($1) returning id", unique_name(label)
    )
    return group_id


async def access_group(conn: asyncpg.Connection, label: str) -> uuid.UUID:
    group_id: uuid.UUID = await conn.fetchval(
        "insert into access_groups (name) values ($1) returning id", unique_name(label)
    )
    return group_id


async def node(conn: asyncpg.Connection, billing_group_id: uuid.UUID, octet: int) -> uuid.UUID:
    """Нода в тарифицируемой группе, с открытым интервалом назначения — как после ввода в
    эксплуатацию. Адрес из документационного диапазона 203.0.113.0/24."""
    node_id: uuid.UUID = await conn.fetchval(
        "insert into nodes (code, name, country, city, provider, public_ipv4, billing_group_id, "
        "bandwidth_mbps, max_conn_per_ip) values ($1, $1, 'JP', 'Tokyo', 'probe', $2, $3, 1000, 8) "
        "returning id",
        unique_name("node"),
        f"203.0.113.{octet}",
        billing_group_id,
    )
    return node_id


class RecordingHours:
    """Служба учёта, запоминающая, какой ноде и когда закрыли час (UC-09 A3). Подключение
    запоминается тоже: закрытие обязано идти на подключении транзакции смены, а не на своём."""

    def __init__(self) -> None:
        self.closed: list[tuple[uuid.UUID, object]] = []

    async def close_hour(self, conn: object, node_id: uuid.UUID, at: object) -> None:
        self.closed.append((node_id, conn))

    @property
    def nodes(self) -> list[uuid.UUID]:
        return [node_id for node_id, _ in self.closed]


async def plan(
    conn: asyncpg.Connection,
    label: str,
    *,
    days: int,
    limit: int | None,
    device_limit: int | None = 2,
) -> uuid.UUID:
    """Тариф под префиксом уборки: срок, лимит трафика (``None`` — Unlimited), лимит устройств."""
    plan_id: uuid.UUID = await conn.fetchval(
        "insert into plans (name, duration_days, traffic_limit_bytes, device_limit) "
        "values ($1, $2, $3, $4) returning id",
        unique_name(label),
        days,
        limit,
        device_limit,
    )
    return plan_id


class RecordingComposition(CompositionService):
    """Поток состава, запоминающий, за кого его разбудили: переход, не разбудивший поток, не
    даёт ни отзыва в пределах Н-14, ни восстановления доступа (§3.4, §4.11)."""

    def __init__(self) -> None:
        super().__init__(pool=None)
        self.published: list[uuid.UUID] = []

    async def publish_user(self, user_id: uuid.UUID) -> None:
        self.published.append(user_id)
