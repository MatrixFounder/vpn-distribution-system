"""Сквозные проверки задачи 001.51: ``emit`` пишет событие §4.17 в ``events`` на подключении
транзакции вызывающего и ставит задачи доставки в ``background``.

TC-E2E-01: каждое из 12 событий со своим адресатом → строка ``events`` и две задачи (почта и
webhook); каждому получателю — своя задача, без секрета. TC-E2E-02: повтор с тем же ``dedup_key``
— ни строки, ни задач. TC-E2E-03: два одновременных ``emit`` с одним ключом — второй ждёт коммита
первого и получает подавление; при откате первого событие пишет второй. TC-E2E-04: битые
получатели ``settings.webhooks`` пропускаются с ERROR, остальным задачи ставятся; длинный адрес
операцию не срывает. TC-E2E-05: откат вызывающего уносит событие и задачи. Плюс: только в
транзакции, адресат и ключ своего события, нагрузка с днём и числом, разрыв непрерывности.

Почти все тесты идут в одной транзакции, которая откатывается: настройки, события и задачи теста
наружу не выходят и не мешают параллельным прогонам. Коммит нужен только TC-E2E-03 — его
события удаляются по ключам, ``settings.webhooks`` возвращается к прежнему значению. Задачи
ставятся под типами ``test-notify.*`` (``tests/conftest.py``): живой исполнитель стенда их не
забирает.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import pytest
from app.notifications import delivery, events
from app.notifications.events import EventType, dedup_key, emit, webhook_job_key, webhook_urls

from tests.conftest import TEST_NOTIFY_EMAIL, TEST_NOTIFY_WEBHOOK
from tests.e2e._reporting import wait_blocked_by
from tests.e2e._spec import EXPECTED_ENUMS

HOOK = "https://hooks.example.test/vpn"
OTHER_HOOK = "https://other.example.test/vpn"
AT = dt.datetime(2026, 11, 26, 9, 30, 15, 120000, tzinfo=dt.UTC)
DAY = dt.date(2026, 11, 25)


async def set_webhooks(conn: asyncpg.Connection, webhooks: Any) -> None:
    await conn.execute("delete from settings where key = 'webhooks'")
    if webhooks is not None:
        await conn.execute(
            "insert into settings (key, value) values ('webhooks', $1::jsonb)", json.dumps(webhooks)
        )


@asynccontextmanager
async def rolled_back(pg_dsn: str, webhooks: Any = None) -> AsyncIterator[asyncpg.Connection]:
    """Подключение в транзакции, которая в конце откатывается; ``settings.webhooks`` внутри неё —
    ``webhooks`` (``None`` — ключа нет)."""
    conn = await asyncpg.connect(pg_dsn)
    tx = conn.transaction()
    await tx.start()
    try:
        await set_webhooks(conn, webhooks)
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


def traffic_80() -> tuple[uuid.UUID, str]:
    user = uuid.uuid4()
    return user, dedup_key(EventType.TRAFFIC_80, user_id=user, period_id=uuid.uuid4())


async def jobs_of(conn: asyncpg.Connection, event_id: uuid.UUID | None) -> dict[str, Any]:
    """Задачи доставки события: ключ идемпотентности → (тип, очередь, нагрузка)."""
    rows = await conn.fetch(
        "select type, queue::text, payload, idempotency_key from jobs "
        "where payload->>'event_id' = $1",
        str(event_id),
    )
    return {r["idempotency_key"]: (r["type"], r["queue"], json.loads(r["payload"])) for r in rows}


async def events_with(conn: asyncpg.Connection, key: str) -> list[asyncpg.Record]:
    rows: list[asyncpg.Record] = await conn.fetch("select * from events where dedup_key = $1", key)
    return rows


def subject_of(event: EventType) -> tuple[uuid.UUID | None, uuid.UUID | None, dict[str, Any]]:
    """Адресат и части ключа события: пользователь, нода, части ``dedup_key`` (§4.17)."""
    user, node, period = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    if event is EventType.NODE_ADDRESS_CHANGED:
        return user, node, {"user_id": user, "node_id": node, "kind": "params", "version": 0}
    if event is EventType.TRAFFIC_EXHAUSTED:
        return user, None, {"user_id": user, "period_id": period, "exhausted_at": AT}
    if event in (EventType.TRAFFIC_80, EventType.TRAFFIC_95):
        return user, None, {"user_id": user, "period_id": period}
    if event is EventType.SUBSCRIPTION_ACTIVATED:  # вид — активация, эпизод — конец периода
        return (
            user,
            None,
            {"user_id": user, "kind": "activated", "period_id": period, "period_end": AT},
        )
    if event in events.USER_EVENTS:  # «истекает», «истекла»: эпизод — конец периода
        return user, None, {"user_id": user, "period_id": period, "period_end": AT}
    if event is EventType.RECONCILIATION_MISMATCH:
        return None, node, {"node_id": node, "kind": "cross_source", "day": DAY}
    if event is EventType.NODE_REPORT_BUFFER_FULL:
        return None, node, {"node_id": node, "period_end": AT}
    return None, node, {"node_id": node, "status_changed_at": AT}


async def test_the_event_types_are_the_database_labels(pg_dsn: str) -> None:
    """12 типов ``EventType`` — метки ``event_type`` базы стенда в том же порядке (§4.17)."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        labels = await conn.fetchval(
            "select array_agg(enumlabel order by enumsortorder) from pg_enum "
            "where enumtypid = 'event_type'::regtype"
        )
    finally:
        await conn.close()
    assert list(labels) == [e.value for e in EventType] == EXPECTED_ENUMS["event_type"]


