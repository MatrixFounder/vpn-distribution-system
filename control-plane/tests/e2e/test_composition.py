"""Поток состава на живой базе (задача 001.29; R-04; UC-01, UC-05, UC-10, UC-11): выделение
``updated_seq`` под блокировкой строки ноды, исполнитель ``composition.publish_user`` на строках
``node_user_state``, вентиль по статусу с ``resync_required`` из колонки ноды и расшифровка
credentials только для ноды из identity.

Нода стенда — настоящая (identity, ``current_node``, стенд приёма отчётов 001.34); прочие ноды —
копии без identity. Задачи очереди здесь не ставятся: на стенде работают живые исполнители, и
поставленную задачу забрал бы чужой процесс с чужим кодом. Обработчик вызывается напрямую.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from app.agent_api.deps import get_composition_service
from app.config import Settings
from app.db.pool import get_pool
from app.domain import composition as composition_module
from app.domain.composition import CompositionService, next_users_seq
from app.domain.nodes import STUB_NODE_ID, NodeStatus, stub_node
from app.domain.statuses import STREAM_GATE
from app.jobs.handlers import HANDLERS
from app.jobs.handlers.composition import PUBLISH_USER
from app.jobs.queue import Job
from app.redis import get_redis
from app.security.crypto import FieldCipher
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from tests._agent import LAYOUT_SEQ
from tests.e2e._composition import (
    cursors,
    group_of,
    plan_of,
    row_of,
    seed_layout,
    set_status,
    state,
    subscribe,
)
from tests.e2e._reporting import AGENT_IP, insert_node, reporting, wait_blocked_by

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "agent_v1"


async def sealed_keys(
    conn: asyncpg.Connection, node_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[bytes, bytes, int]:
    """Контейнеры credentials пары и их версия, как они лежат в базе."""
    row = await conn.fetchrow(
        "select vless_uuid_enc, trojan_password_enc, version from node_user_credentials "
        "where node_id = $1 and user_id = $2",
        node_id,
        user_id,
    )
    assert row is not None, "у пары нет credentials"
    return bytes(row[0]), bytes(row[1]), int(row[2])


def job_for(user_id: uuid.UUID) -> Job:
    """Задача публикации в форме выборки исполнителем: полезная нагрузка — ``user_id``."""
    return Job(
        id=0,
        queue="critical",
        type=PUBLISH_USER,
        payload={"user_id": str(user_id)},
        idempotency_key=f"publish_user:{user_id}:0",
        run_at=dt.datetime.now(dt.UTC),
        attempts=1,
        max_attempts=10,
        status="running",
    )


async def test_tc_e2e_01_the_executor_delivers_the_row_by_delta(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Исполнение ``composition.publish_user`` пишет строку с новым номером, будит канал
    ``node:{id}``, и дельта ноды от прежнего курсора несёт ровно эту строку: строка соседа с
    номером ниже курсора в неё не попадает."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        plan_id = await plan_of(conn, await group_of(conn, stand.node_id))
        neighbour = await subscribe(conn, plan_id)
        user_id = await subscribe(conn, plan_id)
        pool = await get_pool()
        await CompositionService(pool).publish_user(neighbour)
        await CompositionService(pool).publish_user(user_id)
        first = await row_of(conn, stand.node_id, user_id)
        assert first is not None and first["state"] == "active" and first["has_credentials"]
        keys = await sealed_keys(conn, stand.node_id, user_id)

        await conn.execute(
            "update subscriptions set state = 'suspended_quota' where user_id = $1", user_id
        )
        # Грант пишет служба грантов (001.36), а заблокированные адреса (R-27) — лимит адресов:
        # публикация берёт адреса пользователя и не трогает грант строки.
        await conn.execute(
            "update node_user_state set quota_grant_bytes = 123 where node_id = $1 and "
            "user_id = $2",
            stand.node_id,
            user_id,
        )
        await conn.execute(
            "insert into user_blocked_ips (user_id, ip) values ($1, '198.51.100.7')", user_id
        )
        redis = await get_redis()
        woken = None
        async with redis.pubsub() as channel:
            await channel.subscribe(f"node:{stand.node_id}")
            async with pool.acquire() as job_conn:
                await HANDLERS[PUBLISH_USER](job_conn, job_for(user_id))
            for _ in range(40):
                message = await channel.get_message(ignore_subscribe_messages=True, timeout=0.25)
                if message is not None:
                    woken = message
                    break
        assert woken is not None, "канал node:{id} не разбужен после коммита"

        second = await row_of(conn, stand.node_id, user_id)
        assert second is not None and second["state"] == "suspended_quota"
        assert second["updated_seq"] == first["updated_seq"] + 1
        data = woken["data"]
        assert (data.decode() if isinstance(data, bytes) else data) == str(second["updated_seq"]), (
            "пробуждение несёт номер записанной строки"
        )
        desired = await conn.fetchval(
            "select desired_users_seq from nodes where id = $1", stand.node_id
        )
        assert desired == second["updated_seq"], "номер выделен из desired_users_seq ноды"

        response = await state(stand, cursors(first["updated_seq"]))
        assert response.status_code == 200, response.text
        users = response.json()["users"]
        assert users["seq"] == second["updated_seq"] and users["full"] is False
        assert [(r["user_id"], r["state"], r["updated_seq"]) for r in users["rows"]] == [
            (str(user_id), "suspended_quota", second["updated_seq"])
        ]
        assert users["rows"][0].get("credentials") is None, "снятому доступу ключ не выдаётся"
        assert users["rows"][0]["blocked_ips"] == ["198.51.100.7"]
        assert users["rows"][0]["quota_grant_bytes"] == 123, "грант строки публикация не трогает"
        assert await sealed_keys(conn, stand.node_id, user_id) == keys, (
            "credentials создаются только при отсутствии: повтор пару не перешифровывает"
        )


async def test_tc_e2e_03_a_filtering_status_raises_the_column_that_outlives_it(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """``suspended``: только строки, снимающие доступ; курсор — по выборке до фильтра; признак
    полноты снят; ``resync_required`` взведён **в колонке ноды** и после возврата в ``active``
    приходит и на текущих курсорах — §5.2 требует показать его при выходе из статуса."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        group = await group_of(conn, stand.node_id)
        plan_id = await plan_of(conn, group)
        revoked = await subscribe(conn, plan_id)
        granted = await subscribe(conn, plan_id)
        service = CompositionService(await get_pool())
        await service.publish_user(granted)
        await service.publish_user(revoked)
        await conn.execute("update subscriptions set state = 'expired' where user_id = $1", revoked)
        await service.publish_user(revoked)
        await service.publish_user(granted)
        expired = await row_of(conn, stand.node_id, revoked)
        latest = await row_of(conn, stand.node_id, granted)
        assert expired is not None and latest is not None
        assert expired["updated_seq"] < latest["updated_seq"], "самое свежее — выдача"

        await set_status(conn, stand.node_id, "suspended")
        response = await state(stand, cursors(0))
        assert response.status_code == 200, response.text
        body = response.json()
        assert [(r["user_id"], r["state"]) for r in body["users"]["rows"]] == [
            (str(revoked), "expired")
        ]
        assert body["users"]["seq"] == latest["updated_seq"], "курсор — по выборке до фильтра"
        assert body["users"]["full"] is False
        assert body["resync_required"] is True
        assert await conn.fetchval(
            "select resync_required from nodes where id = $1", stand.node_id
        ), "признак взведён в колонке ноды, а не выведен из статуса ответа"

        await set_status(conn, stand.node_id, "active")
        back = await state(stand, cursors(latest["updated_seq"]))
        assert back.status_code == 200, back.text
        assert back.json()["resync_required"] is True, "после выхода из статуса — из колонки"
        assert back.json()["users"]["rows"] == []


