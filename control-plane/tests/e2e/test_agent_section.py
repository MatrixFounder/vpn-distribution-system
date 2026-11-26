"""Раздел ``agent`` ответа состояния на живой базе (задачи 001.29f и 001.29g; контракт 001.26,
§5.2): номер версии раздела — эпоха раскладки образа × 10 000 000 + колонка
``nodes.agent_section_version``, ``max_conn_per_ip`` — колонка ``nodes.max_conn_per_ip``;
остальные поля раздела — значения ``STUB_AGENT`` до своих владельцев (001.27, 001.36, 001.40).
Курсор ``agent_section_version`` запроса сверяется с номером: ниже — раздел выдаётся, равен —
нет, выше — 409 ``cursor_ahead``, снапшот отдаёт раздел целиком.

001.29g: откат кода на образ с меньшей эпохой даёт агенту 409 и снапшот, возврат — раздел;
смена коэффициента (``set_group_multiplier``, ``set_billing_group``) повышает колонку затронутых
нод в транзакции смены и после коммита публикует в ``node:{id}``; смена коэффициента группы и
вход ноды в неё упорядочены строкой группы.
"""

from __future__ import annotations

import asyncio
import ipaddress
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import pytest
from app.db.pool import get_pool
from app.domain import composition
from app.domain.composition import CompositionService, agent_section_number
from app.domain.groups import (
    _LOCK_GROUP_CHANGE,
    _LOCK_GROUP_JOIN,
    _RAISE_GROUP_NODES,
    GroupService,
)
from app.domain.nodes import STUB_NODE_IN, NodeIn, NodeService, stub_node
from app.errors import ApiError
from app.redis import get_redis
from app.security.audit import AuditContext

from tests.e2e._catalog import PREFIX, RecordingHours, billing_group, catalog, node, unique_name
from tests.e2e._composition import REDIS_FAILURE_IDS, REDIS_FAILURES, refuse_redis, state
from tests.e2e._reporting import ReportingStand, reporting, wait_blocked_by
from tests.e2e.test_composition import _ProbePool