@pytest.mark.parametrize("event", list(EventType), ids=[e.value for e in EventType])
async def test_tc_e2e_01_every_event_places_mail_and_webhook(pg_dsn: str, event: EventType) -> None:
    """TC-E2E-01: каждое из 12 событий со своим адресатом — строка ``events`` (тип, адресат,
    нагрузка ``{}`` или вид) и ровно две задачи в ``background``: почта (у операторских тоже —
    выбор каналов §4.17 делает 001.52) и webhook единственному получателю."""
    user, node, parts = subject_of(event)
    payload = {"kind": parts["kind"]} if "kind" in parts else None
    key = dedup_key(event, **parts)
    async with rolled_back(pg_dsn, [{"url": HOOK, "secret": "s3cret"}]) as conn:
        event_id = await emit(conn, event, user, node, payload, dedup_key=key)
        assert event_id is not None
        [row] = await events_with(conn, key)
        assert (row["id"], row["type"], row["user_id"], row["node_id"]) == (
            event_id,
            event.value,
            user,
            node,
        )
        assert json.loads(row["payload"]) == (payload or {})
        assert await jobs_of(conn, event_id) == {
            f"notify:email:{event_id}": (
                TEST_NOTIFY_EMAIL,
                "background",
                {"event_id": str(event_id)},
            ),
            webhook_job_key(event_id, HOOK): (
                TEST_NOTIFY_WEBHOOK,
                "background",
                {"event_id": str(event_id), "url": HOOK},
            ),
        }


