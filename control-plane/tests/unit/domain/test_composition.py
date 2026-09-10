"""Потоки конфигурации и состава: правила, которые в заглушке настоящие (задача 001.28)."""

from __future__ import annotations

import hashlib
import json
import uuid
from unittest import mock

import pytest
from app.domain import composition
from app.domain.composition import (
    REVOKING_STATES,
    STUB_CONFIG_CHECKSUM,
    STUB_CONFIG_DOCUMENT,
    STUB_GENERATION,
    STUB_USER_ACTIVE,
    STUB_USERS_SEQ,
    CompositionService,
    UserRow,
    config_checksum,
    stub_config,
    stub_credentials,
    stub_rows,
    xray_email,
)
from app.domain.nodes import stub_node
from app.errors import ApiError
from pydantic import ValidationError

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


def test_credentials_belong_to_the_pair_of_user_and_node() -> None:
    """§11.3: credentials уникальны на пару «пользователь × нода» — это компенсирующий контроль
    для недоверенной ноды. Значит: разные у разных пользователей, разные у одного пользователя
    на разных нодах, и не равные идентификатору пользователя (иначе они предсказуемы)."""
    first, second = uuid.uuid4(), uuid.uuid4()
    left, right = stub_credentials(NODE, first), stub_credentials(NODE, second)
    assert left.vless_uuid != right.vless_uuid
    assert left.trojan_password != right.trojan_password
    assert left.vless_uuid != first and right.vless_uuid != second
    elsewhere = stub_credentials(OTHER_NODE, first)
    assert elsewhere.vless_uuid != left.vless_uuid, "одна пара — одни credentials, другая — другие"


def test_only_rows_that_grant_access_carry_credentials() -> None:
    """Ключ нужен, чтобы добавить пользователя в inbound, — значит его несёт только строка,
    выдающая доступ (`active`). Снять доступ нода умеет по тегу, поэтому отзывные строки идут
    без ключей: иначе отфильтрованный поток `disabled`/`suspended` (§4.6 «Только отзывы») вёз бы
    живые ключи пар на ноду, которую администратор отключил или провайдер приостановил."""
    assert REVOKING_STATES == {"suspended_quota", "suspended_admin", "expired", "removed"}
    rows = stub_rows(NODE)
    assert [row.updated_seq for row in rows] == [1, 2, 3, 4], "номера изменений возрастают"
    assert max(row.updated_seq for row in rows) == STUB_USERS_SEQ, "курсор = последнее изменение"
    assert len({row.user_id for row in rows}) == len(rows), "строки о разных пользователях"
    assert [row.state for row in rows if row.credentials is None] == ["suspended_quota", "removed"]
    for row in rows:
        assert (row.credentials is not None) is (row.state == "active"), row.state
        # Тег — у каждой строки, включая отзывные: им нода применяет любую из них, и без него
        # отфильтрованный поток нечем было бы исполнить.
        assert row.xray_email == xray_email(row.user_id), row.state


def test_the_newest_change_does_not_remove_access() -> None:
    """Свойство фиксированных данных, без которого правило «курсор идёт по выборке до фильтра»
    не проверяется ничем: в фильтрующем статусе курсор (4) и наибольший номер среди выданных
    строк (3) обязаны расходиться."""
    rows = stub_rows(NODE)
    newest = max(rows, key=lambda row: row.updated_seq)
    assert newest.state not in REVOKING_STATES
    revoking = [row.updated_seq for row in rows if row.state in REVOKING_STATES]
    assert revoking and max(revoking) < newest.updated_seq


async def test_the_domain_refuses_an_unapproved_node_on_its_own() -> None:
    """Вентиль §4.6 стоит и в маршруте (`served_node`), и в домене. Дублирование намеренное:
    страж маршрута прикрывает домен, и без собственной проверки служба, вызванная откуда угодно
    ещё (001.29 и далее), выдала бы неподтверждённой ноде и состояние, и приём подтверждения."""
    service = CompositionService(None)
    pending = stub_node(status="pending")
    with pytest.raises(ApiError) as refused_state:
        await service.state_for(pending, 0, 0, STUB_GENERATION, False)
    assert refused_state.value.status == 403
    with pytest.raises(ApiError) as refused_ack:
        await service.ack(pending, 1, 4)
    assert refused_ack.value.status == 403
    # Вентиль — до всего остального: неподтверждённая нода с чужим поколением обязана получить
    # 403 «тебя не обслуживают», а не 409 «возьми снапшот», иначе она уйдёт в бесконечный ресинк
    # за состоянием, которого ей всё равно не выдадут.
    with pytest.raises(ApiError) as refused_first:
        await service.state_for(pending, 0, 0, STUB_GENERATION + 1, False)
    assert refused_first.value.status == 403
    await service.ack(stub_node(), 1, 4)  # подтверждённой ноде — без отказа


