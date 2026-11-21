"""Ротация credentials на живой базе (задача 001.29d; R-04; UC-12 шаги 1–3, AC-14, AC-35; Н-32):
отзыв identity ставит в своей транзакции ``composition.rotate_node_credentials``, исполнитель
заменяет ключи всех пар ноды и ставит публикацию её состава; перевыпуск токена ротирует
credentials пользователя на всех его нодах в транзакции вызывающего и ставит его публикацию.
Писатель credentials пары берёт строку её ноды ``FOR NO KEY UPDATE`` раньше записи.

Задачи стоят под подменёнными типами (``tests/conftest.py``), исполняет их тест
(``run_publications``): живые исполнители стенда их не берут.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import asyncpg
import pytest
from app.config import Settings
from app.db.pool import get_pool
from app.domain import composition
from app.domain.composition import CompositionService, credentials_bound
from app.domain.nodes import NodeService
from app.jobs import queue as jobs
from app.jobs.handlers import composition as handlers
from app.jobs.worker import decide
from app.security.crypto import CipherMismatch, FieldCipher

from tests.e2e._composition import (
    cursors,
    group_of,
    plan_of,
    run_publications,
    set_status,
    state,
    subscribe,
)
from tests.e2e._reporting import (
    db_now,
    insert_identity,
    insert_node,
    reporting,
    wait_blocked_by,
)

USERS = 1000  # Н-32: до 1 000 записей на ноду при масштабе О-3
STATES = ("active", "suspended_quota", "suspended_admin", "expired")
N32_SECONDS = 4 * 3600

Keys = dict[uuid.UUID, tuple[bytes, bytes, int]]


async def credentials_of(conn: asyncpg.Connection, node_id: uuid.UUID) -> Keys:
    """Контейнеры и версия credentials каждой пары ноды."""
    rows = await conn.fetch(
        "select user_id, vless_uuid_enc, trojan_password_enc, version "
        "from node_user_credentials where node_id = $1",
        node_id,
    )
    return {
        r["user_id"]: (bytes(r["vless_uuid_enc"]), bytes(r["trojan_password_enc"]), r["version"])
        for r in rows
    }


def opened(
    cipher: FieldCipher, user_id: uuid.UUID, node_id: uuid.UUID, keys: tuple[bytes, bytes, int]
) -> tuple[uuid.UUID, str]:
    """Ключ VLESS и пароль Trojan пары — расшифровкой с привязкой своей пары (WI-38)."""
    vless = cipher.decrypt(keys[0], bound=credentials_bound("vless_uuid_enc", user_id, node_id))
    trojan = cipher.decrypt(
        keys[1], bound=credentials_bound("trojan_password_enc", user_id, node_id)
    )
    return uuid.UUID(bytes=vless), trojan.decode()


async def jobs_of(conn: asyncpg.Connection, kind: str, node_id: uuid.UUID) -> list[Any]:
    rows: list[Any] = await conn.fetch(
        "select idempotency_key, status::text as status, xmin::text::bigint as xmin "
        "from jobs where type = $1 and payload->>'node_id' = $2 order by id",
        kind,
        str(node_id),
    )
    return rows


async def test_tc_e2e_02_revocation_rotates_the_credentials_of_every_pair_of_the_node(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """TC-E2E-02 (UC-12 шаги 1–3, AC-14, Н-32): у ноды 1 000 пар во всех состояниях доступа
    (``active``, ``suspended_*``, ``expired``). Отзыв identity ставит в своей
    транзакции одну задачу ``composition.rotate_node_credentials`` с ключом
    ``rotate_node_credentials:{node}:{xid}``; исполнитель заменяет ключи всех 1 000 пар ноды —
    ``version + 1``, новые VLESS и Trojan, контейнеры с привязкой своей пары — и в той же
    транзакции ставит публикацию состава ноды: после неё строки ноды переопубликованы с новыми
    номерами. Время ротации
    записывается и не больше 4 ч (Н-32). Пары тех же пользователей на другой ноде не меняются
    (постусловие UC-12). Повторный отзыв после завершённой ротации новой не ставит (решения
    заказчика 2026-11-20 и 2026-11-21: перезапуск — только отказавшей)."""
    cipher = FieldCipher.from_settings(Settings.load())
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        other = await insert_node(conn, like=stand.node_id)
        plan_id = await plan_of(conn, await group_of(conn, stand.node_id, other))
        # Пары во всех состояниях доступа: ключи пары снятой подписки тоже утекли с нодой.
        users = [
            await subscribe(conn, plan_id, state=STATES[n % len(STATES)]) for n in range(USERS)
        ]
        # Заблокированный пользователь (``users.status = 'blocked'``) группу делит — его пары тоже.
        await conn.execute("update users set status = 'blocked' where id = any($1)", users[::7])
        pool = await get_pool()
        service = CompositionService(pool)
        await service.publish_node(stand.node_id)
        await service.publish_node(other)
        await run_publications(conn)
        before, untouched = (
            await credentials_of(conn, stand.node_id),
            await credentials_of(conn, other),
        )
        assert len(before) == len(untouched) == USERS
        seq_before = await conn.fetchval(
            "select desired_users_seq from nodes where id = $1", stand.node_id
        )

        await NodeService(pool).revoke_identity(stand.node_id, uuid.uuid4())
        placed = await jobs_of(conn, composition.ROTATE_NODE_JOB, stand.node_id)
        revoked_xmin = await conn.fetchval(
            "select xmin::text::bigint from node_identities where node_id = $1 "
            "and revoked_at is not null",
            stand.node_id,
        )
        assert len(placed) == 1, placed
        assert placed[0]["xmin"] == revoked_xmin, "задача коммитится в транзакции отзыва"
        prefix, _, xid = placed[0]["idempotency_key"].rpartition(":")
        assert prefix == f"rotate_node_credentials:{stand.node_id}"
        assert int(xid) % 2**32 == revoked_xmin, "хвост ключа — номер транзакции отзыва"

        job = await jobs.claim(conn, "critical", "t29d", types=(composition.ROTATE_NODE_JOB,))
        assert job is not None
        started = time.monotonic()
        await handlers.rotate_node_credentials(conn, job)
        elapsed = time.monotonic() - started
        await jobs.complete(conn, job.id, "t29d")
        print(f"\nН-32: ротация {USERS} пар ноды — {elapsed:.3f} с")
        assert elapsed < N32_SECONDS

        after = await credentials_of(conn, stand.node_id)
        assert set(after) == set(before) == set(users)
        for user_id in users:
            old, new = before[user_id], after[user_id]
            assert new[2] == old[2] + 1, "версия пары выросла на единицу"
            old_vless, old_trojan = opened(cipher, user_id, stand.node_id, old)
            new_vless, new_trojan = opened(cipher, user_id, stand.node_id, new)
            assert new_vless != old_vless and new_trojan != old_trojan, "новые ключи"
        assert await credentials_of(conn, other) == untouched, "другая нода не тронута"

        rotated_xmin = await conn.fetch(
            "select distinct xmin::text::bigint as xmin from node_user_credentials "
            "where node_id = $1",
            stand.node_id,
        )
        republish = await jobs_of(conn, composition.PUBLISH_NODE_JOB, stand.node_id)
        assert [r["xmin"] for r in rotated_xmin] == [republish[-1]["xmin"]], (
            "ключи всех пар и постановка публикации ноды — одна транзакция"
        )
        published = await run_publications(conn)
        assert published and {j.type for j in published} == {composition.PUBLISH_NODE_JOB}
        seqs = await conn.fetch(
            "select updated_seq from node_user_state where node_id = $1", stand.node_id
        )
        assert len(seqs) == USERS and all(r["updated_seq"] > seq_before for r in seqs), (
            "строки ноды переопубликованы"
        )

        await NodeService(pool).revoke_identity(stand.node_id, uuid.uuid4())
        assert len(await jobs_of(conn, composition.ROTATE_NODE_JOB, stand.node_id)) == 1, (
            "повторный отзыв после завершённой ротации новую не ставит"
        )


async def test_rejecting_a_pending_node_rotates_too(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Отклонение ноды с несовпавшими признаками (UC-01 A2) — тоже первый отзыв: ротация ставится
    (решение заказчика 2026-11-20 — токен мог утечь)."""
    async with reporting(pg_dsn, migrate_env, users=0, status="pending") as stand:
        await NodeService(await get_pool()).revoke_identity(stand.node_id, uuid.uuid4())
        assert len(await jobs_of(stand.conn, composition.ROTATE_NODE_JOB, stand.node_id)) == 1


