"""Раздел ``agent`` ответа состояния на живой базе (задача 001.29f; контракт 001.26, §5.2):
версия раздела — колонка ``nodes.agent_section_version``, ``max_conn_per_ip`` — колонка
``nodes.max_conn_per_ip``; остальные поля раздела — значения ``STUB_AGENT`` до своих владельцев
(001.27, 001.36, 001.40). Курсор ``agent_section_version`` запроса сверяется с колонкой: ниже —
раздел выдаётся, равен — нет, выше — 409 ``cursor_ahead``, снапшот отдаёт раздел целиком.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import pytest
from app.db.pool import get_pool
from app.domain import composition
from app.domain.composition import CompositionService
from app.domain.nodes import STUB_NODE_IN, stub_node

from tests.e2e._composition import state
from tests.e2e._reporting import ReportingStand, reporting
from tests.e2e.test_composition import _ProbePool

STAND_MAX_CONN = 8  # ``max_conn_per_ip`` ноды стенда (``tests/e2e/_reporting.py``), не 32 заглушки
RAISED_MAX_CONN = 77
RAISE = (
    "update nodes set max_conn_per_ip = $2, agent_section_version = agent_section_version + 1 "
    "where id = $1"
)


async def cursors(stand: ReportingStand, agent_section_version: int) -> dict[str, Any]:
    """Текущие курсоры ноды стенда по конфигурации и составу и заданный курсор раздела."""
    node = await stand.conn.fetchrow(
        "select desired_config_version, desired_users_seq from nodes where id = $1", stand.node_id
    )
    return {
        "config_version": node["desired_config_version"],
        "users_seq": node["desired_users_seq"],
        "agent_section_version": agent_section_version,
        "generation": 1,
    }


async def test_tc_e2e_01_the_agent_section_version_is_the_node_column(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """TC-E2E-01: ``agent.version`` ответа — значение колонки ноды. Курсор ниже колонки — раздел
    выдаётся; равен — раздела нет, и при текущих курсорах потоков ответ 204; выше — 409
    ``cursor_ahead``; снапшот с курсором выше колонки отдаёт раздел версии колонки (выход из
    опережения по контракту). Колонка выставлена в 3: значение, которое не совпадает ни с
    ``STUB_AGENT_VERSION`` (1), ни с начальным значением миграции (2)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        await stand.conn.execute(
            "update nodes set agent_section_version = 3 where id = $1", stand.node_id
        )
        for applied in (0, 2):
            behind = await state(stand, await cursors(stand, applied))
            assert behind.status_code == 200, (applied, behind.text)
            assert behind.json()["agent"]["version"] == 3, applied
        same = await state(stand, await cursors(stand, 3))
        assert same.status_code == 204, same.text
        ahead = await state(stand, await cursors(stand, 4))
        assert ahead.status_code == 409, ahead.text
        assert ahead.json()["error"]["code"] == "cursor_ahead"
        snapshot = await state(stand, {**(await cursors(stand, 4)), "full": 1})
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["agent"]["version"] == 3