async def test_tc_e2e_01_each_recipient_gets_its_own_job(
    pg_dsn: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Два получателя — две задачи webhook (один ключ на событие дал бы одну); секрет получателя
    в задачу не попадает; без ключа ``settings.webhooks`` (до сида 001.79) — только почта."""
    hooks = [{"url": HOOK, "secret": "a"}, {"url": OTHER_HOOK, "secret": "b"}]
    async with rolled_back(pg_dsn, hooks) as conn:
        user, key = traffic_80()
        event_id = await emit(conn, EventType.TRAFFIC_80, user_id=user, dedup_key=key)
        assert event_id is not None
        placed = await jobs_of(conn, event_id)
        assert sorted(placed) == sorted(
            [
                f"notify:email:{event_id}",
                webhook_job_key(event_id, HOOK),
                webhook_job_key(event_id, OTHER_HOOK),
            ]
        )
        assert "secret" not in json.dumps([payload for _, _, payload in placed.values()])
    async with rolled_back(pg_dsn, None) as conn:  # до сида 001.79 ключа нет — это не ошибка
        user, key = traffic_80()
        with caplog.at_level(logging.ERROR, logger="app.notifications.events"):
            event_id = await emit(conn, EventType.TRAFFIC_80, user_id=user, dedup_key=key)
        assert list(await jobs_of(conn, event_id)) == [f"notify:email:{event_id}"]
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


async def test_tc_e2e_02_a_repeat_is_suppressed(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-E2E-02: повтор ``traffic_80`` в том же периоде — ``None``, одна строка и ни одной
    постановки (в том числе после завершения первой пары: ключ события, а не очереди)."""
    placed: list[str] = []
    real = vars(events)["enqueue"]  # обёртка фикстуры conftest — считаем поверх неё

    async def counting(conn: Any, queue: str, type_: str, payload: Any, key: str) -> Any:
        placed.append(key)
        return await real(conn, queue, type_, payload, key)

    monkeypatch.setattr(events, "enqueue", counting)
    async with rolled_back(pg_dsn, [{"url": HOOK, "secret": "s"}]) as conn:
        user, key = traffic_80()
        first = await emit(conn, EventType.TRAFFIC_80, user_id=user, dedup_key=key)
        assert first is not None and len(placed) == 2
        await conn.execute(
            "update jobs set status = 'done' where payload->>'event_id' = $1", str(first)
        )
        again = await emit(conn, EventType.TRAFFIC_80, user_id=user, dedup_key=key)
        assert again is None
        assert len(placed) == 2, f"повтор поставил задачи: {placed[2:]}"
        assert len(await events_with(conn, key)) == 1
        assert len(await jobs_of(conn, first)) == 2


async def test_tc_e2e_01_two_events_to_one_recipient_are_two_jobs(pg_dsn: str) -> None:
    """Ключ задачи webhook несёт событие: два события одному получателю — две задачи, пока
    первая стоит (без события в ключе ``enqueue`` вернул бы стоящую, и вторая доставка пропала
    бы). Ожидаемый ключ считается здесь, а не функцией под тестом."""
    digest = hashlib.sha256(HOOK.encode("utf-8")).hexdigest()
    async with rolled_back(pg_dsn, [{"url": HOOK, "secret": "s"}]) as conn:
        made = []
        for _ in range(2):
            user, key = traffic_80()
            event_id = await emit(conn, EventType.TRAFFIC_80, user_id=user, dedup_key=key)
            assert event_id is not None
            made.append(event_id)
        for event_id in made:
            assert f"notify:webhook:{event_id}:{digest}" in await jobs_of(conn, event_id)


@asynccontextmanager
async def committed(
    pg_dsn: str, webhooks: Any
) -> AsyncIterator[tuple[asyncpg.Connection, list[str]]]:
    """Стенд с закоммиченными настройками (TC-E2E-03 — две транзакции на двух подключениях);
    события с ключами из списка и прежнее значение ``settings.webhooks`` возвращаются в конце."""
    conn = await asyncpg.connect(pg_dsn)
    before = await conn.fetchval("select value::text from settings where key = 'webhooks'")
    keys: list[str] = []
    try:
        await set_webhooks(conn, webhooks)
        yield conn, keys
    finally:
        await conn.execute("delete from settings where key = 'webhooks'")
        if before is not None:
            await conn.execute(
                "insert into settings (key, value) values ('webhooks', $1::jsonb)", before
            )
        await conn.execute("delete from events where dedup_key = any($1::text[])", keys)
        await conn.close()


@pytest.mark.parametrize("outcome", ["commit", "rollback"])
async def test_tc_e2e_03_concurrent_emits_of_one_key(pg_dsn: str, outcome: str) -> None:
    """TC-E2E-03: второй ``emit`` с тем же ключом ждёт транзакцию первого на уникальном индексе.
    Первый закоммитил — второй получает ``None``, событие и пара задач одни; первый откатился —
    событие и задачи пишет второй."""
    async with committed(pg_dsn, [{"url": HOOK, "secret": "s"}]) as (conn, keys):
        user, key = traffic_80()
        keys.append(key)
        first_conn = await asyncpg.connect(pg_dsn)
        second_conn = await asyncpg.connect(pg_dsn)
        try:
            first_tx = first_conn.transaction()
            await first_tx.start()
            first = await emit(first_conn, EventType.TRAFFIC_80, user_id=user, dedup_key=key)

            async def second() -> uuid.UUID | None:
                async with second_conn.transaction():
                    return await emit(
                        second_conn, EventType.TRAFFIC_80, user_id=user, dedup_key=key
                    )

            racing = asyncio.create_task(second())
            try:
                await wait_blocked_by(conn, first_conn.get_server_pid())
                assert not racing.done()
            finally:
                await (first_tx.commit() if outcome == "commit" else first_tx.rollback())
            won = await racing
        finally:
            await first_conn.close()
            await second_conn.close()
        [row] = await events_with(conn, key)
        if outcome == "commit":
            assert (won, row["id"]) == (None, first)
        else:
            assert won is not None and row["id"] == won
        assert len(await jobs_of(conn, row["id"])) == 2


async def test_tc_e2e_04_broken_recipients_are_skipped(
    pg_dsn: str, caplog: pytest.LogCaptureFixture
) -> None:
    """TC-E2E-04: элементы без строки ``url`` и повтор адреса пропускаются, каждый — ERROR с
    номером; событие, почта и webhook исправному получателю поставлены (решение заказчика
    2026-11-26). Значение не списком — ERROR и ни одной задачи webhook."""
    hooks = [
        {"secret": "x"},
        "x",
        {"url": HOOK, "secret": "s"},
        {"url": ""},
        {"url": 123},
        {"url": HOOK, "secret": "other"},
    ]
    async with rolled_back(pg_dsn, hooks) as conn:
        with caplog.at_level(logging.ERROR, logger="app.notifications.events"):
            assert await webhook_urls(conn) == [HOOK]
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert [m.split(":")[0] for m in errors] == [
            f"settings.webhooks[{n}]" for n in (0, 1, 3, 4, 5)
        ], errors
        assert "повтор адреса" in errors[-1]
        user, key = traffic_80()
        event_id = await emit(conn, EventType.TRAFFIC_80, user_id=user, dedup_key=key)
        assert event_id is not None
        assert sorted(await jobs_of(conn, event_id)) == sorted(
            [f"notify:email:{event_id}", webhook_job_key(event_id, HOOK)]
        )
    caplog.clear()
    async with rolled_back(pg_dsn, {"url": HOOK}) as conn:
        user, key = traffic_80()
        with caplog.at_level(logging.ERROR, logger="app.notifications.events"):
            event_id = await emit(conn, EventType.TRAFFIC_80, user_id=user, dedup_key=key)
        assert list(await jobs_of(conn, event_id)) == [f"notify:email:{event_id}"]
        assert any("settings.webhooks" in r.getMessage() for r in caplog.records), caplog.text


@pytest.mark.parametrize(
    "value", ["https://hooks.example.test/vpn", 5, True], ids=["str", "int", "bool"]
)
async def test_tc_e2e_04_settings_not_a_list(
    pg_dsn: str, caplog: pytest.LogCaptureFixture, value: Any
) -> None:
    """Значение ``settings.webhooks`` не списком (строка, число, логическое) — ERROR и ни одной
    задачи webhook; операция вызывающего продолжается."""
    async with rolled_back(pg_dsn, value) as conn:
        user, key = traffic_80()
        with caplog.at_level(logging.ERROR, logger="app.notifications.events"):
            event_id = await emit(conn, EventType.TRAFFIC_80, user_id=user, dedup_key=key)
        assert list(await jobs_of(conn, event_id)) == [f"notify:email:{event_id}"]
        messages = [r.getMessage() for r in caplog.records]
        assert any(m.startswith("settings.webhooks: значение не список") for m in messages), (
            messages
        )


async def test_tc_e2e_04_a_long_address_does_not_break_the_caller(pg_dsn: str) -> None:
    """Адрес получателя длиннее строки индекса ключей задач (2704 байта): в ключе задачи —
    отпечаток адреса, задача поставлена, адрес целиком — в нагрузке; операция вызывающего
    продолжается (решение заказчика 2026-11-26)."""
    long_url = HOOK + "?token=" + "x" * 3000
    async with rolled_back(pg_dsn, [{"url": long_url, "secret": "s"}]) as conn:
        user, key = traffic_80()
        event_id = await emit(conn, EventType.TRAFFIC_80, user_id=user, dedup_key=key)
        assert event_id is not None
        placed = await jobs_of(conn, event_id)
        hook_key = webhook_job_key(event_id, long_url)
        assert len(hook_key) < 200 and placed[hook_key][2]["url"] == long_url
        assert await conn.fetchval("select 1") == 1, "транзакция вызывающего жива"


async def test_tc_e2e_05_a_rolled_back_caller_leaves_nothing(pg_dsn: str) -> None:
    """TC-E2E-05: ``emit`` в транзакции, которая откатилась, — ни строки ``events``, ни задач
    (outbox §5.4): уведомления об отменённой операции нет."""
    async with rolled_back(pg_dsn, [{"url": HOOK, "secret": "s"}]) as conn:
        user, key = traffic_80()
        operation = conn.transaction()  # вложенная — точка сохранения операции вызывающего
        await operation.start()
        event_id = await emit(conn, EventType.TRAFFIC_80, user_id=user, dedup_key=key)
        await operation.rollback()
        assert event_id is not None
        assert await events_with(conn, key) == []
        assert await jobs_of(conn, event_id) == {}


async def test_emit_runs_only_in_the_callers_transaction(pg_dsn: str) -> None:
    """Вне транзакции ``emit`` отказывает до записи: событие закоммитилось бы отдельно от
    операции и задач (outbox §5.4)."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        user, key = traffic_80()
        with pytest.raises(RuntimeError, match="транзакц"):
            await emit(conn, EventType.TRAFFIC_80, user_id=user, dedup_key=key)
        assert await events_with(conn, key) == []
    finally:
        await conn.close()


@pytest.mark.parametrize(
    ("event", "subject"),
    [
        (EventType.TRAFFIC_80, {}),  # пользовательскому событию нужен пользователь
        (EventType.TRAFFIC_80, {"user_id": "u", "node_id": "n"}),
        (EventType.NODE_OFFLINE, {"user_id": "u"}),  # операторскому — нода
        (EventType.NODE_OFFLINE, {"user_id": "u", "node_id": "n"}),
        (EventType.NODE_ADDRESS_CHANGED, {"user_id": "u"}),  # смене адреса — оба
    ],
    ids=["user-none", "user-with-node", "operator-user", "operator-both", "address-no-node"],
)
async def test_the_subject_must_fit_the_type(
    pg_dsn: str, event: EventType, subject: dict[str, str]
) -> None:
    """Адресат по типу §4.17: пользовательские — ``user_id`` (смена адреса — и ``node_id``),
    операторские — только ``node_id``; иначе ``ValueError`` до записи. Ключ построен из
    переданного адресата (тип, пользователь, нода, вид, эпизод), поэтому отвергнуть вызов может
    только правило адресата, а не сверка ключа."""
    ids = {"u": uuid.uuid4(), "n": uuid.uuid4()}
    args = {name: ids[value] for name, value in subject.items()}
    segments = [event.value, *(str(args[n]) for n in ("user_id", "node_id") if n in args)]
    if event in events.KINDS:
        segments.append("address")
    key = ":".join([*segments, "episode"])
    async with rolled_back(pg_dsn) as conn:
        with pytest.raises(ValueError, match=f"{event.value}: адресат"):
            await emit(
                conn,
                event,
                user_id=args.get("user_id"),
                node_id=args.get("node_id"),
                payload={"kind": "address"},
                dedup_key=key,
            )
        assert await events_with(conn, key) == []


async def test_the_key_must_belong_to_the_event(pg_dsn: str) -> None:
    """Ключ — того же события: типа (``traffic_95`` под ключом ``traffic_80``), адресата (ключ
    пользователя A при ``user_id`` B подавил бы настоящее событие A), вида (``payload.kind`` и
    вид ключа смены адреса) и не пустой после адресата; иначе ``ValueError`` до записи."""
    node = uuid.uuid4()
    owner = uuid.uuid4()
    address = {"user_id": owner, "node_id": node, "kind": "address", "version": 3}
    address_key = dedup_key(EventType.NODE_ADDRESS_CHANGED, **address)
    user, key = traffic_80()
    cases: list[tuple[EventType, uuid.UUID | None, uuid.UUID | None, Any, str]] = [
        (EventType.TRAFFIC_95, user, None, None, key),  # чужой тип
        (EventType.TRAFFIC_80, uuid.uuid4(), None, None, key),  # чужой пользователь
        (EventType.TRAFFIC_80, user, None, None, f"traffic_80:{user}:"),  # пустой эпизод
        (EventType.TRAFFIC_80, user, None, None, "traffic_80:"),
        (EventType.NODE_ADDRESS_CHANGED, owner, node, {"kind": "params"}, address_key),
        (EventType.NODE_ADDRESS_CHANGED, owner, node, None, address_key),  # нет вида
        (EventType.NODE_ADDRESS_CHANGED, owner, uuid.uuid4(), {"kind": "address"}, address_key),
    ]
    period = uuid.uuid4()
    activated = EventType.SUBSCRIPTION_ACTIVATED
    change_key = dedup_key(
        activated, user_id=owner, kind="plan_changed", period_id=period, changed_at=AT
    )
    activation_key = dedup_key(
        activated, user_id=owner, kind="activated", period_id=period, period_end=AT
    )
    # Вид нагрузки — не вид ключа: письмо «изменена» по ключу активации подавило бы настоящую
    # активацию, письмо «активирована» по ключу смены — сообщило бы не то.
    cases.append((activated, owner, None, {"kind": "activated"}, change_key))
    cases.append((activated, owner, None, {"kind": "plan_changed"}, activation_key))
    cases.append((activated, owner, None, None, activation_key))  # нет вида
    daily = dedup_key(EventType.RECONCILIATION_MISMATCH, kind="arithmetic", day=DAY)
    reconcile = EventType.RECONCILIATION_MISMATCH
    for wrong_kind in ({"kind": "cross_source"}, {"kind": "continuity"}, None):
        cases.append((reconcile, None, None, wrong_kind, daily))  # вид сверки не совпал
    cases.append((reconcile, None, node, {"kind": "arithmetic"}, daily))  # нода у общей сверки
    cross = dedup_key(reconcile, node_id=node, kind="cross_source", day=DAY)
    for wrong_kind in ({"kind": "continuity"}, {"kind": "arithmetic"}):
        cases.append((reconcile, None, node, wrong_kind, cross))  # вид нагрузки не вид ключа
    async with rolled_back(pg_dsn) as conn:
        # Общий стенд может держать настоящее событие этих суток (001.37d): в откатываемой
        # транзакции его строки нет.
        await conn.execute("delete from events where dedup_key = $1", daily)
        for event, user_id, node_id, payload, wrong in cases:
            with pytest.raises(ValueError, match=event.value):
                await emit(conn, event, user_id, node_id, payload, dedup_key=wrong)
        assert await events_with(conn, key) == []
        assert await events_with(conn, address_key) == []
        assert await events_with(conn, daily) == []
        assert await events_with(conn, change_key) == []
        assert await events_with(conn, activation_key) == []


async def test_every_plan_change_places_its_own_letter(pg_dsn: str) -> None:
    """Смена тарифа (решение заказчика 2026-11-26): каждая смена — своё событие вида
    ``plan_changed`` со своим письмом, даже при том же конце периода; повтор той же смены (тот же
    момент) подавлен. Ключи выписаны по формуле задачи, а не вычислены ``dedup_key``."""
    user, period = uuid.uuid4(), uuid.uuid4()
    later = AT + dt.timedelta(minutes=7)
    payload = {"kind": "plan_changed"}
    keys = [
        f"subscription_activated:{user}:plan_changed:{period}:2026-11-26T09:30:15.120000+00:00",
        f"subscription_activated:{user}:plan_changed:{period}:2026-11-26T09:37:15.120000+00:00",
    ]
    assert [
        dedup_key(
            EventType.SUBSCRIPTION_ACTIVATED,
            user_id=user,
            kind="plan_changed",
            period_id=period,
            changed_at=at,
        )
        for at in (AT, later)
    ] == keys
    async with rolled_back(pg_dsn) as conn:
        made = [
            await emit(conn, EventType.SUBSCRIPTION_ACTIVATED, user, None, payload, dedup_key=key)
            for key in keys
        ]
        again = await emit(
            conn, EventType.SUBSCRIPTION_ACTIVATED, user, None, payload, dedup_key=keys[0]
        )
        assert None not in made and made[0] != made[1] and again is None
        for event_id, key in zip(made, keys, strict=True):
            [row] = await events_with(conn, key)
            assert (row["id"], json.loads(row["payload"])) == (event_id, payload)
            assert list(await jobs_of(conn, event_id)) == [f"notify:email:{event_id}"]


async def test_the_arithmetic_event_has_no_node(pg_dsn: str) -> None:
    """Арифметическая сверка — общее событие без ноды (решение заказчика 2026-11-26): строка с
    ``node_id`` NULL, ключ ``reconciliation_mismatch:arithmetic:{сутки}``, повтор в сутки
    подавлен."""
    key = dedup_key(EventType.RECONCILIATION_MISMATCH, kind="arithmetic", day=DAY)
    payload = {"kind": "arithmetic", "day": DAY}
    async with rolled_back(pg_dsn) as conn:
        await conn.execute("delete from events where dedup_key = $1", key)
        made = await emit(
            conn, EventType.RECONCILIATION_MISMATCH, None, None, payload, dedup_key=key
        )
        again = await emit(
            conn, EventType.RECONCILIATION_MISMATCH, None, None, payload, dedup_key=key
        )
        [row] = await events_with(conn, key)
        assert (made, again, row["node_id"], row["user_id"]) == (row["id"], None, None, None)
        assert list(await jobs_of(conn, made)) == [f"notify:email:{made}"], "доставка поставлена"


async def test_a_payload_with_a_day_and_a_number_is_stored_as_json(pg_dsn: str) -> None:
    """Нагрузка сверки 001.37d — ``{kind, day, delta_pct}``: день и момент — ISO 8601, число
    ``numeric`` — строкой (точно), UUID — строкой; значение, которого ``jsonb`` не примет
    (объект, NaN, бесконечность, NUL в строке), — ``TypeError`` до записи, а не сорванная
    транзакция сверки."""
    node = uuid.uuid4()
    key = dedup_key(EventType.RECONCILIATION_MISMATCH, node_id=node, kind="cross_source", day=DAY)
    payload = {
        "kind": "cross_source",
        "day": DAY,
        "delta_pct": decimal.Decimal("0.51"),
        "ref": node,
    }
    other = dedup_key(
        EventType.RECONCILIATION_MISMATCH,
        node_id=node,
        kind="cross_source",
        day=DAY - dt.timedelta(days=1),
    )
    async with rolled_back(pg_dsn) as conn:
        await emit(conn, EventType.RECONCILIATION_MISMATCH, None, node, payload, dedup_key=key)
        [row] = await events_with(conn, key)
        assert json.loads(row["payload"]) == {
            "kind": "cross_source",
            "day": "2026-11-25",
            "delta_pct": "0.51",
            "ref": str(node),
        }
        bad_values = (
            object(),
            float("nan"),
            float("inf"),
            "a\x00b",
            {"nested": ["\x00"]},
            {"k\x00": 1},  # NUL в ключе
            ("\x00",),  # NUL в кортеже
            "\ud800",  # одиночный суррогат
            {"\udfff": 1},
        )
        for bad in bad_values:
            with pytest.raises(TypeError, match="не JSON"):
                await emit(
                    conn,
                    EventType.RECONCILIATION_MISMATCH,
                    None,
                    node,
                    {"kind": "cross_source", "raw": bad},
                    dedup_key=other,
                )
        assert await events_with(conn, other) == []
        assert await conn.fetchval("select 1") == 1, "транзакция вызывающего жива"
        moment = dedup_key(
            EventType.RECONCILIATION_MISMATCH,
            node_id=node,
            kind="cross_source",
            day=DAY - dt.timedelta(days=2),
        )
        await emit(
            conn,
            EventType.RECONCILIATION_MISMATCH,
            None,
            node,
            {"kind": "cross_source", "at": AT},
            dedup_key=moment,
        )
        [stamped] = await events_with(conn, moment)
        assert json.loads(stamped["payload"])["at"] == AT.isoformat()


async def test_a_continuity_gap_is_its_own_event(pg_dsn: str) -> None:
    """Сверка непрерывности (решение заказчика 2026-11-26): два разрыва ноды в одни сутки — два
    события; повторный проход по тому же разрыву — подавлен."""
    node = uuid.uuid4()
    keys = [
        dedup_key(EventType.RECONCILIATION_MISMATCH, node_id=node, kind="continuity", gap_start=at)
        for at in (AT, AT + dt.timedelta(hours=2), AT)
    ]
    async with rolled_back(pg_dsn) as conn:
        made = [
            await emit(
                conn,
                EventType.RECONCILIATION_MISMATCH,
                node_id=node,
                payload={"kind": "continuity"},
                dedup_key=key,
            )
            for key in keys
        ]
        assert made[0] is not None and made[1] is not None and made[2] is None
        for event_id in made[:2]:
            assert list(await jobs_of(conn, event_id)) == [f"notify:email:{event_id}"]


def test_the_jobs_go_to_the_background_queue() -> None:
    """Очередь доставки — ``background`` (§5.4): почта по §4.17 не задерживает отзыв доступа."""
    assert delivery.QUEUE == "background"
    assert (delivery.NOTIFY_EMAIL, delivery.NOTIFY_WEBHOOK) == ("notify.email", "notify.webhook")