async def test_the_saved_resync_flag_reaches_the_agent() -> None:
    """`resync_required` в ответе — сохранённый признак ноды (`nodes.resync_required` §4.2.3), а
    не производная от статуса: §5.2 требует показать его **при выходе** из фильтрующего статуса,
    когда сам статус уже не фильтрует. Заглушка признак не хранит, поэтому добавляет к нему
    «этот ответ отфильтрован», — но взведённый в карточке обязана отдать."""
    flagged = stub_node().model_copy(update={"resync_required": True})
    answer = await CompositionService(None).state_for(flagged, 0, 0, STUB_GENERATION, False)
    assert answer is not None and answer.resync_required is True
    clean = await CompositionService(None).state_for(stub_node(), 0, 0, STUB_GENERATION, False)
    assert clean is not None and clean.resync_required is False, "у active своего признака нет"
    # Полный состав и есть выполненная пересинхронизация: объявить в нём «ты не синхронизирована»
    # значит послать ноду за снапшотом, который она только что получила. Условие остановки для
    # агента наблюдаемо (``users.full``), а свой статус он не знает.
    done = await CompositionService(None).state_for(flagged, 0, 0, STUB_GENERATION, True)
    assert done is not None and done.users.full is True
    assert done.resync_required is False, "снапшот закрывает признак, а не подтверждает его"
    filtered = await CompositionService(None).state_for(
        stub_node(status="disabled"), 0, 0, STUB_GENERATION, True
    )
    assert filtered is not None and filtered.users.full is False, "вентиль урезал — не полный"
    assert filtered.resync_required is True, "и признак остаётся: синхронизации не случилось"


async def test_a_snapshot_cursor_is_the_current_change_not_the_newest_row_sent() -> None:
    """Если самое свежее изменение — удаление пары, оно в снапшот не попадает, а курсор обязан
    уйти за него: иначе номер удалённой пары придёт ноде ещё раз следующей же дельтой."""
    node = stub_node()
    rows = stub_rows(node.id)
    rows.append(
        UserRow(
            user_id=uuid.UUID("00000000-0000-7000-8000-0000000000a5"),
            state="removed",
            xray_email=xray_email(uuid.UUID("00000000-0000-7000-8000-0000000000a5")),
            quota_grant_bytes=0,
            blocked_ips=[],
            updated_seq=max(row.updated_seq for row in rows) + 1,
        )
    )
    with mock.patch.object(composition, "stub_rows", return_value=rows):
        answer = await CompositionService(None).state_for(node, 0, 0, STUB_GENERATION, True)
    assert answer is not None
    sent = [row.updated_seq for row in answer.users.rows]
    assert max(sent) < answer.users.seq == max(row.updated_seq for row in rows)


async def test_the_answer_is_built_for_the_node_that_asked() -> None:
    """Состав и его ключи выдаются по запрошенной ноде, а не по фиксированной: credentials
    уникальны на пару «пользователь × нода» (§11.3), и подстановка константы в выдаче свела бы
    компенсирующий контроль на нет."""
    first = await CompositionService(None).state_for(
        stub_node(node_id=NODE), 0, 0, STUB_GENERATION, False
    )
    second = await CompositionService(None).state_for(
        stub_node(node_id=OTHER_NODE), 0, 0, STUB_GENERATION, False
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


def test_the_row_model_refuses_keys_where_access_is_not_granted() -> None:
    """Инвариант «ключи ровно у выдающей доступ строки» держит модель, а не подбор фиксированных
    строк: в 001.29 строки соберёт JOIN, и джойн, вернувший ключ на приостановленную строку,
    молча отправил бы живые ключи на отключённую ноду."""
    keys = stub_credentials(NODE, STUB_USER_ACTIVE)
    for state in sorted(REVOKING_STATES):
        with pytest.raises(ValidationError):
            UserRow(
                user_id=STUB_USER_ACTIVE,
                state=state,
                xray_email=xray_email(STUB_USER_ACTIVE),
                quota_grant_bytes=0,
                blocked_ips=[],
                updated_seq=1,
                credentials=keys,
            )
    with pytest.raises(ValidationError):
        UserRow(
            user_id=STUB_USER_ACTIVE,
            state="active",
            xray_email=xray_email(STUB_USER_ACTIVE),
            quota_grant_bytes=0,
            blocked_ips=[],
            updated_seq=1,
            credentials=None,
        )