async def test_tc_e2e_02_max_conn_per_ip_is_the_node_column(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """TC-E2E-02: ``max_conn_per_ip`` раздела — колонка ноды (у стенда 8, у заглушки 32), после
    смены колонки вместе с версией раздела — новое значение. Новое значение при прежней версии
    ноде с текущим курсором не выдаётся: доставку решает версия, и задача, меняющая поле, обязана
    её поднять (001.27, 001.87). Остальные поля — значения заглушки."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        first = await state(stand, await cursors(stand, 0))
        assert first.status_code == 200, first.text
        agent = first.json()["agent"]
        assert agent["max_conn_per_ip"] == STAND_MAX_CONN
        for name in ("new_conn_rate_per_ip", "multiplier_milli", "disconnect_strategy"):
            assert agent[name] == getattr(composition.STUB_AGENT, name), name
        version = agent["version"]
        await stand.conn.execute(
            "update nodes set max_conn_per_ip = 77 where id = $1", stand.node_id
        )
        quiet = await state(stand, await cursors(stand, version))
        assert quiet.status_code == 204, "без новой версии раздел не выдаётся"
        await stand.conn.execute(
            "update nodes set agent_section_version = agent_section_version + 1 where id = $1",
            stand.node_id,
        )
        raised = await state(stand, await cursors(stand, version))
        assert raised.status_code == 200, raised.text
        assert raised.json()["agent"]["max_conn_per_ip"] == 77
        assert raised.json()["agent"]["version"] == version + 1


class _BeginProbe:
    """Пул-зонд второго момента: ``between`` коммитится в транзакции, которую служба открыла на
    подключении зонда, непосредственно перед её первым оператором (``repeatable read`` берёт снимок
    на первом операторе). Не сразу после ``BEGIN``: чтение мимо пула службы между ``BEGIN`` и
    первым оператором легло бы по ту же сторону коммита, что и снимок, и смеси не дало бы (роаст
    001.29f, раунд 3). Признак общий для подключений: зонд срабатывает один раз."""

    def __init__(self, pool: asyncpg.Pool, between: Callable[[], Awaitable[None]]) -> None:
        self._pool, self.between, self.fired = pool, between, False

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_BeginConnection]:
        async with self._pool.acquire() as conn:
            yield _BeginConnection(conn, self)


class _BeginConnection:
    def __init__(self, conn: Any, probe: _BeginProbe) -> None:
        self._conn, self._probe = conn, probe

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def _before_first(self) -> None:
        if not self._probe.fired and self._conn.is_in_transaction():
            self._probe.fired = True
            await self._probe.between()

    async def fetchrow(self, query: str, *args: Any) -> Any:
        await self._before_first()
        return await self._conn.fetchrow(query, *args)

    async def fetch(self, query: str, *args: Any) -> Any:
        await self._before_first()
        return await self._conn.fetch(query, *args)


@pytest.mark.parametrize("moment", ["first_read", "begin"])
async def test_tc_e2e_03_the_version_and_the_limit_come_from_one_snapshot(
    pg_dsn: str, migrate_env: dict[str, str], moment: str
) -> None:
    """TC-E2E-03: версия раздела и ``max_conn_per_ip`` — одной строкой ноды в снимке ``_read``.
    Смена лимита вместе с версией коммитится в один из двух моментов: ``first_read`` — после
    первого чтения выдачи (снимок уже взят: ответ — прежняя пара (версия, 8)), ``begin`` — в
    транзакции ``_read`` непосредственно перед её первым оператором (снимок ещё не взят: ответ —
    новая пара (версия + 1, 77)). Пара — точная для момента: чтение версии или лимита вне снимка
    (до транзакции, отдельным подключением, из карточки запроса) или чтение до ``_read``, на
    котором зонд сработал бы раньше, дают смесь или пару другого момента. Затем — карточка ноды
    запроса, прочитанная до смены (лимит 8): раздел несёт новую пару из строки ноды."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        pool = await get_pool()
        before = await stand.conn.fetchval(
            "select agent_section_version from nodes where id = $1", stand.node_id
        )

        async def between() -> None:
            async with pool.acquire() as conn:
                await conn.execute(RAISE, stand.node_id, RAISED_MAX_CONN)

        card = stub_node(
            STUB_NODE_IN.model_copy(update={"max_conn_per_ip": STAND_MAX_CONN}),
            node_id=stand.node_id,
        )
        probe: _ProbePool | _BeginProbe = (
            _ProbePool(pool, between) if moment == "first_read" else _BeginProbe(pool, between)
        )
        raced = await CompositionService(probe).state_for(card, 1, 0, 1, False, 0)
        assert probe.fired, "зонд сработал"
        assert raced is not None and raced.agent is not None
        expected = (
            (before, STAND_MAX_CONN) if moment == "first_read" else (before + 1, RAISED_MAX_CONN)
        )
        assert (raced.agent.version, raced.agent.max_conn_per_ip) == expected, moment
        later = await CompositionService(pool).state_for(card, 1, 0, 1, False, 0)
        assert later is not None and later.agent is not None
        assert (later.agent.version, later.agent.max_conn_per_ip) == (
            before + 1,
            RAISED_MAX_CONN,
        )
