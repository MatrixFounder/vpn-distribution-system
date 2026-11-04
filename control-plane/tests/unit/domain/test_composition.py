"""Потоки конфигурации и состава: правила выдачи на раскладке фикстур без базы (задачи 001.28,
001.29) и выделение номера изменения на живой базе (TC-UNIT-01 001.29)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any

import asyncpg
import pytest
from app.domain.composition import (
    REVOKING_STATES,
    STUB_CONFIG_CHECKSUM,
    STUB_CONFIG_DOCUMENT,
    STUB_GENERATION,
    CompositionService,
    NodeComposition,
    RowsRead,
    StoredRow,
    UserRow,
    config_checksum,
    next_users_seq,
    stub_config,
    xray_email,
)
from app.domain.nodes import stub_node
from app.errors import ApiError
from pydantic import ValidationError

from tests._agent import (
    LAYOUT,
    LAYOUT_SEQ,
    USER_ACTIVE,
    RecordedComposition,
    layout_credentials,
)
from tests.e2e._catalog import billing_group, catalog
from tests.e2e._catalog import node as catalog_node
from tests.e2e._reporting import wait_blocked_by

# Обе ноды намеренно отличны от ``nodes.STUB_NODE_ID``: страж, у которого «произвольная нода»
# побайтово равна фиксированной, не отличит выдачу по запрошенной ноде от выдачи по константе.
NODE = uuid.UUID("00000000-0000-7000-8000-0000000000b9")
OTHER_NODE = uuid.UUID("11111111-1111-7111-8111-111111111111")


def test_checksum_is_sha256_of_the_canonical_form() -> None:
    """Правило контрольной суммы записано в `contracts/agent_v1/README.md` и повторено здесь
    независимо от реализации: вторая сторона обмена считает сумму по описанию, а не по коду."""
    document = {"b": 1, "a": {"я": "ю", "x": [1, 2]}}
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert canonical == '{"a":{"x":[1,2],"я":"ю"},"b":1}'
    assert config_checksum(document) == hashlib.sha256(canonical.encode()).hexdigest()


def test_checksum_does_not_depend_on_key_order() -> None:
    """Сумма считается от документа, а не от текста: перестановка ключей её не меняет, а любое
    изменение значения — меняет. Значение для каркаса заглушки посчитано один раз."""
    assert config_checksum({"a": 1, "b": 2}) == config_checksum({"b": 2, "a": 1})
    assert config_checksum({"a": 1}) != config_checksum({"a": 2})
    assert stub_config().checksum == STUB_CONFIG_CHECKSUM == config_checksum(STUB_CONFIG_DOCUMENT)


def test_the_config_document_carries_no_clients() -> None:
    """§5.2: поток конфигурации идёт без credentials пользователей. Для каркаса это значит
    пустой список клиентов в каждом inbound — генератор 001.27 обязан сохранить свойство."""
    for inbound in STUB_CONFIG_DOCUMENT["inbounds"]:
        assert inbound["settings"]["clients"] == [], inbound["tag"]


def test_xray_email_is_the_stable_tag_of_the_user() -> None:
    """Тег §4.2.3: `u` и идентификатор пользователя — по нему сводятся счётчики §5.9."""
    user_id = uuid.UUID("00000000-0000-7000-8000-0000000000a1")
    assert xray_email(user_id) == f"u{user_id}"


async def test_only_rows_that_grant_access_carry_credentials() -> None:
    """Ключ нужен, чтобы добавить пользователя в inbound, — значит его несёт только строка,
    выдающая доступ (`active`). Снять доступ нода умеет по тегу, поэтому отзывные строки идут
    без ключей: иначе отфильтрованный поток `disabled`/`suspended` (§4.6 «Только отзывы») вёз бы
    живые ключи пар на ноду, которую администратор отключил или провайдер приостановил. Запись
    credentials у снятой пары в раскладке есть (`suspended_quota`) — в ответ она не идёт."""
    assert REVOKING_STATES == {"suspended_quota", "suspended_admin", "expired", "removed"}
    answer = await RecordedComposition().state_for(
        stub_node(node_id=NODE), 0, 0, STUB_GENERATION, False, 0
    )
    assert answer is not None
    rows = answer.users.rows
    assert [row.updated_seq for row in rows] == [1, 2, 3, 4], "номера изменений возрастают"
    assert max(row.updated_seq for row in rows) == LAYOUT_SEQ, "курсор = последнее изменение"
    assert len({row.user_id for row in rows}) == len(rows), "строки о разных пользователях"
    assert [row.state for row in rows if row.credentials is None] == ["suspended_quota", "removed"]
    for row in rows:
        assert (row.credentials is not None) is (row.state == "active"), row.state
        # Тег — у каждой строки, включая отзывные: им нода применяет любую из них, и без него
        # отфильтрованный поток нечем было бы исполнить.
        assert row.xray_email == xray_email(row.user_id), row.state


def test_the_newest_change_does_not_remove_access() -> None:
    """Свойство раскладки, без которого правило «курсор идёт по выборке до фильтра» не
    проверяется ничем: в фильтрующем статусе курсор (4) и наибольший номер среди выданных
    строк (3) обязаны расходиться."""
    _, newest_state, newest_seq, _ = max(LAYOUT, key=lambda row: row[2])
    assert newest_state not in REVOKING_STATES and newest_seq == LAYOUT_SEQ
    revoking = [seq for _, state, seq, _ in LAYOUT if state in REVOKING_STATES]
    assert revoking and max(revoking) < newest_seq


async def test_the_domain_refuses_an_unapproved_node_on_its_own() -> None:
    """Вентиль §4.6 стоит и в маршруте (`served_node`), и в домене. Дублирование намеренное:
    страж маршрута прикрывает домен, и без собственной проверки служба, вызванная откуда угодно
    ещё (001.29 и далее), выдала бы неподтверждённой ноде и состояние, и приём подтверждения."""
    service = CompositionService(None)
    pending = stub_node(status="pending")
    with pytest.raises(ApiError) as refused_state:
        await service.state_for(pending, 0, 0, STUB_GENERATION, False, 0)
    assert refused_state.value.status == 403
    with pytest.raises(ApiError) as refused_ack:
        await service.ack(pending, 1, 4)
    assert refused_ack.value.status == 403
    # Вентиль — до всего остального: неподтверждённая нода с чужим поколением обязана получить
    # 403 «тебя не обслуживают», а не 409 «возьми снапшот», иначе она уйдёт в бесконечный ресинк
    # за состоянием, которого ей всё равно не выдадут.
    with pytest.raises(ApiError) as refused_first:
        await service.state_for(pending, 0, 0, STUB_GENERATION + 1, False, 0)
    assert refused_first.value.status == 403
    await service.ack(stub_node(), 1, 4)  # подтверждённой ноде — без отказа


async def test_the_saved_resync_flag_reaches_the_agent() -> None:
    """`resync_required` в ответе — сохранённый признак ноды (`nodes.resync_required` §4.2.3), а
    не производная от статуса: §5.2 требует показать его **при выходе** из фильтрующего статуса,
    когда сам статус уже не фильтрует. Урезанный вентилем ответ признак взводит."""
    flagged = RecordedComposition(resync_required=True)
    answer = await flagged.state_for(stub_node(), 0, 0, STUB_GENERATION, False, 0)
    assert answer is not None and answer.resync_required is True
    clean = await RecordedComposition().state_for(stub_node(), 0, 0, STUB_GENERATION, False, 0)
    assert clean is not None and clean.resync_required is False, "у active своего признака нет"
    # Полный состав и есть выполненная пересинхронизация: объявить в нём «ты не синхронизирована»
    # значит послать ноду за снапшотом, который она только что получила. Условие остановки для
    # агента наблюдаемо (``users.full``), а свой статус он не знает.
    done = await flagged.state_for(stub_node(), 0, 0, STUB_GENERATION, True, 0)
    assert done is not None and done.users.full is True
    assert done.resync_required is False, "снапшот закрывает признак, а не подтверждает его"
    disabled = RecordedComposition()
    filtered = await disabled.state_for(
        stub_node(status="disabled"), 0, 0, STUB_GENERATION, True, 0
    )
    assert filtered is not None and filtered.users.full is False, "вентиль урезал — не полный"
    assert filtered.resync_required is True, "и признак остаётся: синхронизации не случилось"
    assert disabled.resync_required is True, "признак взведён в хранилище ноды, не только в ответе"
    assert [(row.updated_seq, row.state) for row in filtered.users.rows] == [
        (2, "suspended_quota"),
        (3, "removed"),
    ], "урезанный «снапшот» читается дельтой и везёт удаление строкой"


class TrailingRemoval(RecordedComposition):
    """Раскладка, где самое свежее изменение (5) — удаление пары."""

    async def _read(self, node_id: uuid.UUID, users_seq: int, rows: RowsRead) -> NodeComposition:
        base = await super()._read(node_id, users_seq, rows)
        removed = StoredRow(
            user_id=uuid.UUID("00000000-0000-7000-8000-0000000000a5"),
            state="removed",
            quota_grant_bytes=0,
            blocked_ips=[],
            updated_seq=LAYOUT_SEQ + 1,
            vless_uuid_enc=None,
            trojan_password_enc=None,
            credentials_version=None,
        )
        extra = [removed] if rows == "delta" else []
        return NodeComposition(
            users_seq=LAYOUT_SEQ + 1, resync_required=False, rows=[*base.rows, *extra]
        )


async def test_a_snapshot_cursor_is_the_current_change_not_the_newest_row_sent() -> None:
    """Если самое свежее изменение — удаление пары, оно в снапшот не попадает, а курсор обязан
    уйти за него: иначе номер удалённой пары придёт ноде ещё раз следующей же дельтой."""
    answer = await TrailingRemoval().state_for(stub_node(), 0, 0, STUB_GENERATION, True, 0)
    assert answer is not None
    sent = [row.updated_seq for row in answer.users.rows]
    assert max(sent) < answer.users.seq == LAYOUT_SEQ + 1


async def test_the_answer_is_built_for_the_node_that_asked() -> None:
    """Состав и его ключи выдаются по запрошенной ноде, а не по фиксированной: credentials
    уникальны на пару «пользователь × нода» (§11.3), и подстановка константы в выдаче свела бы
    компенсирующий контроль на нет."""
    first = await RecordedComposition().state_for(
        stub_node(node_id=NODE), 0, 0, STUB_GENERATION, False, 0
    )
    second = await RecordedComposition().state_for(
        stub_node(node_id=OTHER_NODE), 0, 0, STUB_GENERATION, False, 0
    )
    assert first is not None and second is not None
    keys = [
        (row.user_id, row.credentials.vless_uuid)
        for row in first.users.rows
        if row.credentials is not None
    ]
    others = {
        (row.user_id, row.credentials.vless_uuid)
        for row in second.users.rows
        if row.credentials is not None
    }
    assert keys, "в дельте есть строки, выдающие доступ"
    assert not others & set(keys), "у другой ноды — другие ключи для тех же пользователей"
    assert {user for user, _ in keys} == {user for user, _ in others}, "пользователи те же"
    assert keys == [
        (user_id, layout_credentials(NODE, user_id).vless_uuid)
        for user_id, state, _, _ in LAYOUT
        if state == "active"
    ], "ключ — расшифрованный ключ своей пары"


def test_the_row_model_refuses_keys_where_access_is_not_granted() -> None:
    """Инвариант «ключи ровно у выдающей доступ строки» держит модель, а не подбор фиксированных
    строк: в 001.29 строки соберёт JOIN, и джойн, вернувший ключ на приостановленную строку,
    молча отправил бы живые ключи на отключённую ноду."""
    keys = layout_credentials(NODE, USER_ACTIVE)
    for state in sorted(REVOKING_STATES):
        with pytest.raises(ValidationError):
            UserRow(
                user_id=USER_ACTIVE,
                state=state,
                xray_email=xray_email(USER_ACTIVE),
                quota_grant_bytes=0,
                blocked_ips=[],
                updated_seq=1,
                credentials=keys,
            )
    with pytest.raises(ValidationError):
        UserRow(
            user_id=USER_ACTIVE,
            state="active",
            xray_email=xray_email(USER_ACTIVE),
            quota_grant_bytes=0,
            blocked_ips=[],
            updated_seq=1,
            credentials=None,
        )


async def test_tc_unit_01_users_seq_is_issued_in_commit_order(pg_dsn: str) -> None:
    """``next_users_seq``: номер выделяет ``UPDATE nodes … RETURNING`` под блокировкой строки
    ноды, поэтому вторая транзакция ждёт коммита первой и номера идут в порядке коммитов
    (data-model §4.2.3 «Выделение `updated_seq`»: номер последовательности выдаётся до коммита, и
    агент, продвинув курсор, пропустил бы строку, закоммиченную позже меньшим номером)."""
    async with catalog(pg_dsn) as conn:
        node_id = await catalog_node(conn, await billing_group(conn, "u29"), 29)
        first, second = await asyncpg.connect(pg_dsn), await asyncpg.connect(pg_dsn)
        try:
            earlier = first.transaction()
            await earlier.start()
            issued_first = await next_users_seq(first, node_id)
            later = second.transaction()
            await later.start()
            waiting = asyncio.create_task(next_users_seq(second, node_id))
            await wait_blocked_by(conn, first.get_server_pid())
            await asyncio.sleep(0.2)  # коммит первой задержан: вторая обязана ждать его
            assert not waiting.done(), "вторая транзакция получила номер до коммита первой"
            await earlier.commit()
            issued_second = await asyncio.wait_for(waiting, timeout=5)
            await later.commit()
        finally:
            await first.close()
            await second.close()
        assert issued_second == issued_first + 1
        assert issued_second == await conn.fetchval(
            "select desired_users_seq from nodes where id = $1", node_id
        )


class _InsideTransaction:
    """Подключение вызывающего, уже открывшего транзакцию; любое другое обращение валит тест."""

    def is_in_transaction(self) -> bool:
        return True

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"обращение к подключению: {name}")


async def test_the_executor_refuses_a_connection_inside_a_transaction() -> None:
    """Исполнитель публикации работает на подключении вне транзакции: внутри чужой транзакции
    транзакции нод стали бы точками сохранения, блокировка пользователя снялась бы до внешнего
    коммита, и следующий запуск выбрал бы кандидатов без незакоммиченных строк. Отказ — до
    любого обращения к базе."""
    with pytest.raises(RuntimeError, match="вне транзакции"):
        await CompositionService(None).publish_user_on(_InsideTransaction(), uuid.uuid4())