async def test_tc_e2e_04_a_user_who_left_the_node_groups_is_removed_there(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Смена тарифа: новый тариф не разделяет группу с нодой стенда — её строка в ``removed`` с
    новым номером и без адресов, запись credentials пары удалена, дельта ноды несёт ``removed``
    без ключей; на ноде общей группы доступ сохраняется."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        left = stand.node_id
        kept = await insert_node(conn, like=left)
        shared, other = await group_of(conn, kept), await group_of(conn, left)
        user_id = await subscribe(conn, await plan_of(conn, shared, other))
        await conn.execute(
            "insert into user_blocked_ips (user_id, ip) values ($1, '198.51.100.9')", user_id
        )
        service = CompositionService(await get_pool())
        await service.publish_user(user_id)
        before = await row_of(conn, left, user_id)
        assert before is not None and before["state"] == "active" and before["has_credentials"]
        assert [str(ip) for ip in before["blocked_ips"]] == ["198.51.100.9"]

        await subscribe(conn, await plan_of(conn, shared), user_id=user_id)
        await service.publish_user(user_id)
        after = await row_of(conn, left, user_id)
        assert after is not None and after["state"] == "removed"
        assert after["updated_seq"] > before["updated_seq"]
        assert not after["has_credentials"], "credentials пары вне общей группы удалены"
        assert after["blocked_ips"] == [], "строке снятой пары адреса пользователя не нужны"
        still = await row_of(conn, kept, user_id)
        assert still is not None and still["state"] == "active" and still["has_credentials"]

        response = await state(stand, cursors(before["updated_seq"]))
        assert response.status_code == 200, response.text
        rows = response.json()["users"]["rows"]
        assert [(r["user_id"], r["state"], r["updated_seq"]) for r in rows] == [
            (str(user_id), "removed", after["updated_seq"])
        ]
        assert rows[0].get("credentials") is None and rows[0]["blocked_ips"] == []


class FailsOnSecondNodeWrite(CompositionService):
    """Исполнитель, у которого запись второй ноды падает ошибкой базы: сбой посередине."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        super().__init__(pool)
        self.visited: list[uuid.UUID] = []

    async def _publish_pair(self, conn: Any, user_id: uuid.UUID, node_id: uuid.UUID) -> int | None:
        self.visited.append(node_id)
        if len(self.visited) == 2:
            raise asyncpg.exceptions.LockNotAvailableError("сбой записи второй ноды")
        return await super()._publish_pair(conn, user_id, node_id)


async def test_tc_e2e_05_a_failure_midway_repeats_the_whole_publication(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Ноды обходятся по ``node_id``, у каждой своя транзакция: сбой записи второй ноды
    пробрасывается (задача повторяется, а не завершается), первая остаётся закоммиченной;
    повтор публикует обе, первую — с новым номером."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        second_node = await insert_node(conn, like=stand.node_id)
        first, second = sorted((stand.node_id, second_node))
        user_id = await subscribe(
            conn, await plan_of(conn, await group_of(conn, first), await group_of(conn, second))
        )
        pool = await get_pool()
        failing = FailsOnSecondNodeWrite(pool)
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await failing.publish_user(user_id)
        assert failing.visited == [first, second], "ноды обходятся в порядке node_id"
        partial = await row_of(conn, first, user_id)
        assert partial is not None, "первая нода закоммичена до сбоя"
        assert await row_of(conn, second, user_id) is None

        await CompositionService(pool).publish_user(user_id)
        repeated = await row_of(conn, first, user_id)
        assert repeated is not None and repeated["updated_seq"] > partial["updated_seq"]
        assert (await row_of(conn, second, user_id)) is not None, "повтор дошёл до второй ноды"


async def test_no_number_is_spent_when_there_is_nothing_to_write(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Нода попала в кандидаты, но, пока исполнитель ждал блокировку её строки, тариф перестал
    делить с ней группу, а строки у пользователя там нет: писать нечего — номер не выделяется,
    иначе в курсоре ноды осталась бы дыра."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        user_id = await subscribe(conn, await plan_of(conn, await group_of(conn, stand.node_id)))
        before = await conn.fetchval(
            "select desired_users_seq from nodes where id = $1", stand.node_id
        )
        holder = await asyncpg.connect(pg_dsn)
        try:
            transaction = holder.transaction()
            await transaction.start()
            await holder.execute("select 1 from nodes where id = $1 for update", stand.node_id)
            publication = asyncio.create_task(
                CompositionService(await get_pool()).publish_user(user_id)
            )
            await wait_blocked_by(conn, holder.get_server_pid())
            await subscribe(conn, await plan_of(conn), user_id=user_id)  # тариф без групп
            await transaction.commit()
            await asyncio.wait_for(publication, timeout=10)
        finally:
            await holder.close()
        assert await row_of(conn, stand.node_id, user_id) is None
        assert (
            await conn.fetchval("select desired_users_seq from nodes where id = $1", stand.node_id)
            == before
        ), "номер выделен без строки"


async def test_tc_e2e_06_the_user_state_is_read_under_the_node_lock(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Строку ноды держит то, что держит конкурирующий писатель строк ноды (публикация другого
    пользователя, состав ноды 001.29b, приём отчёта), — незакоммиченный ``next_users_seq``
    (``FOR NO KEY UPDATE``). Исполнитель обязан ждать его коммита: тем временем
    коммитится перевод подписки, и строка получает новое состояние, а не прочитанное до чужого
    коммита (иначе устаревшая запись легла бы с большим номером поверх свежей). Держатель
    ``FOR UPDATE`` не отличил бы нужный режим блокировки от ``KEY SHARE``, с которым чтение не
    ждёт."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        user_id = await subscribe(conn, await plan_of(conn, await group_of(conn, stand.node_id)))
        holder = await asyncpg.connect(pg_dsn)
        try:
            transaction = holder.transaction()
            await transaction.start()
            await next_users_seq(holder, stand.node_id)
            publication = asyncio.create_task(
                CompositionService(await get_pool()).publish_user(user_id)
            )
            await wait_blocked_by(conn, holder.get_server_pid())
            await conn.execute(
                "update subscriptions set state = 'suspended_admin' where user_id = $1", user_id
            )
            await transaction.commit()
            await asyncio.wait_for(publication, timeout=10)
        finally:
            await holder.close()
        row = await row_of(conn, stand.node_id, user_id)
        assert row is not None and row["state"] == "suspended_admin"


class SpyCipher(FieldCipher):
    """Шифр, запоминающий расшифрованные контейнеры."""

    def __init__(self, inner: FieldCipher) -> None:
        self.__dict__.update(inner.__dict__)
        self.opened: list[bytes] = []

    def decrypt(self, b: bytes) -> bytes:
        self.opened.append(b)
        return super().decrypt(b)


async def test_tc_e2e_07_only_the_requesting_node_credentials_are_decrypted(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Пользователь на двух нодах: ответ ноде стенда несёт только её строку и её ключи;
    контейнеры второй ноды не расшифровываются вовсе."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        other = await insert_node(conn, like=stand.node_id)
        user_id = await subscribe(
            conn, await plan_of(conn, await group_of(conn, stand.node_id, other))
        )
        pool = await get_pool()
        await CompositionService(pool).publish_user(user_id)
        spy = SpyCipher(FieldCipher.from_settings(Settings.load()))

        async def spying() -> CompositionService:
            return CompositionService(pool, cipher=spy)

        stand.app.dependency_overrides[get_composition_service] = spying
        response = await state(stand, cursors(0))
        assert response.status_code == 200, response.text
        rows = response.json()["users"]["rows"]
        assert [(r["user_id"], r["state"]) for r in rows] == [(str(user_id), "active")]
        own = await conn.fetchrow(
            "select vless_uuid_enc, trojan_password_enc from node_user_credentials "
            "where node_id = $1 and user_id = $2",
            stand.node_id,
            user_id,
        )
        foreign = await conn.fetchrow(
            "select vless_uuid_enc, trojan_password_enc from node_user_credentials "
            "where node_id = $1 and user_id = $2",
            other,
            user_id,
        )
        assert own is not None and foreign is not None
        assert sorted(spy.opened) == sorted([own[0], own[1]])
        assert rows[0]["credentials"]["vless_uuid"] == str(
            uuid.UUID(bytes=spy.decrypt(own["vless_uuid_enc"]))
        )
        assert rows[0]["credentials"]["vless_uuid"] != str(
            uuid.UUID(bytes=spy.decrypt(foreign["vless_uuid_enc"]))
        ), "у пары на другой ноде свои ключи (§11.3)"

        # Отключённой ноде идут только отзывы: ключи её строк не расшифровываются вовсе.
        spy.opened.clear()
        await set_status(conn, stand.node_id, "disabled")
        filtered = await state(stand, cursors(0))
        assert filtered.status_code == 200, filtered.text
        assert filtered.json()["users"]["rows"] == [] and spy.opened == []


BEHIND = {"config_version": 0, "agent_section_version": 0, "users_seq": 0, "generation": 1}


@pytest.mark.parametrize("status", sorted(STREAM_GATE))
@pytest.mark.parametrize("form", ["delta", "snapshot"])
async def test_tc_e2e_02_the_status_gate_holds_on_live_rows(
    pg_dsn: str, migrate_env: dict[str, str], status: NodeStatus, form: str
) -> None:
    """Вентиль §4.6 построчно на строках ``node_user_state`` живой базы (раскладка фикстур,
    номера 1…4): ``pending`` — 403; ``provisioning`` — состав пуст, курсор состава на месте, ни
    одного credential (AC-13); ``disabled``/``suspended`` — только отзывы, курсор по выборке до
    фильтра, полноты нет, колонка ``resync_required`` взведена; остальные — весь состав, у
    снапшота без ``removed`` и с признаком полноты."""
    gate = STREAM_GATE[status]
    params = BEHIND if form == "delta" else {**BEHIND, "full": 1}
    async with reporting(pg_dsn, migrate_env, users=0, status=status) as stand:
        await seed_layout(stand.conn, stand.node_id, FieldCipher.from_settings(Settings.load()))
        response = await state(stand, params)
        flagged = await stand.conn.fetchval(
            "select resync_required from nodes where id = $1", stand.node_id
        )
    if gate.refused:
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "node_not_approved"
        return
    assert response.status_code == 200, response.text
    body = response.json()
    rows = [(r["updated_seq"], r["state"]) for r in body["users"]["rows"]]
    keyed = [r["updated_seq"] for r in body["users"]["rows"] if r.get("credentials")]
    if gate.composition == "none":
        assert rows == [] and body["users"]["seq"] == 0
    elif gate.composition == "revocations":
        assert rows == [(2, "suspended_quota"), (3, "removed")]
        assert body["users"]["seq"] == LAYOUT_SEQ, "курсор — по выборке до фильтра"
    else:
        assert rows == (
            [(1, "active"), (2, "suspended_quota"), (4, "active")]
            if form == "snapshot"
            else [(1, "active"), (2, "suspended_quota"), (3, "removed"), (4, "active")]
        )
        assert body["users"]["seq"] == LAYOUT_SEQ
    assert keyed == [seq for seq, state_ in rows if state_ == "active"], "ключи — у выдачи"
    assert body["users"]["full"] is (form == "snapshot" and gate.composition == "all")
    assert body["resync_required"] is gate.filters_composition
    assert flagged is gate.filters_composition, "признак взведён в колонке ноды"


async def test_the_recorded_state_answers_are_what_the_live_service_gives(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Фикстуры состояния (`contracts/agent_v1/state-*.json`) — ответы живой службы: при
    раскладке фикстур строками базы на ноде `STUB_NODE_ID` в статусе `given.node_status` живой
    раздел отвечает ровно записанным кодом, заголовками и телом. Герметичный прогон
    (`tests/contract`) отвечает на той же раскладке без базы; этот тест держит её правдой.
    `state-unauthenticated` не воспроизводится: его ответ — о форме identity, а identity здесь
    своя у стенда."""
    fixtures = {
        path.stem: json.loads(path.read_text())
        for path in CONTRACTS.glob("state-*.json")
        if path.stem != "state-unauthenticated"
    }
    assert len(fixtures) == 11, sorted(fixtures)
    identity = {"X-Client-Cert", "X-Node-Identity"}
    cipher = FieldCipher.from_settings(Settings.load())
    for stem, fixture in sorted(fixtures.items()):
        async with reporting(
            pg_dsn,
            migrate_env,
            users=0,
            node_id=STUB_NODE_ID,
            status=fixture["given"]["node_status"],
        ) as stand:
            await seed_layout(stand.conn, stand.node_id, cipher)
            request, expected = fixture["request"], fixture["response"]
            headers = {
                **{k: v for k, v in request["headers"].items() if k not in identity},
                **{k: v for k, v in stand.headers.items() if k in identity},
            }
            transport = httpx.ASGITransport(
                app=stand.app, raise_app_exceptions=False, client=(AGENT_IP, 4000)
            )
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                response = await client.request(
                    request["method"], request["path"], params=request["query"], headers=headers
                )
        assert response.status_code == expected["status"], (stem, response.text)
        for name, value in expected["headers"].items():
            assert response.headers.get(name) == value, (stem, name)
        if expected["body"] is None:
            assert response.content == b"", stem
        else:
            assert response.json() == expected["body"], stem


class PausesAfterNodeLock(CompositionService):
    """Публикация, которая останавливается сразу после блокировки строки ноды."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        super().__init__(pool)
        self.locked, self.release = asyncio.Event(), asyncio.Event()
        self.pid = 0

    async def _lock_node(self, conn: Any, node_id: uuid.UUID) -> bool:
        found = await super()._lock_node(conn, node_id)
        self.pid = conn.get_server_pid()
        self.locked.set()
        await self.release.wait()
        return found


async def test_two_publications_on_one_node_wait_for_each_other(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Публикации разных пользователей на одной ноде: вторая ждёт, пока первая держит строку
    ноды, — именно строку, а не блокировку пользователя (общий ключ сериализовал бы публикации
    всех пользователей), — и обе записывают строки с соседними номерами. Режим ``SHARE`` пустил
    бы вторую к своему ``UPDATE nodes`` и свёл обе во взаимную блокировку, ``KEY SHARE`` — не
    заставил бы её ждать вовсе."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        plan_id = await plan_of(conn, await group_of(conn, stand.node_id))
        first_user, second_user = await subscribe(conn, plan_id), await subscribe(conn, plan_id)
        pool = await get_pool()
        holding = PausesAfterNodeLock(pool)
        first = asyncio.create_task(holding.publish_user(first_user))
        await asyncio.wait_for(holding.locked.wait(), timeout=10)
        second = asyncio.create_task(CompositionService(pool).publish_user(second_user))
        try:
            await wait_blocked_by(conn, holding.pid)
            awaited = await conn.fetch(
                "select l.locktype from pg_locks l where not l.granted "
                "and $1 = any(pg_blocking_pids(l.pid))",
                holding.pid,
            )
        finally:
            holding.release.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
        kinds = {record["locktype"] for record in awaited}
        assert kinds and "advisory" not in kinds, f"вторая ждёт не строку ноды: {kinds}"
        rows = [await row_of(conn, stand.node_id, user) for user in (first_user, second_user)]
        assert all(row is not None and row["state"] == "active" for row in rows)
        assert sorted(row["updated_seq"] for row in rows if row is not None) == [1, 2]


class PausesAfterWrite(CompositionService):
    """Публикация, которая останавливается после записи строки — до коммита её транзакции."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        super().__init__(pool)
        self.written, self.release = asyncio.Event(), asyncio.Event()
        self.pid = 0

    async def _publish_pair(self, conn: Any, user_id: uuid.UUID, node_id: uuid.UUID) -> int | None:
        seq = await super()._publish_pair(conn, user_id, node_id)
        self.pid = conn.get_server_pid()
        self.written.set()
        await self.release.wait()
        return seq


async def test_publications_of_one_user_run_one_after_another(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Запуск A пишет доступ на ноде стенда и ещё не закоммитил; тариф сменился на тариф без
    этой ноды, и запуск B (после смены) ждёт, пока A закончит: иначе B выбрал бы кандидатов без
    незакоммиченной строки A и не снял бы доступ, который A затем закоммитит."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        elsewhere = await insert_node(conn, like=stand.node_id)
        user_id = await subscribe(conn, await plan_of(conn, await group_of(conn, stand.node_id)))
        pool = await get_pool()
        earlier = PausesAfterWrite(pool)
        first = asyncio.create_task(earlier.publish_user(user_id))
        await asyncio.wait_for(earlier.written.wait(), timeout=10)
        await subscribe(conn, await plan_of(conn, await group_of(conn, elsewhere)), user_id=user_id)
        second = asyncio.create_task(CompositionService(pool).publish_user(user_id))
        try:
            await wait_blocked_by(conn, earlier.pid)
        finally:
            earlier.release.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
        old = await row_of(conn, stand.node_id, user_id)
        new = await row_of(conn, elsewhere, user_id)
        assert old is not None and old["state"] == "removed" and not old["has_credentials"]
        assert new is not None and new["state"] == "active"


@pytest.mark.parametrize(
    "failure", [RedisConnectionError, RedisTimeoutError], ids=["connection", "timeout"]
)
async def test_a_redis_outage_does_not_stop_the_publication(
    pg_dsn: str,
    migrate_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: type[Exception],
) -> None:
    """Redis недоступен или завис (``socket_timeout`` клиента — ``TimeoutError``, не подкласс
    ``ConnectionError``): строки обеих нод закоммичены, публикация завершается без ошибки, в
    журнале — отказ пробуждения и пропуск остальных (пробуждение стоит до 30 с удержания, а не
    доставку; повтор задачи выдал бы строкам новые номера без нужды)."""

    attempts: list[int] = []

    async def down() -> None:
        attempts.append(1)
        raise failure("Redis недоступен (подставлено тестом)")

    monkeypatch.setattr(composition_module, "get_redis", down)
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        other = await insert_node(conn, like=stand.node_id)
        user_id = await subscribe(
            conn, await plan_of(conn, await group_of(conn, stand.node_id, other))
        )
        with caplog.at_level(logging.WARNING, logger="app.domain.composition"):
            await CompositionService(await get_pool()).publish_user(user_id)
        assert await row_of(conn, stand.node_id, user_id) is not None
        assert await row_of(conn, other, user_id) is not None
        messages = [record.getMessage() for record in caplog.records]
        assert any("пробуждение не отправлено" in m for m in messages), messages
        assert any("пробуждения ещё 1 нод пропущены" in m for m in messages), messages
        assert len(attempts) == 1, "после первого отказа Redis пробуждения не повторяются"


async def test_a_subscription_without_state_shares_no_group(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Подписка в ``none`` с текущим периодом группу с нодой не делит: строка уходит в
    ``removed``, credentials пары удаляются (``none`` не значение ``user_node_state``)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        user_id = await subscribe(conn, await plan_of(conn, await group_of(conn, stand.node_id)))
        service = CompositionService(await get_pool())
        await service.publish_user(user_id)
        await conn.execute("update subscriptions set state = 'none' where user_id = $1", user_id)
        await service.publish_user(user_id)
        row = await row_of(conn, stand.node_id, user_id)
        assert row is not None and row["state"] == "removed" and not row["has_credentials"]


class _ProbeConnection:
    """Подключение пула-зонда: после первого чтения выдачи — каким бы оно ни было, курсора ноды
    или строк, и на каком бы подключении пула ни шло — даёт закоммититься чужой публикации,
    один раз на весь зонд."""

    def __init__(self, conn: Any, probe: _ProbePool) -> None:
        self._conn, self._probe = conn, probe

    def transaction(self, **options: Any) -> Any:
        return self._conn.transaction(**options)

    async def _after_first_read(self) -> None:
        if not self._probe.fired:
            self._probe.fired = True
            await self._probe.between()

    async def fetchrow(self, query: str, *args: Any) -> Any:
        row = await self._conn.fetchrow(query, *args)
        await self._after_first_read()
        return row

    async def fetch(self, query: str, *args: Any) -> Any:
        rows = await self._conn.fetch(query, *args)
        await self._after_first_read()
        return rows


class _ProbePool:
    """Пул-зонд: признак срабатывания общий для всех его подключений — выдача, читающая курсор и
    строки на разных подключениях, не вызовет чужую публикацию дважды."""

    def __init__(self, pool: asyncpg.Pool, between: Callable[[], Awaitable[None]]) -> None:
        self._pool, self.between, self.fired = pool, between, False

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_ProbeConnection]:
        async with self._pool.acquire() as conn:
            yield _ProbeConnection(conn, self)


@pytest.mark.parametrize("form", ["snapshot", "delta"])
async def test_the_cursor_and_the_rows_come_from_one_snapshot(
    pg_dsn: str, migrate_env: dict[str, str], form: str
) -> None:
    """После первого чтения выдачи коммитится публикация другого пользователя. Курсор ноды и
    строки читаются в одном снимке базы, в каком бы порядке ни шли чтения: курсор ответа
    покрывает все его строки, и ни одна строка под курсором не остаётся недоставленной
    (опоздавшую строку доставит следующая дельта). Формы — снапшот с нуля и дельта на текущем
    курсоре ноды."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        plan_id = await plan_of(conn, await group_of(conn, stand.node_id))
        earlier, later = await subscribe(conn, plan_id), await subscribe(conn, plan_id)
        pool = await get_pool()
        await CompositionService(pool).publish_user(earlier)
        first = await row_of(conn, stand.node_id, earlier)
        assert first is not None
        presented = 0 if form == "snapshot" else first["updated_seq"]

        async def between() -> None:
            await CompositionService(pool).publish_user(later)

        probe = CompositionService(_ProbePool(pool, between))
        answer = await probe.state_for(
            stub_node(node_id=stand.node_id), 1, presented, 1, form == "snapshot", 1
        )
        late = await row_of(conn, stand.node_id, later)
        assert late is not None, "чужая запись состоялась"
        delivered = [] if answer is None else [row.user_id for row in answer.users.rows]
        cursor = presented if answer is None else answer.users.seq
        assert later in delivered or cursor < late["updated_seq"], (
            f"курсор {cursor} покрыл недоставленную строку {late['updated_seq']}"
        )
        if answer is not None:
            assert all(row.updated_seq <= answer.users.seq for row in answer.users.rows), (
                "строка ответа выше его курсора"
            )
        if form == "snapshot":
            assert answer is not None and delivered == [earlier]


@pytest.mark.parametrize("status", ["disabled", "suspended", "provisioning"])
async def test_a_node_owing_a_resync_it_cannot_clear_is_held_on_current_cursors(
    pg_dsn: str, migrate_env: dict[str, str], status: NodeStatus
) -> None:
    """Где вентиль урезает состав (``disabled``, ``suspended``) или закрывает его
    (``provisioning`` — например, после повторной регистрации ноды, у которой признак остался
    взведённым), взведённый признак сам по себе не повод отвечать: снять его там нечем
    (полного ответа вентиль не даёт), и нода без 204 не дожидалась бы удержания. Во всех
    статусах, где вентиль отдаёт состав целиком (``active``, ``degraded``, ``offline``,
    ``maintenance`` — выход из приостановки ведёт и в ``offline``), тот же признак на тех же
    курсорах — 200 с ``resync_required``."""
    async with reporting(pg_dsn, migrate_env, users=0, status=status) as stand:
        await seed_layout(stand.conn, stand.node_id, FieldCipher.from_settings(Settings.load()))
        await stand.conn.execute(
            "update nodes set resync_required = true where id = $1", stand.node_id
        )
        held = await state(stand, cursors(LAYOUT_SEQ))
        assert held.status_code == 204, held.text
        complete = sorted(name for name, gate in STREAM_GATE.items() if gate.composition == "all")
        assert complete == ["active", "degraded", "maintenance", "offline"]
        for told_in in complete:
            await set_status(stand.conn, stand.node_id, told_in)
            told = await state(stand, cursors(LAYOUT_SEQ))
            assert told.status_code == 200, (told_in, told.text)
            assert told.json()["resync_required"] is True, told_in


async def test_a_truncated_snapshot_ignores_the_presented_cursor(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """``full=1`` в фильтрующем статусе с курсором впереди серверного (восстановление из копии):
    снапшот присланный курсор игнорирует, и отзывы ноды приходят все, с курсором ноды."""
    async with reporting(pg_dsn, migrate_env, users=0, status="suspended") as stand:
        await seed_layout(stand.conn, stand.node_id, FieldCipher.from_settings(Settings.load()))
        response = await state(stand, cursors(99, full=True))
        assert response.status_code == 200, response.text
        users = response.json()["users"]
        assert [(r["updated_seq"], r["state"]) for r in users["rows"]] == [
            (2, "suspended_quota"),
            (3, "removed"),
        ]
        assert users["seq"] == LAYOUT_SEQ and users["full"] is False


async def test_wakes_go_out_after_every_node_is_committed(
    pg_dsn: str, migrate_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пробуждения уходят после коммита всех нод пользователя: медленный или недоступный Redis
    задерживает пробуждения, а не запись следующих нод (бюджет Н-13 считает запись строк)."""
    async with reporting(pg_dsn, migrate_env, users=0) as stand:
        conn = stand.conn
        other = await insert_node(conn, like=stand.node_id)
        user_id = await subscribe(
            conn, await plan_of(conn, await group_of(conn, stand.node_id, other))
        )
        committed: list[int] = []

        class CountingRedis:
            async def publish(self, channel: str, message: str) -> int:
                committed.append(
                    await conn.fetchval(
                        "select count(*) from node_user_state where user_id = $1", user_id
                    )
                )
                return 0

        async def counting() -> CountingRedis:
            return CountingRedis()

        monkeypatch.setattr(composition_module, "get_redis", counting)
        await CompositionService(await get_pool()).publish_user(user_id)
        assert committed == [2, 2], "пробуждение ушло до коммита второй ноды"