async def test_tc_e2e_03_reissue_rotates_the_user_credentials_on_every_node(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """TC-E2E-03 (AC-35): ``rotate_user_credentials(conn, user_id)`` в транзакции вызывающего
    заменяет ключи пользователя на всех трёх его нодах, в том числе в ``offline`` (``version + 1``,
    новые VLESS и Trojan — каждый) и ставит его публикацию в той
    же транзакции; соседа не трогает. После публикации дельта ноды несёт строку пользователя с
    новыми ключами."""
    cipher = FieldCipher.from_settings(Settings.load())
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        other = await insert_node(conn, like=stand.node_id)
        silent = await insert_node(conn, like=stand.node_id)
        await conn.execute("update nodes set status = 'offline' where id = $1", silent)
        nodes = (stand.node_id, other, silent)
        plan_id = await plan_of(conn, await group_of(conn, *nodes))
        user, neighbour = await subscribe(conn, plan_id), await subscribe(conn, plan_id)
        pool = await get_pool()
        service = CompositionService(pool)
        await service.publish_user(user)
        await service.publish_user(neighbour)
        before = {n: await credentials_of(conn, n) for n in nodes}
        seq_before = await conn.fetchval(
            "select desired_users_seq from nodes where id = $1", stand.node_id
        )
        async with conn.transaction():
            rotated = await service.rotate_user_credentials(conn, user)
            xmin = await conn.fetchval("select pg_current_xact_id()::text::bigint % (2^32)::bigint")
        assert rotated == 3, "на всех нодах пользователя, и в offline"
        for node_id in nodes:
            after = await credentials_of(conn, node_id)
            assert after[user][2] == before[node_id][user][2] + 1, node_id
            new_vless, new_trojan = opened(cipher, user, node_id, after[user])
            old_vless, old_trojan = opened(cipher, user, node_id, before[node_id][user])
            assert new_vless != old_vless, ("VLESS", node_id)
            assert new_trojan != old_trojan, ("Trojan", node_id)
            assert after[neighbour] == before[node_id][neighbour], "сосед не тронут"
        placed = await conn.fetch(
            "select xmin::text::bigint as xmin from jobs where type = $1 "
            "and payload->>'user_id' = $2",
            composition.PUBLISH_USER_JOB,
            str(user),
        )
        assert [r["xmin"] for r in placed] == [xmin], "публикация — в транзакции ротации"
        await run_publications(conn)
        response = await state(stand, cursors(seq_before))
        assert response.status_code == 200, response.text
        rows = {r["user_id"]: r for r in response.json()["users"]["rows"]}
        new_vless, new_trojan = opened(
            cipher, user, stand.node_id, (await credentials_of(conn, stand.node_id))[user]
        )
        assert rows[str(user)]["credentials"] == {
            "vless_uuid": str(new_vless),
            "trojan_password": new_trojan,
            "version": before[stand.node_id][user][2] + 1,
        }


@pytest.mark.parametrize("rotation", ["node", "user"])
async def test_a_rotation_writes_a_pair_only_under_its_node_row_lock(
    pg_dsn: str, migrate_env: dict[str, str], rotation: str
) -> None:
    """Писатель credentials пары берёт строку её ноды ``FOR NO KEY UPDATE`` раньше записи —
    как публикация (``_publish_pair``): пока строку ноды держит конкурирующий писатель (его же
    оператор ``_LOCK_NODE``), ротация ноды и ротация пользователя ждут и ничего не пишут; после
    его отката — записывают. Без общего первого шага ротация ноды (пары по ``user_id``) и ротация
    пользователя (пары по ``node_id``) могли бы ждать друг друга по кругу."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        plan_id = await plan_of(conn, await group_of(conn, stand.node_id))
        user = await subscribe(conn, plan_id)
        pool = await get_pool()
        service = CompositionService(pool)
        await service.publish_user(user)
        version = (await credentials_of(conn, stand.node_id))[user][2]
        holder = await asyncpg.connect(pg_dsn)
        try:
            held = holder.transaction()
            await held.start()
            assert await holder.fetchval(composition._LOCK_NODE, stand.node_id) is not None

            async def by_user() -> int:
                async with pool.acquire() as own, own.transaction():
                    return await service.rotate_user_credentials(own, user)

            task = asyncio.create_task(
                service.rotate_node_credentials(stand.node_id) if rotation == "node" else by_user()
            )
            await wait_blocked_by(conn, holder.get_server_pid())
            await asyncio.sleep(0.2)
            assert not task.done(), "ротация не ждёт строку ноды"
            # Строка пары не занята ротацией: запись пары до блокировки строки ноды держала бы
            # её до коммита, и ``skip locked`` её пропустил бы.
            free = await conn.fetchval(
                "select count(*) from (select 1 from node_user_credentials where user_id = $1 "
                "and node_id = $2 for update skip locked) pair",
                user,
                stand.node_id,
            )
            assert free == 1, "ротация записала пару до блокировки строки ноды"
            await held.rollback()
            assert await asyncio.wait_for(task, timeout=10) == 1
        finally:
            await holder.close()
        assert (await credentials_of(conn, stand.node_id))[user][2] == version + 1


@pytest.mark.parametrize("rotation", ["node", "user"])
async def test_a_refused_process_key_writes_nothing(
    pg_dsn: str, migrate_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, rotation: str
) -> None:
    """Ключ процесса не тот (``CipherMismatch`` сверки ``checked_cipher``, WI-38): ротация ноды
    и ротация пользователя отказывают исключением — задача повторяется, перевыпуск откатывается,
    а не закрывается успехом. Ротация ноды (своя транзакция) ни одной пары не записала и
    публикацию не поставила. Шифр берётся сверенный, а не прочитанный из файла без сверки."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        plan_id = await plan_of(conn, await group_of(conn, stand.node_id))
        user = await subscribe(conn, plan_id)
        pool = await get_pool()
        await CompositionService(pool).publish_user(user)
        await run_publications(conn)
        before = await credentials_of(conn, stand.node_id)

        async def refusing() -> FieldCipher:
            raise CipherMismatch("ключ процесса не тот")

        monkeypatch.setattr(composition, "checked_cipher", refusing)
        service = CompositionService(pool)
        with pytest.raises(CipherMismatch):
            if rotation == "node":
                await service.rotate_node_credentials(stand.node_id)
            else:
                async with conn.transaction():
                    await service.rotate_user_credentials(conn, user)
        if rotation == "user":
            # Ротация пользователя идёт в транзакции вызывающего: отказ — это исключение, которое
            # её откатывает; «ничего не записано» здесь держит откат, а не ротация.
            return
        assert await credentials_of(conn, stand.node_id) == before
        placed = await conn.fetchval(
            "select count(*) from jobs where type = any($1::text[]) and status = 'pending'",
            [composition.PUBLISH_NODE_JOB, composition.PUBLISH_USER_JOB],
        )
        assert placed == 0, "публикация не поставлена"


async def test_a_repeat_revocation_restarts_only_an_unfinished_rotation(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Повторный отзыв (решение заказчика 2026-11-21, H-1): пока ротация стоит (``pending``) или
    завершена (``done``) — новой нет; отказавшая (``dead``) — повторный отзыв ставит новую.
    Так администратор перезапускает ротацию после устранения причины отказа."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        nodes_service = NodeService(await get_pool())

        async def statuses() -> list[str]:
            rows = await jobs_of(conn, composition.ROTATE_NODE_JOB, stand.node_id)
            return [r["status"] for r in rows]

        await nodes_service.revoke_identity(stand.node_id, uuid.uuid4())
        assert await statuses() == ["pending"]
        await nodes_service.revoke_identity(stand.node_id, uuid.uuid4())
        assert await statuses() == ["pending"], "стоящая ротация не дублируется"
        await conn.execute(
            "update jobs set status = 'dead' where type = $1 and payload->>'node_id' = $2",
            composition.ROTATE_NODE_JOB,
            str(stand.node_id),
        )
        await nodes_service.revoke_identity(stand.node_id, uuid.uuid4())
        assert await statuses() == ["dead", "pending"], "отказавшая ротация перезапущена"
        await run_publications(conn)
        assert await statuses() == ["dead", "done"]
        await nodes_service.revoke_identity(stand.node_id, uuid.uuid4())
        assert await statuses() == ["dead", "done"], "завершённая ротация не повторяется"


async def test_a_revocation_without_any_identity_rotates_nothing(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Отзыв ноды, у которой identity не было (только аннулирован токен), ротацию не ставит:
    скомпрометированных ключей нет."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        bare = await insert_node(stand.conn, like=stand.node_id)
        await NodeService(await get_pool()).revoke_identity(bare, uuid.uuid4())
        assert await jobs_of(stand.conn, composition.ROTATE_NODE_JOB, bare) == []


class _OrderCipher(FieldCipher):
    """Шифр, запоминающий пару каждого контейнера VLESS в порядке шифрования."""

    def __init__(self, inner: FieldCipher) -> None:
        self.__dict__.update(inner.__dict__)
        self.pairs: list[tuple[uuid.UUID, uuid.UUID]] = []

    def encrypt(self, b: bytes, *, bound: Any) -> bytes:
        _, column, user_id, node_id = json.loads(bound)
        if column == "vless_uuid_enc":
            self.pairs.append((uuid.UUID(user_id), uuid.UUID(node_id)))
        return super().encrypt(b, bound=bound)


async def test_rotations_write_pairs_in_ascending_order(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Порядок записи пар одной транзакцией: ротация ноды — по возрастанию ``user_id``, ротация
    пользователя — по возрастанию ``node_id``. Порядок блокировок строк нод — в
    ``test_a_reissue_locks_node_rows_in_ascending_order``."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        others = [await insert_node(conn, like=stand.node_id) for _ in range(3)]
        plan_id = await plan_of(conn, await group_of(conn, stand.node_id, *others))
        users = [await subscribe(conn, plan_id) for _ in range(5)]
        pool = await get_pool()
        # Пары пишутся в обратном порядке: физический порядок строк противоположен
        # отсортированному, и чтение без ``order by`` не совпало бы с ним.
        for user_id in reversed(users):
            await CompositionService(pool).publish_user(user_id)
        await run_publications(conn)
        spy = _OrderCipher(FieldCipher.from_settings(Settings.load()))
        service = CompositionService(pool, cipher=spy)
        await service.rotate_node_credentials(stand.node_id)
        by_node = [user_id for user_id, _ in spy.pairs]
        assert by_node == sorted(users) and len(by_node) == 5
        spy.pairs.clear()
        async with conn.transaction():
            await service.rotate_user_credentials(conn, users[0])
        by_user = [node_id for _, node_id in spy.pairs]
        assert by_user == sorted([stand.node_id, *others]), "пары — по возрастанию node_id"


_NODE_FREE = (
    "select count(*) from (select 1 from nodes where id = $1 for no key update skip locked) s"
)
_PAIR_FREE = (
    "select count(*) from (select 1 from node_user_credentials where user_id = $1 "
    "and node_id = $2 for update skip locked) s"
)


async def _free(probe: asyncpg.Connection, sql: str, *args: object) -> int:
    """Сколько строк запрос ``… skip locked`` видит свободными с другого подключения."""
    async with probe.transaction():
        count: int = await probe.fetchval(sql, *args)
    return count


async def test_a_reissue_locks_node_rows_in_ascending_order(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Ротация пользователя берёт строки его нод ``FOR NO KEY UPDATE`` в базе по возрастанию
    ``id`` — один порядок у всех писателей, берущих несколько строк нод (001.42, 001.50): старшую
    ноду держит другой писатель, а младшая к этому моменту уже взята ротацией. Мерится база, а
    не порядок вызовов: оператор, взявший все строки сразу по убыванию, встал бы на старшей, не
    взяв младшую."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        other = await insert_node(conn, like=stand.node_id)
        low, high = sorted((stand.node_id, other))
        user = await subscribe(conn, await plan_of(conn, await group_of(conn, low, high)))
        pool = await get_pool()
        service = CompositionService(pool)
        await service.publish_user(user)
        holder = await asyncpg.connect(pg_dsn)
        probe = await asyncpg.connect(pg_dsn)
        try:
            held = holder.transaction()
            await held.start()
            assert await holder.fetchval(composition._LOCK_NODE, high) is not None

            async def by_user() -> int:
                async with pool.acquire() as own, own.transaction():
                    return await service.rotate_user_credentials(own, user)

            task = asyncio.create_task(by_user())
            await wait_blocked_by(conn, holder.get_server_pid())
            await asyncio.sleep(0.2)
            assert not task.done(), "ротация пользователя не ждёт строку старшей ноды"
            low_free = await _free(probe, _NODE_FREE, low)
            assert low_free == 0, "младшая нода не взята раньше старшей"
            await held.rollback()
            assert await asyncio.wait_for(task, timeout=10) == 2
        finally:
            await holder.close()
            await probe.close()


async def test_a_new_compromise_after_a_finished_rotation_rotates_again(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Первый отзыв ставит ротацию всегда — и тогда, когда прежняя ротация ноды завершена:
    пересозданную ноду скомпрометировали снова, и отзыв её новой identity обязан ротировать
    ключи пар, а не опираться на статус прежней задачи."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        nodes_service = NodeService(await get_pool())
        await nodes_service.revoke_identity(stand.node_id, uuid.uuid4())
        await run_publications(conn)
        await insert_identity(conn, stand.node_id, await db_now(conn), generation=2)
        await nodes_service.revoke_identity(stand.node_id, uuid.uuid4())
        rows = await jobs_of(conn, composition.ROTATE_NODE_JOB, stand.node_id)
        assert [r["status"] for r in rows] == ["done", "pending"]


# История задач ротации ноды до повторного отзыва → ставит ли он новую (решение заказчика
# 2026-11-21, H-1): решает последняя задача этой ноды.
RESTART = {
    "failed": (["failed"], True),
    "running": (["running"], False),
    "done-then-dead": (["done", "dead"], True),
    "dead-then-done": (["dead", "done"], False),
}


@pytest.mark.parametrize("history", sorted(RESTART))
async def test_the_restart_follows_the_last_rotation_of_this_node(
    pg_dsn: str, migrate_env: dict[str, str], history: str
) -> None:
    """Повторный отзыв перезапускает ротацию по последней задаче ротации этой ноды: отказавшая
    (``failed``, ``dead``) — перезапуск, исполняемая (``running``) и завершённая (``done``) —
    нет; «последняя», а не «любая»: история «завершена, затем отказала» перезапускается. Чужая
    нода со свежей завершённой ротацией отказавшую ротацию этой ноды не скрывает."""
    statuses, restarts = RESTART[history]
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        pool = await get_pool()
        service = CompositionService(pool)
        await conn.execute(
            "update node_identities set revoked_at = now() where node_id = $1", stand.node_id
        )
        for status in statuses:
            async with conn.transaction():
                await service.enqueue_rotate_node(conn, stand.node_id)
            await conn.execute(
                "update jobs set status = $3::job_status where id = (select max(id) from jobs "
                "where type = $1 and payload->>'node_id' = $2)",
                composition.ROTATE_NODE_JOB,
                str(stand.node_id),
                status,
            )
        neighbour = await insert_node(conn, like=stand.node_id)
        async with conn.transaction():
            await service.enqueue_rotate_node(conn, neighbour)
        await conn.execute(
            "update jobs set status = 'done' where type = $1 and payload->>'node_id' = $2",
            composition.ROTATE_NODE_JOB,
            str(neighbour),
        )
        await NodeService(pool).revoke_identity(stand.node_id, uuid.uuid4())
        rows = await jobs_of(conn, composition.ROTATE_NODE_JOB, stand.node_id)
    assert [r["status"] for r in rows] == statuses + (["pending"] if restarts else [])


async def test_a_later_publication_of_the_node_does_not_close_a_dead_rotation(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Решает последняя задача ротации ноды, а не последняя задача ноды: завершённая позже
    публикация состава той же ноды (``composition.publish_node``, нагрузка с тем же ``node_id``)
    отказавшую ротацию не закрывает — повторный отзыв её перезапускает."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        pool = await get_pool()
        nodes_service = NodeService(pool)
        await nodes_service.revoke_identity(stand.node_id, uuid.uuid4())
        await conn.execute(
            "update jobs set status = 'dead' where type = $1 and payload->>'node_id' = $2",
            composition.ROTATE_NODE_JOB,
            str(stand.node_id),
        )
        async with conn.transaction():
            await CompositionService(pool).enqueue_publish_node(conn, stand.node_id)
        await run_publications(conn)
        later = await jobs_of(conn, composition.PUBLISH_NODE_JOB, stand.node_id)
        assert [r["status"] for r in later][-1:] == ["done"]
        await nodes_service.revoke_identity(stand.node_id, uuid.uuid4())
        rows = await jobs_of(conn, composition.ROTATE_NODE_JOB, stand.node_id)
        assert [r["status"] for r in rows] == ["dead", "pending"]


async def test_a_revoked_identity_without_any_rotation_is_restarted(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Identity ноды отозвана, а задачи ротации нет (отзыв до 001.29d): повторный отзыв ставит
    ротацию — «последней задачи нет» входит в перезапуск (решение заказчика 2026-11-21)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        await stand.conn.execute(
            "update node_identities set revoked_at = now() where node_id = $1", stand.node_id
        )
        await NodeService(await get_pool()).revoke_identity(stand.node_id, uuid.uuid4())
        rows = await jobs_of(stand.conn, composition.ROTATE_NODE_JOB, stand.node_id)
        assert [r["status"] for r in rows] == ["pending"]


# Все значения node_status (data-model.md §2): пары есть на ноде любого статуса.
NODE_STATUSES = (
    "pending",
    "provisioning",
    "active",
    "degraded",
    "offline",
    "maintenance",
    "disabled",
    "suspended",
)


@pytest.mark.parametrize("pair_state", STATES)
async def test_a_reissue_rotates_every_pair_of_the_user_on_nodes_of_every_status(
    pg_dsn: str, migrate_env: dict[str, str], pair_state: str
) -> None:
    """AC-35 «на всех нодах»: пользователь в любом состоянии пары, у которого есть credentials
    (``active``, ``suspended_quota``, ``suspended_admin``, ``expired``; у ``removed`` записи нет),
    на нодах всех восьми статусов — ротация пользователя заменяет ключи всех восьми пар. Ключи
    утекли вместе с прежним токеном, и пары, временно не выдающие доступ (приостановка, истёкшая
    подписка, пересоздаваемая нода в ``pending``), восстановят его с ними."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        nodes = [stand.node_id] + [await insert_node(conn, like=stand.node_id) for _ in range(7)]
        plan_id = await plan_of(conn, await group_of(conn, *nodes))
        user = await subscribe(conn, plan_id, state=pair_state)
        pool = await get_pool()
        await CompositionService(pool).publish_user(user)
        for node_id, status in zip(nodes, NODE_STATUSES, strict=True):
            await set_status(conn, node_id, status)
        before = {n: (await credentials_of(conn, n))[user][2] for n in nodes}
        async with conn.transaction():
            rotated = await CompositionService(pool).rotate_user_credentials(conn, user)
        after = {n: (await credentials_of(conn, n))[user][2] for n in nodes}
    assert rotated == 8
    assert after == {n: v + 1 for n, v in before.items()}


async def test_a_reissue_waits_for_the_user_publication_lock(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Ротация пользователя берёт блокировку его публикации (``PUBLISH_LOCK_CLASS``) первой —
    раньше чтения его пар и строк нод (порядок публикаций «пользователь, затем строки нод»; роаст
    001.29d, раунды 2–3). Держатель — публикация (``_LOCK_USER``): пока перевыпуск ждёт, он не
    держит ни строку ноды, ни строку пары (иначе публикация, берущая строку ноды под блокировкой
    пользователя, ждала бы его по кругу), а пара, которую публикация создала на второй ноде за
    время ожидания, ротируется (иначе её ключи, выданные при действующем прежнем токене,
    остались бы с ``version`` 1)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        second = await insert_node(conn, like=stand.node_id)
        user = await subscribe(
            conn, await plan_of(conn, await group_of(conn, stand.node_id, second))
        )
        pool = await get_pool()
        service = CompositionService(pool)
        await service.publish_user(user)
        late = await conn.fetchrow(
            "delete from node_user_credentials where user_id = $1 and node_id = $2 "
            "returning xray_email, vless_uuid_enc, trojan_password_enc",
            user,
            second,
        )
        assert late is not None
        holder = await asyncpg.connect(pg_dsn)
        probe = await asyncpg.connect(pg_dsn)
        try:
            await holder.execute(composition._LOCK_USER, composition.PUBLISH_LOCK_CLASS, str(user))

            async def reissue() -> int:
                async with pool.acquire() as own, own.transaction():
                    return await service.rotate_user_credentials(own, user)

            task = asyncio.create_task(reissue())
            await wait_blocked_by(conn, holder.get_server_pid())
            await asyncio.sleep(0.2)
            assert not task.done(), "ротация пользователя не ждёт блокировку его публикации"
            node_free = await _free(probe, _NODE_FREE, stand.node_id)
            pair_free = await _free(probe, _PAIR_FREE, user, stand.node_id)
            assert (node_free, pair_free) == (1, 1), "ждущий перевыпуск держит строку ноды/пары"
            # Публикация под блокировкой пользователя создаёт пару на второй ноде.
            await holder.execute(
                "insert into node_user_credentials "
                "(user_id, node_id, xray_email, vless_uuid_enc, trojan_password_enc) "
                "values ($1, $2, $3, $4, $5)",
                user,
                second,
                late["xray_email"],
                late["vless_uuid_enc"],
                late["trojan_password_enc"],
            )
            await holder.execute(
                composition._UNLOCK_USER, composition.PUBLISH_LOCK_CLASS, str(user)
            )
            assert await asyncio.wait_for(task, timeout=10) == 2, "новая пара не ротирована"
        finally:
            await holder.close()
            await probe.close()
        keys = (await credentials_of(conn, second))[user]
        assert keys[2] == 2
        assert keys[0] != bytes(late["vless_uuid_enc"])


async def test_the_user_publication_lock_ends_with_the_callers_transaction(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Блокировка публикации пользователя — уровня транзакции: коммит вызывающего её снимает и на
    подключении, которое остаётся открытым (вне пула: пул снимает блокировки сессии при возврате
    подключения и уровень не различил бы). Сессионная блокировка держала бы публикацию
    пользователя до закрытия подключения."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        user = await subscribe(conn, await plan_of(conn, await group_of(conn, stand.node_id)))
        pool = await get_pool()
        service = CompositionService(pool)
        await service.publish_user(user)
        caller = await asyncpg.connect(pg_dsn)
        other = await asyncpg.connect(pg_dsn)
        try:
            async with caller.transaction():
                assert await service.rotate_user_credentials(caller, user) == 1
            got = await other.fetchval(
                "select pg_try_advisory_lock($1, hashtext($2))",
                composition.PUBLISH_LOCK_CLASS,
                str(user),
            )
            if got:
                await other.execute(
                    composition._UNLOCK_USER, composition.PUBLISH_LOCK_CLASS, str(user)
                )
            assert got, "блокировка публикации пользователя пережила коммит перевыпуска"
        finally:
            await caller.close()
            await other.close()


async def test_a_refused_process_key_is_retried_by_the_queue(
    pg_dsn: str, migrate_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Обработчик ``composition.rotate_node_credentials`` пропускает ``CipherMismatch`` как есть:
    это сбой среды, и решение очереди (``decide``) по нему — повтор, а не ``failed`` (как после
    ``NonRetryableError``); задача не закрывается успехом (тогда отказавшую ротацию не
    перезапустил бы повторный отзыв)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        pool = await get_pool()
        await NodeService(pool).revoke_identity(stand.node_id, uuid.uuid4())

        async def refusing() -> FieldCipher:
            raise CipherMismatch("ключ процесса не тот")

        monkeypatch.setattr(composition, "checked_cipher", refusing)
        job = await jobs.claim(conn, "critical", "t29d", types=(composition.ROTATE_NODE_JOB,))
        assert job is not None
        with pytest.raises(CipherMismatch) as raised:
            await handlers.rotate_node_credentials(conn, job)
        assert decide(raised.value, attempts=job.attempts, max_attempts=job.max_attempts) == (
            "retry",
            job.attempts,
        )
        await jobs.complete(conn, job.id, "t29d")
