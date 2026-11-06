"""Публикация состава через очередь на живой базе (задача 001.29b; R-04; UC-05, UC-11): постановка
``composition.publish_user`` в транзакции бизнес-операции, ``composition.publish_node`` при смене
групп тарифа и удалении группы, состав ноды порциями под блокировками пользователей, снятие
доступа удалённого пользователя.

Задачи ставятся под типами тестов (``tests/conftest.py``): живые исполнители стенда их не берут,
исполняет их тест — ``run_publications`` (выборка, обработчик, завершение).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
import pytest
from app.db.pool import get_pool, transaction
from app.domain import composition as composition_module
from app.domain.composition import PUBLISH_LOCK_CLASS, CompositionService, next_users_seq
from app.domain.groups import GroupService
from app.domain.nodes import NodeIn, NodeService
from app.domain.plans import PlanIn, PlanPatch, PlanService
from app.domain.subscriptions import SubscriptionService
from app.errors import ApiError
from app.security.crypto import CipherMismatch, FieldCipher

from tests.e2e._catalog import RecordingHours, unique_name
from tests.e2e._composition import (
    cursors,
    group_of,
    plan_of,
    row_of,
    run_publications,
    state,
    subscribe,
)
from tests.e2e._reporting import insert_node, insert_user, reporting, wait_blocked_by

# Профили тарифа: модель ``Plan`` требует хотя бы один, а тарифы стенда (``plan_of``) заводятся
# без них — изменение передаёт их вместе с группами.
PROFILES: list[Any] = ["vless_raw_vision"]


async def _jobs_of(conn: asyncpg.Connection, kind: str, key: str, value: uuid.UUID) -> list[Any]:
    """Поставленные задачи типа ``kind`` с ``payload[key] = value``."""
    rows: list[Any] = await conn.fetch(
        "select queue::text as queue, payload, idempotency_key, status::text as status, "
        "xmin::text::bigint as xmin from jobs where type = $1 and payload->>$2 = $3 order by id",
        kind,
        key,
        str(value),
    )
    return rows


class _FailsAfterEnqueue(CompositionService):
    """Публикация поставлена, а переход после неё падает: транзакция перехода откатывается."""

    async def enqueue_publish_user(self, conn: Any, user_id: uuid.UUID) -> None:
        await super().enqueue_publish_user(conn, user_id)
        raise RuntimeError("переход упал после постановки")


async def test_tc_e2e_01_a_transition_enqueues_the_publication_in_its_transaction(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Перевод в ``suspended_quota`` ставит ``composition.publish_user`` в транзакции перехода:
    откат перехода уносит и задачу (и состояние); коммит оставляет одну задачу очереди
    ``critical`` с ключом ``publish_user:{user_id}:{xid}``; после её исполнения строка
    пользователя на ноде — в новом состоянии, и дельта ноды её несёт."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        user_id = await subscribe(conn, await plan_of(conn, await group_of(conn, stand.node_id)))
        kind = composition_module.PUBLISH_USER_JOB
        pool = await get_pool()
        with pytest.raises(RuntimeError, match="после постановки"):
            await SubscriptionService(pool, _FailsAfterEnqueue(pool)).set_state(
                user_id, "suspended_quota", "квота"
            )
        assert await _jobs_of(conn, kind, "user_id", user_id) == [], "задача ушла с откатом"
        assert (
            await conn.fetchval("select state::text from subscriptions where user_id = $1", user_id)
            == "active"
        )
        await SubscriptionService(pool).set_state(user_id, "suspended_quota", "квота")
        placed = await _jobs_of(conn, kind, "user_id", user_id)
        assert len(placed) == 1 and placed[0]["queue"] == "critical"
        assert re.fullmatch(rf"publish_user:{user_id}:\d+", placed[0]["idempotency_key"])
        assert await row_of(conn, stand.node_id, user_id) is None, "до исполнения строки нет"
        executed = await run_publications(conn)
        assert [job.type for job in executed] == [kind]
        row = await row_of(conn, stand.node_id, user_id)
        assert row is not None and row["state"] == "suspended_quota"
        response = await state(stand, cursors(0))
        rows = {r["user_id"]: r["state"] for r in response.json()["users"]["rows"]}
        assert rows[str(user_id)] == "suspended_quota"


async def test_one_transaction_places_one_publication_per_user(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Ключ задачи — пользователь и транзакция: две постановки в одной транзакции — одна задача,
    постановка в другой транзакции — ещё одна (её изменения публикует уже она)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        user_id = await subscribe(conn, await plan_of(conn, await group_of(conn, stand.node_id)))
        pool = await get_pool()
        service = CompositionService(pool)
        async with transaction(pool) as tx:
            await service.enqueue_publish_user(tx, user_id)
            await service.enqueue_publish_user(tx, user_id)
        async with transaction(pool) as tx:
            await service.enqueue_publish_user(tx, user_id)
        placed = await _jobs_of(conn, composition_module.PUBLISH_USER_JOB, "user_id", user_id)
        assert len(placed) == 2
        assert len({job["idempotency_key"] for job in placed}) == 2


async def test_tc_e2e_02_a_plan_composition_change_publishes_its_nodes(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Группа нод вошла в состав тарифа: задача ``composition.publish_node`` на каждую ноду группы
    в транзакции изменения; после исполнения у нод строки всех пользователей тарифа в их
    состояниях. Группа вышла — снова задача на каждую ноду, после исполнения строки ``removed``."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        node = stand.node_id
        other = await insert_node(conn, like=node)
        group = await group_of(conn, node, other)
        plan_id = await plan_of(conn)
        active = await subscribe(conn, plan_id)
        paused = await subscribe(conn, plan_id, state="suspended_admin")
        plans = PlanService(await get_pool())
        await plans.update(plan_id, PlanPatch(access_group_ids=[group], profiles=PROFILES))
        kind = composition_module.PUBLISH_NODE_JOB
        for node_id in (node, other):
            placed = await _jobs_of(conn, kind, "node_id", node_id)
            assert len(placed) == 1 and placed[0]["queue"] == "critical", node_id
            assert re.fullmatch(rf"publish_node:{node_id}:\d+", placed[0]["idempotency_key"])
        await run_publications(conn)
        for node_id in (node, other):
            first = await row_of(conn, node_id, active)
            second = await row_of(conn, node_id, paused)
            assert first is not None and first["state"] == "active" and first["has_credentials"]
            assert second is not None and second["state"] == "suspended_admin"
        await plans.update(plan_id, PlanPatch(access_group_ids=[], profiles=PROFILES))
        await run_publications(conn)
        for node_id in (node, other):
            for user in (active, paused):
                row = await row_of(conn, node_id, user)
                assert row is not None and row["state"] == "removed", (node_id, user)
                assert not row["has_credentials"]


async def test_an_unchanged_plan_composition_publishes_nothing(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Состав тарифа передан тем же: ни одна группа не вошла и не вышла — задач нет."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        group = await group_of(conn, stand.node_id)
        plan_id = await plan_of(conn, group)
        await PlanService(await get_pool()).update(
            plan_id, PlanPatch(access_group_ids=[group], profiles=PROFILES)
        )
        kind = composition_module.PUBLISH_NODE_JOB
        assert await _jobs_of(conn, kind, "node_id", stand.node_id) == []


async def test_a_new_plan_publishes_nothing(pg_dsn: str, migrate_env: dict[str, str]) -> None:
    """Новый тариф с группами составы нод не меняет — подписок на него ещё нет: задач
    ``composition.publish_node`` его создание не ставит."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        group = await group_of(conn, stand.node_id)
        await PlanService(await get_pool()).create(
            PlanIn(
                name=unique_name("c29b"),
                duration_days=30,
                access_group_ids=[group],
                profiles=PROFILES,
            )
        )
        kind = composition_module.PUBLISH_NODE_JOB
        assert await _jobs_of(conn, kind, "node_id", stand.node_id) == []


