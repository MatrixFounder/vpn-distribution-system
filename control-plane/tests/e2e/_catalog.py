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

PREFIX = "t19-"


def unique_name(label: str) -> str:
    """Уникальное имя под префиксом уборки: «t19-<метка>-<восемь знаков>»."""
    return f"{PREFIX}{label}-{uuid.uuid4().hex[:8]}"


async def cleanup(conn: asyncpg.Connection) -> None:
    """Удалить всё, что тесты каталога завели под префиксом, в порядке внешних ключей."""
    like = f"{PREFIX}%"
    await conn.execute(
        "delete from node_billing_assignments where node_id in "
        "(select id from nodes where code like $1)",
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
    await conn.execute("delete from plans where name like $1", like)
    await conn.execute(
        "delete from billing_group_multipliers where billing_group_id in "
        "(select id from billing_groups where name like $1)",
        like,
    )
    await conn.execute("delete from billing_groups where name like $1", like)
    await conn.execute("delete from access_groups where name like $1", like)


@asynccontextmanager
async def catalog(pg_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    """Подключение для подготовки и сверки; уборка до и после — прогон не зависит от того, чем
    кончился предыдущий."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        await cleanup(conn)
        yield conn
    finally:
        await cleanup(conn)
        await conn.close()


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
