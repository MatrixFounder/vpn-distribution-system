"""Стенд потока состава на живой базе (задача 001.29) поверх стенда приёма отчётов
(``_reporting.py``): нода с настоящей identity и ``current_node``, её группы доступа, ещё ноды в
своих группах, тариф с группами и пользователи с подписками; раскладка фикстур контракта
(``tests/_agent.py``) — строками ``node_user_state`` и зашифрованными ``node_user_credentials``.

Всё, что стенд заводит, живёт под префиксом уборки каталога и уходит вместе со стендом отчётов.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
import httpx
from app.domain.composition import xray_email
from app.security.crypto import FieldCipher

from tests._agent import LAYOUT, LAYOUT_SEQ, layout_credentials
from tests.e2e._catalog import access_group, plan
from tests.e2e._reporting import AGENT_IP, ReportingStand, insert_user


async def seed_layout(conn: asyncpg.Connection, node_id: uuid.UUID, cipher: FieldCipher) -> None:
    """Раскладка фикстур на ноде: пользователи с идентификаторами раскладки, строки состава с
    номерами 1…4, credentials пар, которым публикация их создала бы, и курсор ноды — 4."""
    for user_id, state, seq, blocked in LAYOUT:
        await insert_user(conn, None, wanted_id=user_id)
        await conn.execute(
            "insert into node_user_state (node_id, user_id, state, quota_grant_bytes, "
            "blocked_ips, updated_seq) values ($1, $2, $3, $4, $5::inet[], $6)",
            node_id,
            user_id,
            state,
            1073741824 if state == "active" else 0,
            list(blocked),
            seq,
        )
        if state != "removed":
            keys = layout_credentials(node_id, user_id)
            await conn.execute(
                "insert into node_user_credentials (user_id, node_id, xray_email, vless_uuid_enc, "
                "trojan_password_enc, version) values ($1, $2, $3, $4, $5, $6)",
                user_id,
                node_id,
                xray_email(user_id),
                cipher.encrypt(keys.vless_uuid.bytes),
                cipher.encrypt(keys.trojan_password.encode()),
                keys.version,
            )
    await conn.execute("update nodes set desired_users_seq = $2 where id = $1", node_id, LAYOUT_SEQ)


async def group_of(conn: asyncpg.Connection, *node_ids: uuid.UUID) -> uuid.UUID:
    """Группа доступа под префиксом уборки с нодами ``node_ids``."""
    group_id = await access_group(conn, "c29")
    for node_id in node_ids:
        await conn.execute(
            "insert into node_access_groups (node_id, access_group_id) values ($1, $2)",
            node_id,
            group_id,
        )
    return group_id


async def plan_of(conn: asyncpg.Connection, *group_ids: uuid.UUID) -> uuid.UUID:
    """Тариф под префиксом уборки с группами доступа ``group_ids``."""
    plan_id = await plan(conn, "c29", days=30, limit=None)
    for group_id in group_ids:
        await conn.execute(
            "insert into plan_access_groups (plan_id, access_group_id) values ($1, $2)",
            plan_id,
            group_id,
        )
    return plan_id


async def subscribe(
    conn: asyncpg.Connection,
    plan_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None = None,
    state: str = "active",
) -> uuid.UUID:
    """Пользователь (новый, если ``user_id`` не задан) с подпиской ``state`` на период тарифа
    ``plan_id``. У существующего пользователя период заменяется новым — смена тарифа."""
    if user_id is None:
        user_id = await insert_user(conn)
    period_id: uuid.UUID = await conn.fetchval(
        "insert into subscription_periods (user_id, plan_id, period_start, period_end, source) "
        "values ($1, $2, now() - interval '1 day', now() + interval '29 days', 'admin') "
        "returning id",
        user_id,
        plan_id,
    )
    await conn.execute(
        "insert into subscriptions (user_id, state, current_period_id) values ($1, $2, $3) "
        "on conflict (user_id) do update set state = excluded.state, "
        "current_period_id = excluded.current_period_id",
        user_id,
        state,
        period_id,
    )
    return user_id


async def row_of(
    conn: asyncpg.Connection, node_id: uuid.UUID, user_id: uuid.UUID
) -> asyncpg.Record | None:
    """Строка состава пары и наличие её credentials."""
    row: asyncpg.Record | None = await conn.fetchrow(
        "select s.state::text as state, s.updated_seq, s.blocked_ips, exists(select 1 from "
        "node_user_credentials c where c.node_id = s.node_id and c.user_id = s.user_id) "
        "as has_credentials from node_user_state s where s.node_id = $1 and s.user_id = $2",
        node_id,
        user_id,
    )
    return row


async def state(
    stand: ReportingStand, params: dict[str, Any], app: Any | None = None
) -> httpx.Response:
    """Запрос состояния от ноды стенда через настоящие зависимости раздела."""
    transport = httpx.ASGITransport(
        app=app or stand.app, raise_app_exceptions=False, client=(AGENT_IP, 4000)
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get("/agent/v1/state", params=params, headers=stand.headers)


async def set_status(conn: asyncpg.Connection, node_id: uuid.UUID, status: str) -> None:
    await conn.execute("update nodes set status = $2 where id = $1", node_id, status)


def cursors(users_seq: int, *, full: bool = False) -> dict[str, Any]:
    """Курсоры запроса: конфигурация и раздел ``agent`` — текущие (их поток в этой задаче не
    меняется), состав — ``users_seq``."""
    params: dict[str, Any] = {
        "config_version": 1,
        "agent_section_version": 1,
        "users_seq": users_seq,
        "generation": 1,
    }
    if full:
        params["full"] = 1
    return params