async def test_deleting_a_plan_group_publishes_its_nodes(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Удаление группы доступа уносит каскадом её связь с тарифом: на каждую ноду группы в той же
    транзакции ставится ``composition.publish_node``, и после исполнения пользователи тарифа на
    этих нодах — ``removed``. Группа вне тарифов публикацию не ставит."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        node = stand.node_id
        other = await insert_node(conn, like=node)
        group = await group_of(conn, node, other)
        user_id = await subscribe(conn, await plan_of(conn, group))
        pool = await get_pool()
        await CompositionService(pool).publish_user(user_id)
        idle = await group_of(conn, node)
        groups = GroupService(pool, RecordingHours())
        await groups.delete_access(idle)
        kind = composition_module.PUBLISH_NODE_JOB
        assert await _jobs_of(conn, kind, "node_id", node) == [], "группа вне тарифов"
        await groups.delete_access(group)
        for node_id in (node, other):
            assert len(await _jobs_of(conn, kind, "node_id", node_id)) == 1, node_id
        await run_publications(conn)
        for node_id in (node, other):
            row = await row_of(conn, node_id, user_id)
            assert row is not None and row["state"] == "removed", node_id


async def test_tc_e2e_03_a_deleted_user_gets_only_removed_rows(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Удалённый пользователь (``users.status = 'deleted'``) при действующей подписке получает на
    всех нодах только строки ``removed``, его credentials удалены; ``remove_user`` ставит
    публикацию в транзакции вызывающего и вне транзакции отказывает."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        node = stand.node_id
        other = await insert_node(conn, like=node)
        user_id = await subscribe(conn, await plan_of(conn, await group_of(conn, node, other)))
        pool = await get_pool()
        service = CompositionService(pool)
        await service.publish_user(user_id)
        async with pool.acquire() as bare:
            with pytest.raises(RuntimeError, match="транзакции"):
                await service.remove_user(bare, user_id)
        async with transaction(pool) as tx:
            await tx.execute("update users set status = 'deleted' where id = $1", user_id)
            await service.remove_user(tx, user_id)
        await run_publications(conn)
        for node_id in (node, other):
            row = await row_of(conn, node_id, user_id)
            assert row is not None and row["state"] == "removed", node_id
            assert not row["has_credentials"]
        await service.publish_user(user_id)
        for node_id in (node, other):
            row = await row_of(conn, node_id, user_id)
            assert row is not None and row["state"] == "removed", "повтор доступ не возвращает"


async def test_publish_node_takes_each_user_lock_before_the_node_lock(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """``publish_node`` пишет строку пользователя под его блокировкой публикации, взятой до
    блокировки строки ноды (001.29, роаст раунд 3, R3-1): пока блокировку пользователя держит
    другой, строки этого пользователя нет, а строка ноды свободна (иначе ``publish_user`` того же
    пользователя, ждущий ноду, замкнул бы круг); после снятия — строки всех пользователей."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        node = stand.node_id
        plan_id = await plan_of(conn, await group_of(conn, node))
        users = [await subscribe(conn, plan_id) for _ in range(2)]
        held = max(users)
        holder = await asyncpg.connect(pg_dsn)
        probe = await asyncpg.connect(pg_dsn)
        try:
            await holder.execute(
                "select pg_advisory_lock($1, hashtext($2))", PUBLISH_LOCK_CLASS, str(held)
            )
            pool = await get_pool()

            async def publish() -> None:
                async with pool.acquire() as worker:
                    await CompositionService(pool).publish_node_on(worker, node)

            running = asyncio.create_task(publish())
            await wait_blocked_by(conn, await holder.fetchval("select pg_backend_pid()"))
            assert await row_of(conn, node, held) is None, "строка записана без блокировки"
            async with probe.transaction():
                free = await probe.fetchval(
                    "select id from nodes where id = $1 for no key update nowait", node
                )
            assert free == node
            await holder.execute(
                "select pg_advisory_unlock($1, hashtext($2))", PUBLISH_LOCK_CLASS, str(held)
            )
            await asyncio.wait_for(running, timeout=10)
        finally:
            await holder.close()
            await probe.close()
        for user in users:
            row = await row_of(conn, node, user)
            assert row is not None and row["state"] == "active", user


async def test_publish_node_wakes_its_node_once_after_the_last_commit(
    pg_dsn: str, migrate_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Порция состава ноды будит ноду один раз, после коммита строки последнего пользователя
    порции, номером этой строки — текущим номером ноды (порцию из нескольких — ниже,
    ``test_every_batch_wakes_its_node``)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        node = stand.node_id
        plan_id = await plan_of(conn, await group_of(conn, node))
        users = [await subscribe(conn, plan_id) for _ in range(3)]
        woken: list[tuple[str, str, int]] = []

        class RecordingRedis:
            async def publish(self, channel: str, message: str) -> int:
                committed = await conn.fetchval(
                    "select count(*) from node_user_state where node_id = $1 "
                    "and user_id = any($2::uuid[])",
                    node,
                    users,
                )
                woken.append((channel, message, committed))
                return 0

        async def recording() -> RecordingRedis:
            return RecordingRedis()

        monkeypatch.setattr(composition_module, "get_redis", recording)
        await CompositionService(await get_pool()).publish_node(node)
        desired = await conn.fetchval("select desired_users_seq from nodes where id = $1", node)
        assert woken == [(f"node:{node}", str(desired), len(users))]


async def _foreign_key() -> FieldCipher:
    raise CipherMismatch("ключ процесса не тот (подставлено тестом)")


async def test_a_foreign_process_key_still_publishes_the_node_revocations(
    pg_dsn: str, migrate_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ключ процесса не тот (WI-38): состав ноды пишет строки без доступа и будит ноду, строку
    ``active`` пропускает и отказывает ``CipherMismatch`` — как публикация пользователя."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        node = stand.node_id
        plan_id = await plan_of(conn, await group_of(conn, node))
        active = await subscribe(conn, plan_id)
        paused = await subscribe(conn, plan_id, state="suspended_admin")
        woken: list[str] = []

        class RecordingRedis:
            async def publish(self, channel: str, message: str) -> int:
                woken.append(channel)
                return 0

        async def recording() -> RecordingRedis:
            return RecordingRedis()

        monkeypatch.setattr(composition_module, "get_redis", recording)
        monkeypatch.setattr(composition_module, "checked_cipher", _foreign_key)
        pool = await get_pool()
        async with pool.acquire() as worker:
            with pytest.raises(CipherMismatch):
                await CompositionService(pool).publish_node_on(worker, node)
        row = await row_of(conn, node, paused)
        assert row is not None and row["state"] == "suspended_admin"
        assert await row_of(conn, node, active) is None
        assert woken == [f"node:{node}"]


# --- раунд 1 роаста: транзакция постановки, сериализация, порции -------------------------------

_TRANSITIONS = ["activate", "renew", "change_plan", "add_traffic", "expire", "set_state"]


@pytest.mark.parametrize("transition", _TRANSITIONS)
async def test_every_transition_commits_its_publication_with_itself(
    pg_dsn: str, migrate_env: dict[str, str], transition: str
) -> None:
    """Каждый из шести переходов коммитит задачу публикации в своей же транзакции: ``xmin``
    строки задачи равен ``xmin`` строки, которую пишет переход, а хвост ключа — номер этой
    транзакции (младшие 32 бита ``pg_current_xact_id`` — ``xmin``). Постановка в отдельной
    транзакции после коммита перехода — та самая потеря отзыва при крахе (Н-14) — даёт другой
    номер (роаст 001.29b, G-1, G-6)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        plan_id = await plan_of(conn, await group_of(conn, stand.node_id))
        other = await plan_of(conn)
        user_id = (
            await insert_user(conn) if transition == "activate" else await subscribe(conn, plan_id)
        )
        service = SubscriptionService(await get_pool())
        steps: dict[str, Callable[[], Awaitable[object]]] = {
            "activate": lambda: service.activate(user_id, plan_id, "admin"),
            "renew": lambda: service.renew(user_id, "admin"),
            "change_plan": lambda: service.change_plan(user_id, other, uuid.uuid4()),
            "add_traffic": lambda: service.add_traffic(user_id, 1024, None, None),
            "expire": lambda: service.expire(user_id),
            "set_state": lambda: service.set_state(user_id, "suspended_admin", "проверка"),
        }
        await steps[transition]()
        written = await conn.fetchval(
            "select p.xmin::text::bigint from subscriptions s "
            "join subscription_periods p on p.id = s.current_period_id where s.user_id = $1"
            if transition == "add_traffic"
            else "select xmin::text::bigint from subscriptions where user_id = $1",
            user_id,
        )
        placed = await _jobs_of(conn, composition_module.PUBLISH_USER_JOB, "user_id", user_id)
        assert len(placed) == 1
        assert placed[0]["xmin"] == written, "задача закоммичена не транзакцией перехода"
        assert int(placed[0]["idempotency_key"].rsplit(":", 1)[1]) % 2**32 == written


class _PausesBeforeSecondNode(CompositionService):
    """Постановка останавливается перед второй нодой операции: первая уже поставлена."""

    def __init__(self, pool: Any) -> None:
        super().__init__(pool)
        self.calls = 0
        self.paused, self.release = asyncio.Event(), asyncio.Event()

    async def enqueue_publish_node(self, conn: Any, node_id: uuid.UUID) -> None:
        self.calls += 1
        if self.calls == 2:
            self.paused.set()
            await self.release.wait()
        await super().enqueue_publish_node(conn, node_id)


async def _node_jobs(conn: asyncpg.Connection, nodes: list[uuid.UUID]) -> int:
    count: int = await conn.fetchval(
        "select count(*) from jobs where type = $1 and payload->>'node_id' = any($2::text[])",
        composition_module.PUBLISH_NODE_JOB,
        [str(node) for node in nodes],
    )
    return count


@pytest.mark.parametrize("operation", ["plan_update", "group_delete"])
async def test_node_publications_commit_with_their_operation(
    pg_dsn: str, migrate_env: dict[str, str], operation: str
) -> None:
    """Задачи ``composition.publish_node`` смены групп тарифа и удаления группы не видны другой
    сессии, пока операция не закоммичена: отдельная транзакция постановки разбудила бы
    исполнителя раньше коммита изменения, и он прочитал бы прежний состав (роаст 001.29b, G-2)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        nodes = [stand.node_id, await insert_node(conn, like=stand.node_id)]
        group = await group_of(conn, *nodes)
        pool = await get_pool()
        composition = _PausesBeforeSecondNode(pool)
        call: Awaitable[object]
        if operation == "plan_update":
            patch = PlanPatch(access_group_ids=[group], profiles=PROFILES)
            call = PlanService(pool, composition).update(await plan_of(conn), patch)
        else:
            await plan_of(conn, group)
            call = GroupService(pool, RecordingHours(), composition).delete_access(group)
        running = asyncio.ensure_future(call)
        await asyncio.wait_for(composition.paused.wait(), timeout=10)
        try:
            visible = await _node_jobs(conn, nodes)
        finally:
            composition.release.set()
        await asyncio.wait_for(running, timeout=10)
        assert visible == 0, "задача publish_node закоммичена раньше своей операции"
        assert await _node_jobs(conn, nodes) == 2


async def test_publish_node_holds_one_user_lock_until_the_pair_commits(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Пока исполнитель ждёт блокировку пользователя, других блокировок публикации он не держит, а
    строки прежних пользователей закоммичены; каждое снятие блокировки идёт после коммита (или
    отката) транзакции пары. Утечка блокировок, их захват всем списком сразу или снятие до
    коммита пары оставили бы ``publish_user`` уже записанного пользователя ждать всю ноду либо
    дали бы ему выбрать кандидатов без незакоммиченной строки (роаст 001.29b, G-3, G-4)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        node = stand.node_id
        plan_id = await plan_of(conn, await group_of(conn, node))
        users = [await subscribe(conn, plan_id) for _ in range(3)]
        held = max(users)
        holder = await asyncpg.connect(pg_dsn)
        statements: list[str] = []
        try:
            await holder.execute(
                "select pg_advisory_lock($1, hashtext($2))", PUBLISH_LOCK_CLASS, str(held)
            )
            pool = await get_pool()
            async with pool.acquire() as worker:
                pid = worker.get_server_pid()

                def log(record: Any) -> None:
                    statements.append(record.query)

                worker.add_query_logger(log)
                try:
                    running = asyncio.create_task(
                        CompositionService(pool).publish_node_on(worker, node)
                    )
                    await wait_blocked_by(conn, await holder.fetchval("select pg_backend_pid()"))
                    granted = await conn.fetchval(
                        "select count(*) from pg_locks where locktype = 'advisory' and pid = $1 "
                        "and classid = $2 and granted",
                        pid,
                        PUBLISH_LOCK_CLASS,
                    )
                    earlier = [await row_of(conn, node, user) for user in users if user != held]
                    await holder.execute(
                        "select pg_advisory_unlock($1, hashtext($2))",
                        PUBLISH_LOCK_CLASS,
                        str(held),
                    )
                    await asyncio.wait_for(running, timeout=10)
                finally:
                    worker.remove_query_logger(log)
        finally:
            await holder.close()
        assert granted == 0, f"исполнитель держит {granted} блокировок публикации, пока ждёт"
        assert all(row is not None for row in earlier), "прежние пользователи не закоммичены"
        events = [
            "UNLOCK" if "pg_advisory_unlock" in query else query.split()[0].rstrip(";").upper()
            for query in statements
            if "pg_advisory_unlock" in query
            or query.lstrip().upper().startswith(("COMMIT", "ROLLBACK"))
        ]
        unlocks = [i for i, event in enumerate(events) if event == "UNLOCK"]
        assert len(unlocks) == len(users)
        assert all(i > 0 and events[i - 1] in ("COMMIT", "ROLLBACK") for i in unlocks), events


async def test_a_blocked_user_keeps_his_row_and_credentials(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Группу не делит только удалённый: заблокированный (``users.status = 'blocked'``, вместе с
    ``suspended_admin``, 001.50) сохраняет строку ``suspended_admin`` и credentials пары — иначе
    при разблокировке получил бы новые ключи на всех нодах (роаст 001.29b, G-7)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        plan_id = await plan_of(conn, await group_of(conn, stand.node_id))
        user_id = await subscribe(conn, plan_id, state="suspended_admin")
        await conn.execute("update users set status = 'blocked' where id = $1", user_id)
        await CompositionService(await get_pool()).publish_user(user_id)
        row = await row_of(conn, stand.node_id, user_id)
        assert row is not None and row["state"] == "suspended_admin" and row["has_credentials"]


async def test_publish_node_reads_its_users_after_an_in_flight_pair_write(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Порция читает кандидатов под блокировкой строки ноды: публикация пользователя, прочитавшая
    пару до смены состава тарифа и ещё не закоммиченная, держит строку ноды; состав ноды ждёт её
    коммита, видит её строку и снимает доступ, который тариф больше не даёт. Без блокировки
    порция не увидела бы пользователя ни в группах тарифа, ни среди строк ноды, и строка
    ``active`` осталась бы (роаст 001.29b, L1)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        node = stand.node_id
        plan_id = await plan_of(conn, await group_of(conn, node))
        user_id = await subscribe(conn, plan_id)
        pool = await get_pool()
        holder = await asyncpg.connect(pg_dsn)
        try:
            await holder.execute(
                "select pg_advisory_lock($1, hashtext($2))", PUBLISH_LOCK_CLASS, str(user_id)
            )
            pair = holder.transaction()
            await pair.start()
            await holder.execute("select id from nodes where id = $1 for no key update", node)
            await holder.execute(
                "insert into node_user_state (node_id, user_id, state, blocked_ips, updated_seq) "
                "values ($1, $2, 'active', '{}', $3)",
                node,
                user_id,
                await next_users_seq(holder, node),
            )
            await PlanService(pool).update(
                plan_id, PlanPatch(access_group_ids=[], profiles=PROFILES)
            )

            async def publish() -> None:
                async with pool.acquire() as worker:
                    await run_publications(worker)

            running = asyncio.create_task(publish())
            await wait_blocked_by(conn, await holder.fetchval("select pg_backend_pid()"))
            await pair.commit()
            await holder.execute(
                "select pg_advisory_unlock($1, hashtext($2))", PUBLISH_LOCK_CLASS, str(user_id)
            )
            await asyncio.wait_for(running, timeout=10)
        finally:
            await holder.close()
        row = await row_of(conn, node, user_id)
        assert row is not None and row["state"] == "removed"


async def test_deleting_a_group_waits_for_a_plan_link_being_added(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Удаление группы блокирует её строку до чтения её тарифов: незакоммиченная вставка группы в
    тариф держит строку ``KEY SHARE``, удаление ждёт её коммита и ставит публикацию нод группы;
    иначе прочитало бы «в тарифах её нет», и связь, ушедшая каскадом, осталась бы без публикации
    (роаст 001.29b, L2)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        group = await group_of(conn, stand.node_id)
        plan_id = await plan_of(conn)
        holder = await asyncpg.connect(pg_dsn)
        try:
            link = holder.transaction()
            await link.start()
            await holder.execute(
                "insert into plan_access_groups (plan_id, access_group_id) values ($1, $2)",
                plan_id,
                group,
            )
            groups = GroupService(await get_pool(), RecordingHours())
            deleting = asyncio.create_task(groups.delete_access(group))
            await wait_blocked_by(conn, await holder.fetchval("select pg_backend_pid()"))
            await link.commit()
            await asyncio.wait_for(deleting, timeout=10)
        finally:
            await holder.close()
        assert await _node_jobs(conn, [stand.node_id]) == 1


@pytest.mark.parametrize("change", ["leaves", "enters"])
async def test_a_plan_group_change_waits_for_a_node_joining_the_group(
    pg_dsn: str, migrate_env: dict[str, str], change: str
) -> None:
    """Смена групп тарифа блокирует строки прежних и новых групп до чтения их нод: нода, которую
    одновременно вводят в группу (незакоммиченная вставка держит строку группы ``KEY SHARE``),
    попадает в публикацию — и когда группа выходит из тарифа, и когда входит; иначе её состав
    остался бы прежним (роаст 001.29b, L8; раунд 2, R2-4)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        node = stand.node_id
        joining = await insert_node(conn, like=node)
        group = await group_of(conn, node)
        plan_id = await plan_of(conn, group) if change == "leaves" else await plan_of(conn)
        holder = await asyncpg.connect(pg_dsn)
        try:
            join = holder.transaction()
            await join.start()
            await holder.execute(
                "insert into node_access_groups (node_id, access_group_id) values ($1, $2)",
                joining,
                group,
            )
            groups = [] if change == "leaves" else [group]
            patch = PlanPatch(access_group_ids=groups, profiles=PROFILES)
            updating = asyncio.create_task(PlanService(await get_pool()).update(plan_id, patch))
            await wait_blocked_by(conn, await holder.fetchval("select pg_backend_pid()"))
            await join.commit()
            await asyncio.wait_for(updating, timeout=10)
        finally:
            await holder.close()
        assert await _node_jobs(conn, [node, joining]) == 2


async def test_publish_node_goes_in_batches(
    pg_dsn: str, migrate_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Состав ноды пишется порциями по ``PUBLISH_NODE_BATCH`` пользователей (решение заказчика
    2026-11-05): порция ставит продолжение с ключом ``publish_node:{node_id}:after:{user_id}``
    в конец очереди ``critical``, и публикация пользователя, поставленная раньше продолжения,
    исполняется между порциями; цепочка пишет строки всех пользователей ноды (роаст 001.29b, L3)."""
    monkeypatch.setattr(composition_module, "PUBLISH_NODE_BATCH", 2)
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        node = stand.node_id
        plan_id = await plan_of(conn, await group_of(conn, node))
        users = sorted([await subscribe(conn, plan_id) for _ in range(5)])
        revoked = await subscribe(conn, await plan_of(conn), state="suspended_admin")
        pool = await get_pool()
        service = CompositionService(pool)
        async with transaction(pool) as tx:
            await service.enqueue_publish_node(tx, node)
        async with transaction(pool) as tx:
            await service.enqueue_publish_user(tx, revoked)
        executed = await run_publications(conn)
        kinds = [(job.type, job.payload.get("after")) for job in executed]
        node_kind, user_kind = (
            composition_module.PUBLISH_NODE_JOB,
            composition_module.PUBLISH_USER_JOB,
        )
        assert kinds == [
            (node_kind, None),
            (user_kind, None),
            (node_kind, str(users[1])),
            (node_kind, str(users[3])),
        ]
        assert executed[2].idempotency_key == f"publish_node:{node}:after:{users[1]}"
        for user in users:
            row = await row_of(conn, node, user)
            assert row is not None and row["state"] == "active", user


class _NodeVanishes(CompositionService):
    """Строки ноды нет для ``_lock_node``: после ``survives`` блокировок — любой ноды, а нода
    ``gone`` — всегда."""

    def __init__(
        self, pool: Any, *, survives: int | None = None, gone: uuid.UUID | None = None
    ) -> None:
        super().__init__(pool)
        self.locks, self.survives, self.gone = 0, survives, gone

    async def _lock_node(self, conn: Any, node_id: uuid.UUID) -> bool:
        self.locks += 1
        if node_id == self.gone or (self.survives is not None and self.locks > self.survives):
            return False
        return await super()._lock_node(conn, node_id)


async def test_a_vanished_node_stops_its_batch_and_is_skipped_by_a_user(
    pg_dsn: str, migrate_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Нода исчезла посреди порции: ``publish_node`` останавливается — без пустых блокировок
    остальных пользователей и без продолжения; ``publish_user`` пропускает исчезнувшую ноду и
    пишет остальные (роаст 001.29b, L7)."""
    monkeypatch.setattr(composition_module, "PUBLISH_NODE_BATCH", 3)
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        node = stand.node_id
        other = await insert_node(conn, like=node)
        plan_id = await plan_of(conn, await group_of(conn, node, other))
        for _ in range(4):
            await subscribe(conn, plan_id)
        pool = await get_pool()
        vanishing = _NodeVanishes(pool, survives=2)  # чтение кандидатов и первый пользователь
        await vanishing.publish_node(node)
        assert vanishing.locks == 3, "после исчезновения ноды порция продолжилась"
        assert await _node_jobs(conn, [node]) == 0, "продолжение исчезнувшей ноды"
        user_id = await subscribe(conn, plan_id)
        first, second = sorted((node, other))
        await _NodeVanishes(pool, gone=first).publish_user(user_id)
        assert await row_of(conn, first, user_id) is None
        row = await row_of(conn, second, user_id)
        assert row is not None and row["state"] == "active", "нода после исчезнувшей пропущена"


# --- раунд 2 роаста: порции, блокировки групп, транзакция постановки ---------------------------


def _recording_redis(woken: list[Any], probe: Callable[[], Awaitable[Any]] | None = None) -> Any:
    """Подмена ``get_redis``: пробуждения пишутся в ``woken`` — канал, сообщение и, если задан
    ``probe``, его результат на момент пробуждения."""

    class RecordingRedis:
        async def publish(self, channel: str, message: str) -> int:
            woken.append((channel, message, await probe() if probe else None))
            return 0

    async def recording() -> RecordingRedis:
        return RecordingRedis()

    return recording


async def test_every_batch_wakes_its_node(
    pg_dsn: str, migrate_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Каждая порция будит ноду после своего последнего коммита, а не только последняя порция
    цепочки: отзывы первой порции не ждут остальных (бюджет Н-13 «запись строки, ``PUBLISH``»;
    роаст 001.29b, раунд 2, R2-1)."""
    monkeypatch.setattr(composition_module, "PUBLISH_NODE_BATCH", 2)
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        node = stand.node_id
        plan_id = await plan_of(conn, await group_of(conn, node))
        users = [await subscribe(conn, plan_id) for _ in range(3)]

        async def committed() -> Any:
            return await conn.fetchval(
                "select count(*) from node_user_state where node_id = $1 "
                "and user_id = any($2::uuid[])",
                node,
                users,
            )

        woken: list[Any] = []
        monkeypatch.setattr(composition_module, "get_redis", _recording_redis(woken, committed))
        pool = await get_pool()
        async with transaction(pool) as tx:
            await CompositionService(pool).enqueue_publish_node(tx, node)
        assert len(await run_publications(conn)) == 2
        assert [(channel, rows) for channel, _, rows in woken] == [
            (f"node:{node}", 2),
            (f"node:{node}", 3),
        ]


async def test_a_foreign_process_key_still_continues_the_node_chain(
    pg_dsn: str, migrate_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ключ процесса не тот (WI-38): порция, пропустившая строку ``active``, всё равно ставит
    продолжение до отказа ``CipherMismatch`` — строки без доступа следующих порций пишутся
    (примечания задачи, общий сценарий; роаст 001.29b, раунд 2, R2-2)."""
    monkeypatch.setattr(composition_module, "PUBLISH_NODE_BATCH", 2)
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        node = stand.node_id
        plan_id = await plan_of(conn, await group_of(conn, node))
        users = sorted([await subscribe(conn, plan_id) for _ in range(3)])
        for user in users[1:]:  # первая порция: active и без доступа, вторая — без доступа
            await conn.execute(
                "update subscriptions set state = 'suspended_admin' where user_id = $1", user
            )
        monkeypatch.setattr(composition_module, "get_redis", _recording_redis([]))
        monkeypatch.setattr(composition_module, "checked_cipher", _foreign_key)
        pool = await get_pool()
        service = CompositionService(pool)
        async with pool.acquire() as worker:
            with pytest.raises(CipherMismatch):
                await service.publish_node_on(worker, node)
            placed = await _jobs_of(conn, composition_module.PUBLISH_NODE_JOB, "node_id", node)
            assert [job["idempotency_key"] for job in placed] == [
                f"publish_node:{node}:after:{users[1]}"
            ]
            await service.publish_node_on(worker, node, users[1])
        assert await row_of(conn, node, users[0]) is None
        row = await row_of(conn, node, users[2])
        assert row is not None and row["state"] == "suspended_admin"


async def test_the_production_batch_is_200_users(
    pg_dsn: str, migrate_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Порция — 200 пользователей (критерий приёмки, решение заказчика 2026-11-05): нода с 201
    пользователем — 200 строк первой порцией и продолжение после двухсотого (роаст 001.29b,
    раунд 2, R2-5)."""
    assert composition_module.PUBLISH_NODE_BATCH == 200
    monkeypatch.setattr(composition_module, "get_redis", _recording_redis([]))
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        node = stand.node_id
        plan_id = await plan_of(conn, await group_of(conn, node))
        users = sorted([await subscribe(conn, plan_id) for _ in range(201)])
        pool = await get_pool()
        async with pool.acquire() as worker:
            await CompositionService(pool).publish_node_on(worker, node)
        written = await conn.fetchval(
            "select count(*) from node_user_state where node_id = $1", node
        )
        assert written == 200
        placed = await _jobs_of(conn, composition_module.PUBLISH_NODE_JOB, "node_id", node)
        assert [job["idempotency_key"] for job in placed] == [
            f"publish_node:{node}:after:{users[199]}"
        ]


async def test_a_full_last_batch_ends_the_chain(
    pg_dsn: str, migrate_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Продолжение ставится, только если пользователи остались: число пользователей, кратное
    порции, заканчивает цепочку последней полной порцией без пустой задачи (роаст 001.29b,
    раунд 2, R2-6)."""
    monkeypatch.setattr(composition_module, "PUBLISH_NODE_BATCH", 2)
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        node = stand.node_id
        plan_id = await plan_of(conn, await group_of(conn, node))
        users = sorted([await subscribe(conn, plan_id) for _ in range(4)])
        pool = await get_pool()
        async with transaction(pool) as tx:
            await CompositionService(pool).enqueue_publish_node(tx, node)
        executed = await run_publications(conn)
        assert [job.payload.get("after") for job in executed] == [None, str(users[1])]


async def _groups_against_order(conn: asyncpg.Connection) -> tuple[uuid.UUID, uuid.UUID]:
    """Две группы ``(меньшая, большая)`` по ``id``, у которых порядок множества Python и порядок
    строк в куче обратны порядку ``id``: запрос ``[большая, меньшая]`` отделяет запись по
    возрастанию ``id`` от порядка запроса и от ``set`` без сортировки, а блокировку
    ``order by id`` — от блокировки без ``order by`` (роаст 001.29b, раунд 3, R3-G2, R3-G3)."""
    while True:
        small, large = sorted([uuid.uuid4(), uuid.uuid4()])
        if list(set([large, small])) == [large, small]:
            break
    for group_id in (large, small):  # большая вставляется первой — в куче раньше меньшей
        await conn.execute(
            "insert into access_groups (id, name) values ($1, $2)", group_id, unique_name("c29")
        )
    heap = await conn.fetch(
        "select id from access_groups where id = any($1::uuid[]) order by ctid", [small, large]
    )
    assert [row["id"] for row in heap] == [large, small], "порядок кучи совпал с порядком id"
    return small, large


async def _wait_for_group_waiters(conn: asyncpg.Connection, holder_pid: int, count: int) -> None:
    """Дождаться ``count`` сессий, ждущих блокировку в запросе к группам или их связям, кроме
    держателя ``holder_pid``: второй ждущий строки встаёт за блокировкой кортежа первого, а не за
    держателем, и ``wait_blocked_by`` его не различает."""
    for _ in range(400):
        waiting = await conn.fetchval(
            "select count(*) from pg_stat_activity where datname = current_database() "
            "and wait_event_type = 'Lock' and query like '%access_groups%' and pid <> $1",
            holder_pid,
        )
        if waiting >= count:
            return
        await asyncio.sleep(0.025)
    raise AssertionError(f"за держателем {holder_pid} не встали {count} сессии")


async def _creation(
    conn: asyncpg.Connection, node_id: uuid.UUID, operation: str, groups: list[uuid.UUID]
) -> Awaitable[Any]:
    """Создание тарифа (``plan_create``) или ноды по образцу ноды стенда (``node_create``) с
    группами ``groups`` — ещё не запущенное."""
    pool = await get_pool()
    if operation == "plan_create":
        plan = PlanIn(
            name=unique_name("c29b"), duration_days=30, access_group_ids=groups, profiles=PROFILES
        )
        return PlanService(pool).create(plan)
    like = await conn.fetchrow(
        "select country, city, provider, host(public_ipv4) as public_ipv4, "
        "billing_group_id, bandwidth_mbps, max_conn_per_ip from nodes where id = $1",
        node_id,
    )
    code = unique_name("n29b")
    node = NodeIn(code=code, name=code, access_group_ids=groups, **dict(like))
    return NodeService(pool).create(node, uuid.uuid4())


async def test_two_plan_updates_with_shared_groups_do_not_deadlock(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Строки групп блокируются по возрастанию ``id``: два изменения тарифов с одними и теми же
    группами в разном порядке, отпущенные вместе, оба проходят (роаст 001.29b, раунд 2, R2-7)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        first, second = sorted([await group_of(conn), await group_of(conn)])
        plans = [await plan_of(conn), await plan_of(conn)]
        holder = await asyncpg.connect(pg_dsn)
        try:
            hold = holder.transaction()
            await hold.start()
            await holder.execute(
                "select id from access_groups where id = any($1::uuid[]) for update",
                [first, second],
            )
            service = PlanService(await get_pool())
            updates = [
                asyncio.create_task(
                    service.update(plan_id, PlanPatch(access_group_ids=groups, profiles=PROFILES))
                )
                for plan_id, groups in zip(plans, ([first, second], [second, first]), strict=True)
            ]
            await _wait_for_group_waiters(conn, await holder.fetchval("select pg_backend_pid()"), 2)
            await hold.commit()
            results = await asyncio.wait_for(
                asyncio.gather(*updates, return_exceptions=True), timeout=15
            )
        finally:
            await holder.close()
        assert [repr(r) for r in results if isinstance(r, BaseException)] == []


class _SeesItsOperation(CompositionService):
    """При каждой постановке ``composition.publish_node`` читает ``query`` на переданном
    подключении и на ``outside`` — в другой сессии: незакоммиченное изменение операции видно только
    в её транзакции."""

    def __init__(self, pool: Any, outside: asyncpg.Connection, query: str, arg: uuid.UUID) -> None:
        super().__init__(pool)
        self.outside, self.query, self.arg = outside, query, arg
        self.seen: list[tuple[int, int]] = []

    async def enqueue_publish_node(self, conn: Any, node_id: uuid.UUID) -> None:
        mine = await conn.fetchval(self.query, self.arg)
        self.seen.append((mine, await self.outside.fetchval(self.query, self.arg)))
        await super().enqueue_publish_node(conn, node_id)


@pytest.mark.parametrize("operation", ["plan_update", "group_delete"])
async def test_node_publications_are_placed_in_the_operation_transaction(
    pg_dsn: str, migrate_env: dict[str, str], operation: str
) -> None:
    """``composition.publish_node`` ставится в транзакции операции до её коммита: подключение
    постановки видит незакоммиченное изменение, другая сессия — ещё нет. Отдельная транзакция
    постановки до коммита операции изменения не видит (роаст 001.29b, раунд 2, R2-3), после
    коммита — его видит и другая сессия (раунд 3, R3-G1)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        nodes = [stand.node_id, await insert_node(conn, like=stand.node_id)]
        group = await group_of(conn, *nodes)
        pool = await get_pool()
        if operation == "plan_update":
            plan_id = await plan_of(conn)
            composition = _SeesItsOperation(
                pool, conn, "select count(*) from plan_access_groups where plan_id = $1", plan_id
            )
            await PlanService(pool, composition).update(
                plan_id, PlanPatch(access_group_ids=[group], profiles=PROFILES)
            )
            assert composition.seen == [(1, 0), (1, 0)]
        else:
            await plan_of(conn, group)
            composition = _SeesItsOperation(
                pool, conn, "select count(*) from access_groups where id = $1", group
            )
            await GroupService(pool, RecordingHours(), composition).delete_access(group)
            assert composition.seen == [(0, 1), (0, 1)]


async def test_two_node_placements_in_one_transaction_are_one_job(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Ключ ``publish_node:{node_id}:{xid}`` (§5.4): хвост — номер транзакции постановки, и
    вторая постановка той же ноды той же транзакцией — та же задача (роаст 001.29b, раунд 2,
    R2-8)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        node = stand.node_id
        pool = await get_pool()
        service = CompositionService(pool)
        async with transaction(pool) as tx:
            xid = await tx.fetchval("select pg_current_xact_id()::text")
            await service.enqueue_publish_node(tx, node)
            await service.enqueue_publish_node(tx, node)
        placed = await _jobs_of(conn, composition_module.PUBLISH_NODE_JOB, "node_id", node)
        assert [job["idempotency_key"] for job in placed] == [f"publish_node:{node}:{xid}"]


@pytest.mark.parametrize("operation", ["plan_create", "node_create"])
async def test_group_links_are_written_in_id_order(
    pg_dsn: str, migrate_env: dict[str, str], operation: str
) -> None:
    """Связи с группами пишутся по возрастанию ``id`` группы — в порядке блокировок ``FOR UPDATE``
    изменения тарифа: проверка внешнего ключа вставки держит строку группы ``KEY SHARE``, и
    вставка в порядке запроса против изменения тарифа с теми же группами дала бы взаимную
    блокировку (роаст 001.29b, раунд 2, R2-L1). Порядок запроса и порядок множества обратны
    порядку ``id`` (раунд 3, R3-G3). Строку меньшей группы держит удаление в другой сессии:
    создание ждёт его, не держа строку большей, и после коммита удаления — 422."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        small, large = await _groups_against_order(conn)
        call = await _creation(conn, stand.node_id, operation, [large, small])
        holder = await asyncpg.connect(pg_dsn)
        try:
            deleting = holder.transaction()
            await deleting.start()
            await holder.execute("delete from access_groups where id = $1", small)
            creating = asyncio.ensure_future(call)
            await wait_blocked_by(conn, await holder.fetchval("select pg_backend_pid()"))
            try:
                async with conn.transaction():
                    await conn.execute(
                        "select id from access_groups where id = $1 for update nowait", large
                    )
                held = False
            except asyncpg.LockNotAvailableError:
                held = True
            await deleting.commit()
            with pytest.raises(ApiError, match="группа доступа не найдена"):
                await asyncio.wait_for(creating, timeout=10)
        finally:
            await holder.close()
        assert not held, "создание держит строку большей группы, ожидая меньшую"


# --- раунд 3 роаста: порядок блокировки изменения против создания, повтор группы --------------


@pytest.mark.parametrize("operation", ["plan_create", "node_create"])
async def test_a_plan_update_and_a_creation_with_shared_groups_do_not_deadlock(
    pg_dsn: str, migrate_env: dict[str, str], operation: str
) -> None:
    """Изменение тарифа блокирует строки групп по возрастанию ``id`` — в порядке вставки связей
    созданием тарифа или ноды (критерий приёмки). Держатель ``FOR UPDATE`` большей группы, за ним
    изменение тарифа (держит меньшую), за ним создание с теми же группами; после коммита
    держателя оба проходят. Блокировка по убыванию ``id`` или без ``order by`` при обратном
    порядке кучи — взаимная блокировка (роаст 001.29b, раунд 3, R3-G2)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        small, large = await _groups_against_order(conn)
        plan_id = await plan_of(conn)
        call = await _creation(conn, stand.node_id, operation, [large, small])
        holder = await asyncpg.connect(pg_dsn)
        try:
            hold = holder.transaction()
            await hold.start()
            await holder.execute("select id from access_groups where id = $1 for update", large)
            holder_pid = await holder.fetchval("select pg_backend_pid()")
            patch = PlanPatch(access_group_ids=[small, large], profiles=PROFILES)
            updating = asyncio.ensure_future(PlanService(await get_pool()).update(plan_id, patch))
            await wait_blocked_by(conn, holder_pid)
            creating = asyncio.ensure_future(call)
            await _wait_for_group_waiters(conn, holder_pid, 2)
            await hold.commit()
            results = await asyncio.wait_for(
                asyncio.gather(updating, creating, return_exceptions=True), timeout=15
            )
        finally:
            await holder.close()
        assert [repr(r) for r in results if isinstance(r, BaseException)] == []


@pytest.mark.parametrize("operation", ["plan_create", "node_create"])
async def test_a_repeated_group_is_written_once(
    pg_dsn: str, migrate_env: dict[str, str], operation: str
) -> None:
    """Группа, повторённая в запросе, — одна связь, а не 500 нарушения первичного ключа: список
    приводится к множеству до сортировки (роаст 001.29b, раунд 3, R3-G4)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        group = await group_of(conn)
        created = await (await _creation(conn, stand.node_id, operation, [group, group]))
        assert created.access_group_ids == [group]