STAND_MAX_CONN = 8  # ``max_conn_per_ip`` ноды стенда (``tests/e2e/_reporting.py``), не 32 заглушки
RAISED_MAX_CONN = 77
RAISE = (
    "update nodes set max_conn_per_ip = $2, agent_section_version = agent_section_version + 1 "
    "where id = $1"
)
COLUMN = "select agent_section_version from nodes where id = $1"
# Контекст журнала для прямых вызовов смены коэффициента (001.46), как в ``test_catalog``.
CTX = AuditContext(
    actor_type="admin",
    actor_id=uuid.UUID("00000000-0000-7000-8000-00000000ad01"),
    actor_role="super_admin",
    session_id="test-session",
    ip="203.0.113.200",
    user_agent="test",
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


async def test_tc_e2e_01_29g_the_section_number_carries_the_layout_epoch(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """TC-E2E-01 (001.29g; было TC-E2E-01 001.29f): ``agent.version`` ответа — эпоха раскладки
    образа (1) × 10 000 000 + колонка ноды (3), то есть 10 000 003. Курсор 0 и курсор 3 (номер,
    который выдавал образ 001.29f) — раздел выдаётся; курсор 10 000 003 — раздела нет, и при
    текущих курсорах потоков ответ 204; выше — 409 ``cursor_ahead``; снапшот с курсором выше
    отдаёт раздел с номером 10 000 003 (выход из опережения по контракту). Колонка 3 не совпадает
    ни с ``STUB_AGENT_VERSION`` (1), ни с умолчанием миграции (2)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        await stand.conn.execute(
            "update nodes set agent_section_version = 3 where id = $1", stand.node_id
        )
        for applied in (0, 3):
            behind = await state(stand, await cursors(stand, applied))
            assert behind.status_code == 200, (applied, behind.text)
            assert behind.json()["agent"]["version"] == 10_000_003, applied
        same = await state(stand, await cursors(stand, 10_000_003))
        assert same.status_code == 204, same.text
        ahead = await state(stand, await cursors(stand, 10_000_004))
        assert ahead.status_code == 409, ahead.text
        assert ahead.json()["error"]["code"] == "cursor_ahead"
        snapshot = await state(stand, {**(await cursors(stand, 10_000_004)), "full": 1})
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["agent"]["version"] == 10_000_003


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
            (agent_section_number(before), STAND_MAX_CONN)
            if moment == "first_read"
            else (agent_section_number(before + 1), RAISED_MAX_CONN)
        )
        assert (raced.agent.version, raced.agent.max_conn_per_ip) == expected, moment
        later = await CompositionService(pool).state_for(card, 1, 0, 1, False, 0)
        assert later is not None and later.agent is not None
        assert (later.agent.version, later.agent.max_conn_per_ip) == (
            agent_section_number(before + 1),
            RAISED_MAX_CONN,
        )


# --- 001.29g: откат и возврат образа --------------------------------------------------------------


async def test_tc_e2e_02_29g_rollback_and_return_of_the_image(
    pg_dsn: str, migrate_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-E2E-02 (001.29g): эпоха 2 → 1 → 2 подменой константы у ноды с колонкой 3, применившей
    раздел эпохи 2 (20 000 003). После отката курсор 20 000 003 выше номера образа (10 000 003) —
    409 ``cursor_ahead``, снапшот отдаёт раздел с номером 10 000 003; после возврата номер
    20 000 003 выше применённого — раздел выдаётся, а не 204. Затем откат на образ 001.29f (номер
    равен колонке — эпоха 0 подменой): курсор 20 000 003 — 409, снапшот отдаёт 3; возврат — раздел
    с 20 000 003."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        await stand.conn.execute(
            "update nodes set agent_section_version = 3 where id = $1", stand.node_id
        )
        for older, older_number in ((1, 10_000_003), (0, 3)):
            monkeypatch.setattr(composition, "AGENT_SECTION_LAYOUT_EPOCH", 2)
            current = await state(stand, await cursors(stand, 20_000_003))
            assert current.status_code == 204, (older, current.text)
            monkeypatch.setattr(composition, "AGENT_SECTION_LAYOUT_EPOCH", older)
            ahead = await state(stand, await cursors(stand, 20_000_003))
            assert ahead.status_code == 409, (older, ahead.text)
            assert ahead.json()["error"]["code"] == "cursor_ahead"
            snapshot = await state(stand, {**(await cursors(stand, 20_000_003)), "full": 1})
            assert snapshot.status_code == 200, (older, snapshot.text)
            assert snapshot.json()["agent"]["version"] == older_number, older
            applied = await state(stand, await cursors(stand, older_number))
            assert applied.status_code == 204, (older, applied.text)
            monkeypatch.setattr(composition, "AGENT_SECTION_LAYOUT_EPOCH", 2)
            back = await state(stand, await cursors(stand, older_number))
            assert back.status_code == 200, (older, back.text)
            assert back.json()["agent"]["version"] == 20_000_003, older


async def test_tc_e2e_04_29g_a_multiplier_change_inside_the_rollback_window(
    pg_dsn: str, migrate_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-E2E-04 (001.29g): нода с колонкой 3 применила раздел эпохи 2 (20 000 003); откат на
    эпоху 1 — 409, снапшот, курсор 10 000 003; смена коэффициента группы ноды под эпохой 1 без
    опроса ноды повышает колонку до 4; после возврата эпохи 2 номер 20 000 004 выше применённого
    10 000 003 — раздел выдаётся. Без повышения колонки при смене коэффициента номер после возврата
    был бы 20 000 003 — тоже выше 10 000 003, поэтому здесь решает номер в ответе, а не код ответа.

    Второе окно — агент, молчавший весь откат (его курсор 20 000 004 нового образа): откат на
    эпоху 1, смена коэффициента под ней повышает колонку до 5, возврат эпохи 2 — номер 20 000 005
    выше курсора, раздел выдаётся. Без повышения колонки номер остался бы 20 000 004, ответ — 204,
    и агент держал бы прежний коэффициент до следующего повышения: ради этого случая смена
    коэффициента повышает колонку уже в 001.29g."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        await stand.conn.execute(
            "update nodes set agent_section_version = 3 where id = $1", stand.node_id
        )
        group = await stand.conn.fetchval(
            "select billing_group_id from nodes where id = $1", stand.node_id
        )
        monkeypatch.setattr(composition, "AGENT_SECTION_LAYOUT_EPOCH", 1)
        ahead = await state(stand, await cursors(stand, 20_000_003))
        assert ahead.status_code == 409, ahead.text
        snapshot = await state(stand, {**(await cursors(stand, 20_000_003)), "full": 1})
        assert snapshot.json()["agent"]["version"] == 10_000_003
        await GroupService(await get_pool(), RecordingHours()).set_group_multiplier(
            group, 1500, ctx=CTX
        )
        assert await stand.conn.fetchval(COLUMN, stand.node_id) == 4
        monkeypatch.setattr(composition, "AGENT_SECTION_LAYOUT_EPOCH", 2)
        back = await state(stand, await cursors(stand, 10_000_003))
        assert back.status_code == 200, back.text
        assert back.json()["agent"]["version"] == 20_000_004

        monkeypatch.setattr(composition, "AGENT_SECTION_LAYOUT_EPOCH", 1)
        await GroupService(await get_pool(), RecordingHours()).set_group_multiplier(
            group, 2000, ctx=CTX
        )
        assert await stand.conn.fetchval(COLUMN, stand.node_id) == 5
        monkeypatch.setattr(composition, "AGENT_SECTION_LAYOUT_EPOCH", 2)
        silent = await state(stand, await cursors(stand, 20_000_004))
        assert silent.status_code == 200, silent.text
        assert silent.json()["agent"]["version"] == 20_000_005


# --- 001.29g: смена коэффициента повышает колонку -------------------------------------------------


class PausingHours:
    """Служба учёта, останавливающая операцию на первом закрытии часа — внутри её транзакции,
    после повышения колонки: ``inside`` — колонки нод ``watch`` на подключении транзакции, ``pid``
    — её серверный процесс (держатель блокировок для ``wait_blocked_by``)."""

    def __init__(self, *watch: uuid.UUID) -> None:
        self.watch = watch
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.inside: dict[uuid.UUID, int] = {}
        self.pid = 0

    async def close_hour(self, conn: asyncpg.Connection, node_id: uuid.UUID, at: object) -> None:
        if self.entered.is_set():
            return
        self.pid = conn.get_server_pid()
        self.inside = {n: await conn.fetchval(COLUMN, n) for n in self.watch}
        self.entered.set()
        await self.release.wait()


class FailingHours:
    """Служба учёта, отказывающая на закрытии часа: транзакция смены откатывается целиком."""

    async def close_hour(self, conn: object, node_id: uuid.UUID, at: object) -> None:
        raise RuntimeError("сбой закрытия часа")


class ObservingWakes(CompositionService):
    """Поток состава, читающий в момент пробуждения колонку ноды с отдельного подключения:
    пробуждение после коммита видит новое значение, пробуждение до коммита — прежнее (чтение
    идёт, пока транзакция смены ещё открыта)."""

    def __init__(self, pool: Any, observer: asyncpg.Connection) -> None:
        super().__init__(pool)
        self.observer = observer
        self.seen: dict[uuid.UUID, int] = {}

    async def wake_agent_section(self, node_id: uuid.UUID, column: int) -> bool:
        self.seen[node_id] = await self.observer.fetchval(COLUMN, node_id)
        return await super().wake_agent_section(node_id, column)


async def columns(conn: asyncpg.Connection, *node_ids: uuid.UUID) -> dict[uuid.UUID, int]:
    return {n: await conn.fetchval(COLUMN, n) for n in node_ids}


async def wait_blocked_on(conn: asyncpg.Connection, holder_pid: int, statement: str) -> None:
    """Дождаться, пока за процессом ``holder_pid`` встанет именно оператор ``statement``, а не
    любой ждущий: посторонний процесс стенда, ждущий того же держателя, ожидания проверяемой
    операции не доказывает."""
    for _ in range(400):
        if await conn.fetchval(
            "select count(*) from pg_stat_activity "
            "where $1 = any(pg_blocking_pids(pid)) and query = $2",
            holder_pid,
            statement,
        ):
            return
        await asyncio.sleep(0.025)
    raise AssertionError(f"за процессом {holder_pid} не встал оператор {statement!r}")


@asynccontextmanager
async def listening(
    *node_ids: uuid.UUID,
) -> AsyncIterator[Callable[..., Awaitable[dict[uuid.UUID, list[str]]]]]:
    """Подписка на ``node:{id}`` нод; ``drain(seconds, until=…)`` собирает сообщения до срока или
    до момента, когда у каждой ноды ``until`` есть сообщение."""
    redis = await get_redis()
    async with redis.pubsub() as channel:
        await channel.subscribe(*(f"node:{n}" for n in node_ids))
        loop = asyncio.get_running_loop()

        async def drain(
            seconds: float, until: tuple[uuid.UUID, ...] = ()
        ) -> dict[uuid.UUID, list[str]]:
            got: dict[uuid.UUID, list[str]] = {n: [] for n in node_ids}
            deadline = loop.time() + seconds
            while (left := deadline - loop.time()) > 0:
                message = await channel.get_message(
                    ignore_subscribe_messages=True, timeout=min(left, 0.1)
                )
                if message is None:
                    continue
                target = uuid.UUID(str(message["channel"]).split(":", 1)[1])
                got[target].append(str(message["data"]))
                if until and all(got[n] for n in until):
                    break
            return got

        yield drain


@asynccontextmanager
async def group_stand(pg_dsn: str) -> AsyncIterator[tuple[asyncpg.Connection, asyncpg.Pool]]:
    async with catalog(pg_dsn) as conn:
        pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=4)
        assert pool is not None
        try:
            yield conn, pool
        finally:
            await pool.close()


async def test_tc_e2e_03_29g_a_group_multiplier_change_raises_its_plain_nodes(
    pg_dsn: str,
) -> None:
    """TC-E2E-03 (001.29g), ``set_group_multiplier``: колонки двух нод группы без собственного
    переопределения коэффициента выросли на 1 в транзакции смены (на её подключении — уже, снаружи
    — ещё нет, пробуждений до коммита нет); нода с переопределением и нода другой группы — не
    выросли. После коммита — ровно одно сообщение в ``node:{id}`` каждой затронутой ноды с её
    новым номером, и в момент пробуждения колонка снаружи уже новая. Двух затронутых нод
    достаточно, чтобы пробуждение только первой краснело. Отказ внутри транзакции (закрытие часа)
    откатывает повышение и не публикует."""
    async with group_stand(pg_dsn) as (conn, pool):
        group = await billing_group(conn, "29g")
        other = await billing_group(conn, "29g-other")
        plain = await node(conn, group, 71)
        second = await node(conn, group, 84)
        overridden = await node(conn, group, 72)
        await conn.execute("update nodes set multiplier_milli = 300 where id = $1", overridden)
        stranger = await node(conn, other, 73)
        watched = (plain, second, overridden, stranger)
        before = await columns(conn, *watched)
        raised = {**before, plain: before[plain] + 1, second: before[second] + 1}
        quiet: dict[uuid.UUID, list[str]] = {n: [] for n in watched}
        async with listening(*watched) as drain:
            with pytest.raises(RuntimeError, match="сбой закрытия часа"):
                await GroupService(pool, FailingHours()).set_group_multiplier(group, 2000, ctx=CTX)
            assert await columns(conn, *watched) == before
            assert await drain(0.3) == quiet

            hours = PausingHours(*watched)
            wakes = ObservingWakes(pool, conn)
            change = asyncio.create_task(
                GroupService(pool, hours, wakes).set_group_multiplier(group, 2000, ctx=CTX)
            )
            try:
                await asyncio.wait_for(hours.entered.wait(), 10)
                assert hours.inside == raised, "в транзакции смены"
                assert await columns(conn, *watched) == before, "до коммита"
                assert await drain(0.3) == quiet, "до коммита"
            finally:
                # Остановленная смена держит подключение пула: без освобождения закрытие пула
                # ждало бы его вечно, и провал проверки выше стал бы зависанием.
                hours.release.set()
                (outcome,) = await asyncio.gather(change, return_exceptions=True)
            assert not isinstance(outcome, BaseException), outcome
            got = await drain(3.0, until=(plain, second))
            for target, late in (await drain(0.3)).items():
                got[target] += late
        assert await columns(conn, *watched) == raised
        assert got == {
            **quiet,
            plain: [f"agent:{agent_section_number(raised[plain])}"],
            second: [f"agent:{agent_section_number(raised[second])}"],
        }, got
        assert wakes.seen == {plain: raised[plain], second: raised[second]}, (
            "пробуждение — после коммита"
        )


async def test_tc_e2e_03_29g_a_reassignment_raises_the_node(pg_dsn: str) -> None:
    """TC-E2E-03 (001.29g), ``set_billing_group`` в своей транзакции: колонка переназначенной
    ноды выросла на 1 в транзакции назначения, ``Assignment`` несёт новое значение, после коммита —
    сообщение в ``node:{id}``; до коммита снаружи колонка прежняя и пробуждений нет. Отказ внутри
    транзакции откатывает повышение и не публикует."""
    async with group_stand(pg_dsn) as (conn, pool):
        group = await billing_group(conn, "29g")
        other = await billing_group(conn, "29g-other")
        moved = await node(conn, other, 74)
        before = await conn.fetchval(COLUMN, moved)
        async with listening(moved) as drain:
            with pytest.raises(RuntimeError, match="сбой закрытия часа"):
                await GroupService(pool, FailingHours()).set_billing_group(moved, group, ctx=CTX)
            assert await conn.fetchval(COLUMN, moved) == before
            assert await drain(0.3) == {moved: []}

            hours = PausingHours(moved)
            wakes = ObservingWakes(pool, conn)
            assign = asyncio.create_task(
                GroupService(pool, hours, wakes).set_billing_group(moved, group, ctx=CTX)
            )
            try:
                await asyncio.wait_for(hours.entered.wait(), 10)
                assert hours.inside == {moved: before + 1}, "в транзакции назначения"
                assert await conn.fetchval(COLUMN, moved) == before, "до коммита"
                assert await drain(0.3) == {moved: []}, "до коммита"
            finally:
                hours.release.set()
                (assignment,) = await asyncio.gather(assign, return_exceptions=True)
            assert not isinstance(assignment, BaseException), assignment
            got = await drain(3.0, until=(moved,))
        assert assignment.agent_section_column == before + 1
        assert await conn.fetchval(COLUMN, moved) == before + 1
        assert got[moved] == [f"agent:{agent_section_number(before + 1)}"], got
        assert wakes.seen == {moved: before + 1}, "пробуждение — после коммита"


async def test_tc_e2e_03_29g_a_reassignment_on_the_callers_connection(pg_dsn: str) -> None:
    """TC-E2E-03 (001.29g), ``set_billing_group`` на переданном подключении (001.87 —
    транзакция ``PATCH /admin/nodes/{id}``): повышение — в транзакции вызывающего (видно на его
    подключении, снаружи — нет), откат вызывающего снимает его вместе с назначением; коммит
    вызывающего оставляет колонку + 1, а сама операция не публикует — коммитом владеет
    вызывающий. Подключение вне транзакции — ``RuntimeError`` до записи."""
    async with group_stand(pg_dsn) as (conn, pool):
        group = await billing_group(conn, "29g")
        other = await billing_group(conn, "29g-other")
        moved = await node(conn, other, 75)
        before = await conn.fetchval(COLUMN, moved)
        service = GroupService(pool, RecordingHours())
        async with listening(moved) as drain, pool.acquire() as caller:
            with pytest.raises(RuntimeError):
                await service.set_billing_group(moved, group, ctx=CTX, conn=caller)
            assert await conn.fetchval(COLUMN, moved) == before

            tx = caller.transaction()
            await tx.start()
            rolled = await service.set_billing_group(moved, group, ctx=CTX, conn=caller)
            assert rolled.agent_section_column == before + 1
            assert await caller.fetchval(COLUMN, moved) == before + 1, "в транзакции вызывающего"
            assert await conn.fetchval(COLUMN, moved) == before, "снаружи — до коммита"
            await tx.rollback()
            assert await conn.fetchval(COLUMN, moved) == before, "откат вызывающего"
            assert (
                await conn.fetchval("select billing_group_id from nodes where id = $1", moved)
                == other
            )

            async with caller.transaction():
                kept = await service.set_billing_group(moved, group, ctx=CTX, conn=caller)
            assert kept.agent_section_column == before + 1
            assert await conn.fetchval(COLUMN, moved) == before + 1
            assert await drain(0.5) == {moved: []}, "публикует вызывающий после своего коммита"


@pytest.mark.parametrize("failure", REDIS_FAILURES, ids=REDIS_FAILURE_IDS)
async def test_tc_e2e_03_29g_wake_agent_section_reports_a_refusal(
    monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    """TC-E2E-03 (001.29g): ``wake_agent_section`` — интерфейс, которым будит и вызывающий со
    своим подключением после своего коммита (001.87, PATCH): отказ Redis на команде — обрыв,
    зависание или ответ-ошибка — ``False``, а не исключение: правка уже закоммичена (роаст,
    раунды 2–3)."""
    client = refuse_redis(monkeypatch, failure)
    node_id = uuid.uuid4()
    assert await CompositionService(await get_pool()).wake_agent_section(node_id, 3) is False
    assert client.attempts == [f"node:{node_id}"], "отказ — на самой публикации"


@pytest.mark.parametrize("failure", REDIS_FAILURES, ids=REDIS_FAILURE_IDS)
async def test_tc_e2e_03_29g_wakes_stop_at_the_first_redis_refusal(
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: type[Exception],
) -> None:
    """TC-E2E-03 (001.29g): Redis отказывает на настоящем пути пробуждения (клиент строится, а
    ``publish`` отказывает) — смена коэффициента группы из трёх нод и назначение ноды закоммичены
    и возвращаются без ошибки, колонки выросли; за смену — одна попытка публикации (остальные не
    отправляются: каждая ждала бы того же отказа), журнал называет число пропущенных. Отказ — на
    команде, а не на получении клиента: обработка, охватывающая только ``get_redis`` или только
    часть отказов, дала бы 5xx на закоммиченную смену (роаст, раунды 2–3)."""
    async with group_stand(pg_dsn) as (conn, pool):
        group = await billing_group(conn, "29g")
        other = await billing_group(conn, "29g-other")
        nodes = [await node(conn, group, octet) for octet in (80, 81, 82)]
        moved = await node(conn, other, 85)
        before = await columns(conn, *nodes, moved)
        client = refuse_redis(monkeypatch, failure)
        service = GroupService(pool, RecordingHours())
        with caplog.at_level("WARNING", logger="app.domain.groups"):
            await service.set_group_multiplier(group, 2000, ctx=CTX)
        assert len(client.attempts) == 1, client.attempts
        assert "пробуждения ещё 2 нод пропущены" in caplog.text
        assigned = await service.set_billing_group(moved, group, 1500, ctx=CTX)
        assert client.attempts[1:] == [f"node:{moved}"], client.attempts
        assert assigned.agent_section_column == before[moved] + 1
        assert await columns(conn, *nodes, moved) == {n: before[n] + 1 for n in [*nodes, moved]}


# --- 001.29g: смена коэффициента и вход ноды в группу -------------------------------------------


async def test_tc_e2e_05_29g_a_join_waits_for_the_multiplier_change(pg_dsn: str) -> None:
    """TC-E2E-05 (001.29g), смена первой: смена коэффициента группы повысила колонки её нод и ещё
    не закоммичена; вход другой ноды в эту группу ждёт её коммита на строке группы, а не
    коммитится раньше — иначе агент вошедшей ноды прочитал бы прежний коэффициент под последним
    номером, и смена его ноду не повысила бы. Держатель — сама операция смены (остановлена на
    закрытии часа), а не отдельный оператор."""
    async with group_stand(pg_dsn) as (conn, pool):
        group = await billing_group(conn, "29g")
        other = await billing_group(conn, "29g-other")
        plain = await node(conn, group, 76)
        joining = await node(conn, other, 77)
        before = await columns(conn, plain, joining)
        hours = PausingHours(plain)
        change = asyncio.create_task(
            GroupService(pool, hours).set_group_multiplier(group, 2000, ctx=CTX)
        )
        await asyncio.wait_for(hours.entered.wait(), 10)
        join = asyncio.create_task(
            GroupService(pool, RecordingHours()).set_billing_group(joining, group, ctx=CTX)
        )
        try:
            await wait_blocked_on(conn, hours.pid, _LOCK_GROUP_JOIN)
            assert not join.done(), "вход в группу не ждёт смены коэффициента"
            assert (
                await conn.fetchval("select billing_group_id from nodes where id = $1", joining)
                == other
            ), "вход не закоммичен до коммита смены"
        finally:
            hours.release.set()
            outcomes = await asyncio.gather(change, join, return_exceptions=True)
        assert not any(isinstance(o, BaseException) for o in outcomes), outcomes
        assert await columns(conn, plain, joining) == {
            plain: before[plain] + 1,
            joining: before[joining] + 1,
        }


async def test_tc_e2e_05_29g_a_multiplier_change_waits_for_a_join(pg_dsn: str) -> None:
    """TC-E2E-05 (001.29g), вход первым: нода вошла в группу и ещё не закоммичена; смена
    коэффициента группы ждёт её коммита до выборки нод и затем повышает и вошедшую ноду — её
    колонка выросла дважды (вход и смена). Блокировка после выборки или без неё оставляет
    вошедшую ноду с одним повышением: выборка смены её не видит."""
    async with group_stand(pg_dsn) as (conn, pool):
        group = await billing_group(conn, "29g")
        other = await billing_group(conn, "29g-other")
        plain = await node(conn, group, 78)
        joining = await node(conn, other, 79)
        before = await columns(conn, plain, joining)
        hours = PausingHours(joining)
        join = asyncio.create_task(
            GroupService(pool, hours).set_billing_group(joining, group, ctx=CTX)
        )
        await asyncio.wait_for(hours.entered.wait(), 10)
        change = asyncio.create_task(
            GroupService(pool, RecordingHours()).set_group_multiplier(group, 2000, ctx=CTX)
        )
        try:
            await wait_blocked_on(conn, hours.pid, _LOCK_GROUP_CHANGE)
            assert not change.done(), "смена коэффициента не ждёт входа ноды"
        finally:
            hours.release.set()
            outcomes = await asyncio.gather(join, change, return_exceptions=True)
        assert not any(isinstance(o, BaseException) for o in outcomes), outcomes
        assert await columns(conn, plain, joining) == {
            plain: before[plain] + 1,
            joining: before[joining] + 2,
        }


async def test_tc_e2e_05_29g_two_changes_of_one_group_do_not_queue(pg_dsn: str) -> None:
    """TC-E2E-05 (001.29g), две смены одной группы: смена держит строку группы ``FOR SHARE``,
    и вторая смена той же группы на строке группы не выстраивается — как до 001.29g, она ждёт
    незакоммиченный интервал первой и после её коммита получает 409 от ``EXCLUDE``, история —
    один интервал. Режим, исключающий сам себя (``FOR NO KEY UPDATE``, ``FOR UPDATE``), выстроил
    бы смены в очередь на строке группы, и вторая прошла бы последовательной сменой — поведение,
    которого задача не меняет."""
    async with group_stand(pg_dsn) as (conn, pool):
        group = await billing_group(conn, "29g")
        plain = await node(conn, group, 83)
        hours = PausingHours(plain)
        first = asyncio.create_task(
            GroupService(pool, hours).set_group_multiplier(group, 2000, ctx=CTX)
        )
        await asyncio.wait_for(hours.entered.wait(), 10)
        second = asyncio.create_task(
            GroupService(pool, RecordingHours()).set_group_multiplier(group, 3000, ctx=CTX)
        )
        try:
            await wait_blocked_by(conn, hours.pid)
        finally:
            hours.release.set()
            await first
        with pytest.raises(ApiError) as refused:
            await second
        assert refused.value.status == 409, refused.value.message
        rows = await conn.fetch(
            "select multiplier_milli from billing_group_multipliers where billing_group_id = $1",
            group,
        )
        assert [r["multiplier_milli"] for r in rows] == [2000]


class _Gate:
    """Пул, останавливающий операцию непосредственно перед оператором ``statement`` (один раз):
    ``reached`` — операция дошла до него, ``pid`` — её серверный процесс."""

    def __init__(self, pool: asyncpg.Pool, statement: str) -> None:
        self._pool, self.statement = pool, statement
        self.reached, self.release = asyncio.Event(), asyncio.Event()
        self.pid = 0

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        async with self._pool.acquire() as conn:
            yield _GateConnection(conn, self)


class _GateConnection:
    def __init__(self, conn: Any, gate: _Gate) -> None:
        self._conn, self._gate = conn, gate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def _stop(self, query: str) -> None:
        if query == self._gate.statement and not self._gate.reached.is_set():
            self._gate.pid = self._conn.get_server_pid()
            self._gate.reached.set()
            await self._gate.release.wait()

    async def fetchval(self, query: str, *args: Any) -> Any:
        await self._stop(query)
        return await self._conn.fetchval(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        await self._stop(query)
        return await self._conn.fetchrow(query, *args)

    async def fetch(self, query: str, *args: Any) -> Any:
        await self._stop(query)
        return await self._conn.fetch(query, *args)

    async def execute(self, query: str, *args: Any) -> Any:
        await self._stop(query)
        return await self._conn.execute(query, *args)


async def test_tc_e2e_05_29g_a_member_reassigned_while_its_group_changes(pg_dsn: str) -> None:
    """TC-E2E-05 (001.29g), порядок блокировок входа — строка группы до строки ноды: нода уже в
    группе без переопределения, её переназначение в ту же группу с переопределением остановлено
    перед блокировкой строки группы, и в это время идёт смена коэффициента группы. Смена
    повышает ноду и коммитится (вставка назначения держит на строке ноды только ключевую
    блокировку внешнего ключа), затем назначение продолжает: обе операции завершаются, колонка
    ноды + 2. Строка ноды, записанная входом раньше строки группы, дала бы взаимную блокировку:
    смена держит группу и ждёт строку ноды, вход держит строку ноды и ждёт группу."""
    async with group_stand(pg_dsn) as (conn, pool):
        group = await billing_group(conn, "29g")
        member = await node(conn, group, 86)
        before = await conn.fetchval(COLUMN, member)
        gate = _Gate(pool, _LOCK_GROUP_JOIN)
        join = asyncio.create_task(
            GroupService(gate, RecordingHours()).set_billing_group(member, group, 1500, ctx=CTX)
        )
        await asyncio.wait_for(gate.reached.wait(), 10)
        change = asyncio.create_task(
            GroupService(pool, RecordingHours()).set_group_multiplier(group, 2000, ctx=CTX)
        )
        try:
            done, _ = await asyncio.wait([change], timeout=10)
            assert change in done, "смена коэффициента ждёт остановленный вход"
        finally:
            gate.release.set()
            outcomes = await asyncio.gather(join, change, return_exceptions=True)
        assert not any(isinstance(o, BaseException) for o in outcomes), outcomes
        assert await conn.fetchval(COLUMN, member) == before + 2


async def test_tc_e2e_05_29g_a_join_on_the_callers_connection(pg_dsn: str) -> None:
    """TC-E2E-05 (001.29g) на подключении вызывающего (001.87, транзакция PATCH): вход другой
    ноды в группу сделан, транзакция вызывающего открыта — смена коэффициента группы ждёт её
    коммита на строке группы и затем повышает и вошедшую ноду (+ 2)."""
    async with group_stand(pg_dsn) as (conn, pool):
        group = await billing_group(conn, "29g")
        other = await billing_group(conn, "29g-other")
        plain = await node(conn, group, 87)
        joining = await node(conn, other, 88)
        before = await columns(conn, plain, joining)
        async with pool.acquire() as caller:
            change: asyncio.Task[Any] | None = None
            try:
                async with caller.transaction():
                    await GroupService(pool, RecordingHours()).set_billing_group(
                        joining, group, ctx=CTX, conn=caller
                    )
                    change = asyncio.create_task(
                        GroupService(pool, RecordingHours()).set_group_multiplier(
                            group, 2000, ctx=CTX
                        )
                    )
                    await wait_blocked_on(conn, caller.get_server_pid(), _LOCK_GROUP_CHANGE)
                    assert not change.done(), "смена не ждёт входа на подключении вызывающего"
            finally:
                if change is not None:
                    settled = list(await asyncio.gather(change, return_exceptions=True))
                    assert not isinstance(settled[0], BaseException), settled
        assert await columns(conn, plain, joining) == {
            plain: before[plain] + 1,
            joining: before[joining] + 2,
        }


async def test_tc_e2e_05_29g_node_creation_does_not_wait_for_a_join(pg_dsn: str) -> None:
    """TC-E2E-05 (001.29g): вход держит строку группы ``FOR NO KEY UPDATE``, а не ``FOR UPDATE``
    — создание ноды в этой группе (``NodeService.create``: вставка ноды и её первого назначения
    берёт на строке группы только ключевую блокировку внешнего ключа) незакоммиченного входа не
    ждёт: создание групповой блокировки не берёт и не должно стоять за ней. Создание идёт через
    службу, а не сырой вставкой: блокировка группы, добавленная в ``create``, краснела бы
    (роаст, раунд 3)."""
    async with group_stand(pg_dsn) as (conn, pool):
        group = await billing_group(conn, "29g")
        other = await billing_group(conn, "29g-other")
        joining = await node(conn, other, 89)
        hours = PausingHours(joining)
        join = asyncio.create_task(
            GroupService(pool, hours).set_billing_group(joining, group, ctx=CTX)
        )
        side = await asyncpg.create_pool(
            pg_dsn, min_size=1, max_size=1, server_settings={"lock_timeout": "2s"}
        )
        assert side is not None
        try:
            await asyncio.wait_for(hours.entered.wait(), 10)
            created = await NodeService(side).create(
                NodeIn(
                    code=unique_name("node"),
                    name=unique_name("node"),
                    country="JP",
                    city="Tokyo",
                    provider="probe",
                    public_ipv4=ipaddress.IPv4Address("203.0.113.102"),
                    billing_group_id=group,
                    bandwidth_mbps=1000,
                    max_conn_per_ip=8,
                ),
                created_by=uuid.UUID("00000000-0000-7000-8000-00000000ad01"),
            )
        finally:
            hours.release.set()
            outcomes = await asyncio.gather(join, return_exceptions=True)
            await side.close()
        assert not any(isinstance(o, BaseException) for o in outcomes), outcomes
        assert (
            await conn.fetchval("select billing_group_id from nodes where id = $1", created.id)
            == group
        )


async def test_tc_e2e_05_29g_two_assignments_of_one_node_to_one_group(pg_dsn: str) -> None:
    """TC-E2E-05 (001.29g): два одновременных назначения одной ноды в одну группу с разными
    переопределениями — второе, начатое позже, получает 409 от ``EXCLUDE``, как до 001.29g
    (UC-09 A1, 001.19): блокировка строки группы берётся после записи назначения и арбитром
    одновременных назначений не становится. Блокировка до записи выстроила бы их в очередь, и
    второе молча переписало бы первое («последний выиграл»)."""
    async with group_stand(pg_dsn) as (conn, pool):
        group = await billing_group(conn, "29g")
        other = await billing_group(conn, "29g-other")
        moved = await node(conn, other, 103)
        hours = PausingHours(moved)
        first = asyncio.create_task(
            GroupService(pool, hours).set_billing_group(moved, group, 1500, ctx=CTX)
        )
        await asyncio.wait_for(hours.entered.wait(), 10)
        second = asyncio.create_task(
            GroupService(pool, RecordingHours()).set_billing_group(moved, group, 3000, ctx=CTX)
        )
        try:
            await wait_blocked_by(conn, hours.pid)
        finally:
            hours.release.set()
            outcomes = await asyncio.gather(first, second, return_exceptions=True)
        assert not isinstance(outcomes[0], BaseException), outcomes
        assert isinstance(outcomes[1], ApiError) and outcomes[1].status == 409, outcomes
        assert await conn.fetchval("select multiplier_milli from nodes where id = $1", moved) == (
            1500
        )


async def test_tc_e2e_05_29g_the_change_takes_node_rows_in_id_order(pg_dsn: str) -> None:
    """TC-E2E-05 (001.29g): смена коэффициента блокирует строки нод группы по возрастанию ``id``
    — порядком других писателей нескольких строк нод (``_LOCK_NODE`` ротации credentials). Строка
    нижней ноды переписана так, что её версия лежит дальше строки верхней (скан без порядка
    встретил бы верхнюю первой); держатель берёт верхнюю оператором ``_LOCK_NODE``. Смена встаёт
    за держателем, уже держа нижнюю: проба нижней с ``skip locked`` её не получает. Остальные
    ключи строки, по которым можно было бы упорядочить (``code``, ``name``, ``public_ipv4``,
    ``created_at``), у нижней по ``id`` ноды больше, чем у верхней: порядок по любому из них
    краснеет на каждом прогоне, а не в половине (роаст, раунд 2)."""
    async with group_stand(pg_dsn) as (conn, pool):
        group = await billing_group(conn, "29g")
        lower, higher = sorted([await node(conn, group, 104), await node(conn, group, 105)])
        tag = uuid.uuid4().hex[:8]
        await conn.execute(
            "update nodes set code = $2, name = $2, public_ipv4 = '203.0.113.104' where id = $1",
            higher,
            f"{PREFIX}a-{tag}",
        )
        await conn.execute(
            "update nodes set public_ipv4 = '203.0.113.105', "
            "created_at = (select created_at from nodes where id = $2) + interval '1 second' "
            "where id = $1",
            lower,
            higher,
        )
        later = (
            "select (select ctid from nodes where id = $1) > (select ctid from nodes where id = $2)"
        )
        for attempt in range(50):
            # Изменение уникальной колонки — не HOT: новая версия строки получает новый адрес.
            await conn.execute(
                "update nodes set code = $2, name = $2 where id = $1",
                lower,
                f"{PREFIX}z{attempt}-{tag}",
            )
            if await conn.fetchval(later, lower, higher):
                break
        assert await conn.fetchval(later, lower, higher), "версия нижней ноды не легла дальше"
        reversed_keys = await conn.fetchval(
            "select l.code > h.code and l.name > h.name and l.public_ipv4 > h.public_ipv4 "
            "and l.created_at > h.created_at from nodes l, nodes h where l.id = $1 and h.id = $2",
            lower,
            higher,
        )
        assert reversed_keys, "другие ключи нижней ноды не больше, чем у верхней"
        before = await columns(conn, lower, higher)
        holder = await asyncpg.connect(pg_dsn)
        change: asyncio.Task[Any] | None = None
        try:
            async with holder.transaction():
                await holder.fetchval(composition._LOCK_NODE, higher)
                change = asyncio.create_task(
                    GroupService(pool, RecordingHours()).set_group_multiplier(group, 2000, ctx=CTX)
                )
                await wait_blocked_on(conn, holder.get_server_pid(), _RAISE_GROUP_NODES)
                taken = await conn.fetchval(
                    "select id from nodes where id = $1 for no key update skip locked", lower
                )
                assert taken is None, "нижняя строка не взята до верхней"
        finally:
            await holder.close()
            if change is not None:
                settled = list(await asyncio.gather(change, return_exceptions=True))
                assert not isinstance(settled[0], BaseException), settled
        assert await columns(conn, lower, higher) == {n: before[n] + 1 for n in (lower, higher)}


async def test_tc_e2e_03_29g_the_column_limit_rolls_the_change_back(pg_dsn: str) -> None:
    """TC-E2E-03 (001.29g): повышение колонки сверх 9 999 999 отвергает ``CHECK`` колонки, и
    транзакция смены откатывается целиком — отдельной обработки нет (решение заказчика
    2026-11-25): смена коэффициента группы и назначение ноды отказывают, интервала коэффициента и
    назначения нет, колонки прежние. Насыщение (``least(…, 9999999)``) вместо отказа оставило бы
    агента молча на прежнем коэффициенте (роаст, раунд 2)."""
    async with group_stand(pg_dsn) as (conn, pool):
        group = await billing_group(conn, "29g")
        other = await billing_group(conn, "29g-other")
        full = await node(conn, group, 106)
        moved = await node(conn, other, 107)
        for n in (full, moved):
            await conn.execute("update nodes set agent_section_version = 9999999 where id = $1", n)
        service = GroupService(pool, RecordingHours())
        with pytest.raises(asyncpg.CheckViolationError):
            await service.set_group_multiplier(group, 2000, ctx=CTX)
        assert not await conn.fetchval(
            "select count(*) from billing_group_multipliers where billing_group_id = $1", group
        ), "смена откатилась"
        with pytest.raises(asyncpg.CheckViolationError):
            await service.set_billing_group(moved, group, ctx=CTX)
        assert await conn.fetchval("select billing_group_id from nodes where id = $1", moved) == (
            other
        ), "назначение откатилось"
        assert await columns(conn, full, moved) == {full: 9_999_999, moved: 9_999_999}
