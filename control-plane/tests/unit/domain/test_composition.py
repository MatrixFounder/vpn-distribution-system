"""Потоки конфигурации и состава: правила выдачи на раскладке фикстур без базы (задачи 001.28,
001.29, 001.29c) и выделение номера изменения на живой базе (TC-UNIT-01 001.29)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any

import asyncpg
import pytest
from app.config import SecretError
from app.domain.composition import (
    REVOKING_STATES,
    STUB_AGENT_VERSION,
    STUB_GENERATION,
    CompositionService,
    NodeComposition,
    RowsRead,
    StoredRow,
    UserRow,
    config_checksum,
    credentials_bound,
    next_users_seq,
    node_agent,
    xray_email,
)
from app.domain.inbounds import stub_private_key, xray_key
from app.domain.nodes import stub_node
from app.domain.statuses import INT4_MAX
from app.errors import ApiError
from app.security.crypto import CipherMismatch, FieldCipher
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError

from tests._agent import (
    LAYOUT,
    LAYOUT_CONFIG_VERSION,
    LAYOUT_SEQ,
    USER_ACTIVE,
    RecordedComposition,
    layout_config,
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
    изменение значения — меняет."""
    assert config_checksum({"a": 1, "b": 2}) == config_checksum({"b": 2, "a": 1})
    assert config_checksum({"a": 1}) != config_checksum({"a": 2})


async def test_the_issued_config_carries_the_keys_of_the_node_that_asked() -> None:
    """001.29c: выданный документ — строка версии ноды с закрытым ключом REALITY своей ноды и
    своего inbound в каждом ``realitySettings``; у двух нод ключи различаются, а
    ``config.checksum`` — сумма выданного документа с ключами, она же записана в строке. §5.2:
    состав inbound пуст — credentials пользователей потоком конфигурации не идут."""
    issued = {}
    for node_id in (NODE, OTHER_NODE):
        answer = await RecordedComposition().state_for(
            stub_node(node_id=node_id), 0, 0, STUB_GENERATION, False, 0
        )
        assert answer is not None and answer.config is not None
        config = answer.config
        assert config.version == LAYOUT_CONFIG_VERSION
        assert (
            config.checksum == config_checksum(config.document) == layout_config(node_id).checksum
        )
        for inbound in config.document["inbounds"]:
            reality = inbound["streamSettings"]["realitySettings"]
            assert reality["privateKey"] == xray_key(stub_private_key(node_id, inbound["tag"]))
            assert inbound["settings"]["users"] == [], inbound["tag"]
        issued[node_id] = [
            inbound["streamSettings"]["realitySettings"]["privateKey"]
            for inbound in config.document["inbounds"]
        ]
    assert len(set(issued[NODE])) == 3, "у каждого inbound свой ключ"
    assert not set(issued[NODE]) & set(issued[OTHER_NODE]), "у другой ноды — другие ключи"


async def _foreign_key(conn: Any, node_id: uuid.UUID, tag: str) -> bytes:
    """Источник ключа, отдающий ключ другой ноды: ключевой материал сменился без новой версии."""
    return stub_private_key(OTHER_NODE, tag)


async def test_a_withheld_config_alone_is_no_reason_to_answer() -> None:
    """Выдача конфигурации отказала (расхождение суммы), а по составу и разделу ``agent`` нода
    на текущих курсорах: изменений, которые нода может применить, нет — ``None`` (204, удержание),
    а не пустой 200, на который нода тут же спросила бы снова. Та же нода, отставшая и по
    составу, получает состав без раздела ``config``."""
    service = RecordedComposition(reality_key=_foreign_key)
    node = stub_node(node_id=NODE)
    assert await service.state_for(node, 0, LAYOUT_SEQ, STUB_GENERATION, False, 1) is None
    answer = await service.state_for(node, 0, 0, STUB_GENERATION, False, STUB_AGENT_VERSION)
    assert answer is not None and answer.config is None
    assert [row.updated_seq for row in answer.users.rows] == [1, 2, 3, 4]


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

    async def _read(
        self, node_id: uuid.UUID, users_seq: int, rows: RowsRead, config_after: int | None
    ) -> NodeComposition:
        base = await super()._read(node_id, users_seq, rows, config_after)
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
            users_seq=LAYOUT_SEQ + 1,
            resync_required=False,
            rows=[*base.rows, *extra],
            config_version=base.config_version,
            config=base.config,
            agent_version=base.agent_version,
            max_conn_per_ip=base.max_conn_per_ip,
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


@pytest.mark.parametrize(
    "executor", ["publish_user_on", "publish_node_on", "rotate_node_credentials_on"]
)
async def test_the_executor_refuses_a_connection_inside_a_transaction(executor: str) -> None:
    """Исполнители публикации (пользователя — 001.29, ноды — 001.29b) работают на подключении вне
    транзакции: внутри чужой транзакции транзакции пар стали бы точками сохранения, блокировка
    пользователя снялась бы до внешнего коммита, и следующий запуск выбрал бы кандидатов без
    незакоммиченных строк. Отказ — до любого обращения к базе. Исполнитель ротации credentials
    ноды (001.29d) открывает свою транзакцию тоже: ротация и постановка публикации коммитятся
    вместе, а внутри чужой транзакции стали бы её частью."""
    with pytest.raises(RuntimeError, match="вне транзакции"):
        await getattr(CompositionService(None), executor)(_InsideTransaction(), uuid.uuid4())


# Известный ответ (WI-38): контейнеры, снятые один раз фиксированным ключом для фиксированной
# пары. Любое изменение привязки credentials (порядок частей, таблица, колонка) или состава
# связанных данных их не откроет — иначе после развёртывания такого изменения каждая пара
# выглядела бы подменённой, и ноды получили бы отзыв всех пользователей.
KAT_KEY = bytes(range(32))
KAT_USER = uuid.UUID("00000000-0000-7000-8000-0000000000a1")
KAT_NODE = uuid.UUID("00000000-0000-7000-8000-0000000000b1")
KAT_VLESS = bytes.fromhex(
    "01357f8d16ca5fd8ef6f40e53400d1bc70b6eb992ea077eea24e6191bc75b0e2a3e070103a1c41e082670088c3"
)
KAT_TROJAN = bytes.fromhex(
    "01c203b0ad4ea90149261a95f5525f5654c35b05548b5d1067671592849edbb8dbbfc564bbb690b0595417ca3c"
    "92eb3c8781"
)


def test_the_credentials_binding_is_pinned_by_a_known_answer() -> None:
    """Привязка credentials пары — ровно таблица, колонка, пользователь, нода в этом порядке;
    снятые контейнеры открываются ею, колонки не взаимозаменяемы."""
    assert credentials_bound("vless_uuid_enc", KAT_USER, KAT_NODE) == (
        b'["node_user_credentials","vless_uuid_enc",'
        b'"00000000-0000-7000-8000-0000000000a1","00000000-0000-7000-8000-0000000000b1"]'
    )
    cipher = FieldCipher(KAT_KEY)
    vless = cipher.decrypt(KAT_VLESS, bound=credentials_bound("vless_uuid_enc", KAT_USER, KAT_NODE))
    assert vless == bytes(range(16))
    trojan = cipher.decrypt(
        KAT_TROJAN, bound=credentials_bound("trojan_password_enc", KAT_USER, KAT_NODE)
    )
    assert trojan == b"known-trojan-password"
    with pytest.raises(InvalidTag):
        cipher.decrypt(
            KAT_VLESS, bound=credentials_bound("trojan_password_enc", KAT_USER, KAT_NODE)
        )


class _OutsideTransaction:
    """Подключение вне транзакции: любое обращение, кроме вопроса о транзакции, валит тест."""

    def is_in_transaction(self) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"постановка обратилась к подключению вне транзакции: {name}")


@pytest.mark.parametrize(
    "method",
    [
        "enqueue_publish_user",
        "enqueue_publish_node",
        "remove_user",
        "publish_config",
        "enqueue_rotate_node",
        "rotate_user_credentials",
    ],
)
async def test_a_publication_is_placed_only_inside_a_transaction(method: str) -> None:
    """Публикация ставится в транзакции бизнес-операции (§5.4, Н-14): вне транзакции постановка
    пережила бы откат операции или потерялась бы при крахе между коммитом и постановкой —
    ``RuntimeError`` до обращения к базе (001.29b). Версия конфигурации — так же (001.29c): вне
    транзакции изменения она пережила бы его откат, и нода получила бы документ, которого
    изменение не оставило. Ротация credentials (001.29d) — так же: ротация пользователя коммитится
    вместе с перевыпуском токена, постановка ротации ноды — вместе с отзывом identity."""
    with pytest.raises(RuntimeError, match="транзакции"):
        await getattr(CompositionService(pool=None), method)(_OutsideTransaction(), uuid.uuid4())


# Иные исключения источника: дефект кода, ошибка базы и таймаут подключения в 001.27, отсутствие
# файла ключа процесса (``SecretError`` — родитель ``CipherMismatch``, его бросает
# ``FieldCipher.from_settings``).
UNEXPECTED: dict[str, Exception] = {
    "RuntimeError": RuntimeError("источник сломан"),
    "KeyError": KeyError("нет строки inbound_secrets"),
    "AttributeError": AttributeError("источник сломан"),
    "TypeError": TypeError("источник сломан"),
    "OSError": OSError("файл ключа недоступен"),
    "TimeoutError": TimeoutError("база не ответила"),
    "SecretError": SecretError("файл ключа недоступен"),
    "PostgresError": asyncpg.exceptions.UndefinedTableError("relation does not exist"),
    # Листовые классы сценариев раунда 3 (H2): лист ловит расширение перехвата до любого предка —
    # таймаут запроса (``OperatorInterventionError``), обрыв соединения (``ConnectionError``),
    # разрыв подключения (``PostgresConnectionError``), ошибка клиента (``InterfaceError``).
    "QueryCanceledError": asyncpg.exceptions.QueryCanceledError("statement timeout"),
    "ConnectionResetError": ConnectionResetError("соединение сброшено"),
    "ConnectionDoesNotExistError": asyncpg.exceptions.ConnectionDoesNotExistError("closed"),
    "InterfaceError": asyncpg.exceptions.InterfaceError("cannot perform operation"),
}


@pytest.mark.parametrize("error", sorted(UNEXPECTED))
async def test_an_unexpected_source_error_is_not_taken_for_a_refused_key(error: str) -> None:
    """Отказ источника ключа — только ``InvalidTag``, ``ValueError`` и ``CipherMismatch``: иное
    исключение источника (дефект кода, ошибка базы, таймаут, нет файла ключа) пробрасывается, а
    не превращается в ответ без ``config`` с ложной записью «ключ не получен» (роаст 001.29c,
    раунды 1–3). Проверяется на двойнике ``RecordedComposition``; живой путь выдачи —
    ``tests/e2e/test_composition.py::test_29c_an_unexpected_source_error_answers_500``."""
    raised = UNEXPECTED[error]

    async def broken(conn: Any, node_id: uuid.UUID, tag: str) -> bytes:
        raise raised

    with pytest.raises(type(raised)):
        await RecordedComposition(reality_key=broken).state_for(
            stub_node(node_id=NODE), 0, 0, STUB_GENERATION, False, 0
        )


async def test_the_key_source_reads_inside_the_publishing_transaction(pg_dsn: str) -> None:
    """Источник ключа получает подключение вызывающего и видит его незакоммиченную версию: в
    001.27 ротация ключевой пары пишет новый ключ и публикует версию одной транзакцией, и сумма
    обязана считаться по новому ключу этой транзакции, а не по прежнему с другого подключения."""
    async with catalog(pg_dsn) as conn:
        node_id = await catalog_node(conn, await billing_group(conn, "u29c"), 191)
        seen: list[tuple[bool, int]] = []

        async def watching(source_conn: Any, node: uuid.UUID, tag: str) -> bytes:
            version = await source_conn.fetchval(
                "select desired_config_version from nodes where id = $1", node
            )
            seen.append((source_conn.is_in_transaction(), version))
            return stub_private_key(node, tag)

        async with conn.transaction():
            version = await CompositionService(None, reality_key=watching).publish_config(
                conn, node_id
            )
        assert version == 1
        assert seen == [(True, 1)] * 3, "три inbound REALITY, версия своей транзакции"


REFUSED: dict[str, Exception] = {
    "InvalidTag": InvalidTag(),
    "ValueError": ValueError("контейнер не разбирается"),
    "CipherMismatch": CipherMismatch("ключ процесса не тот"),
}


@pytest.mark.parametrize("refusal", sorted(REFUSED))
async def test_a_refused_key_rolls_the_publication_back(pg_dsn: str, refusal: str) -> None:
    """Отказ источника ключа при публикации — каждый из трёх — пробрасывается и откатывает
    транзакцию вызывающего: ни версии, ни строки. Версия, закоммиченная без суммы по ключам, не
    выдавалась бы никогда."""
    raised = REFUSED[refusal]

    async def refusing(conn: Any, node_id: uuid.UUID, tag: str) -> bytes:
        raise raised

    async with catalog(pg_dsn) as conn:
        node_id = await catalog_node(conn, await billing_group(conn, "u29c"), 192)
        with pytest.raises(type(raised)):
            async with conn.transaction():
                await CompositionService(None, reality_key=refusing).publish_config(conn, node_id)
        assert (
            await conn.fetchval("select desired_config_version from nodes where id = $1", node_id)
            == 0
        )
        assert (
            await conn.fetchval(
                "select count(*) from node_config_versions where node_id = $1", node_id
            )
            == 0
        )


async def test_config_versions_are_issued_in_commit_order(pg_dsn: str) -> None:
    """Версию выделяет ``UPDATE nodes … RETURNING`` под блокировкой строки ноды: вторая
    публикация той же ноды ждёт коммита первой и получает следующую версию — не ту же (нарушение
    ключа строки, 500 бизнес-операции) и не раньше коммита первой."""
    async with catalog(pg_dsn) as conn:
        node_id = await catalog_node(conn, await billing_group(conn, "u29c"), 193)
        service = CompositionService(None)
        first, second = await asyncpg.connect(pg_dsn), await asyncpg.connect(pg_dsn)
        try:
            earlier = first.transaction()
            await earlier.start()
            issued_first = await service.publish_config(first, node_id)
            later = second.transaction()
            await later.start()
            waiting = asyncio.create_task(service.publish_config(second, node_id))
            await wait_blocked_by(conn, first.get_server_pid())
            await asyncio.sleep(0.2)  # коммит первой задержан: вторая обязана ждать его
            assert not waiting.done(), "вторая публикация получила версию до коммита первой"
            await earlier.commit()
            issued_second = await asyncio.wait_for(waiting, timeout=5)
            await later.commit()
        finally:
            await first.close()
            await second.close()
        assert (issued_first, issued_second) == (1, 2)
        rows = await conn.fetch(
            "select config_version from node_config_versions where node_id = $1 "
            "order by config_version",
            node_id,
        )
        assert [row["config_version"] for row in rows] == [1, 2]


async def test_publishing_for_a_missing_node_is_a_lookup_error(pg_dsn: str) -> None:
    """Ноды нет — ``LookupError`` до записи строки: версия без ноды не пишется."""
    missing = uuid.uuid4()
    async with catalog(pg_dsn) as conn:
        async with conn.transaction():
            with pytest.raises(LookupError):
                await CompositionService(None).publish_config(conn, missing)
        assert (
            await conn.fetchval(
                "select count(*) from node_config_versions where node_id = $1", missing
            )
            == 0
        )


def test_the_agent_section_passes_the_model_bounds() -> None:
    """``node_agent`` (001.29f) собирает раздел проверкой модели ``StateAgent``: значения колонок
    ноды проходят те же границы, что поля заглушки. Версия или лимит вне границ —
    ``ValidationError``, а не раздел, который маршрут отдал бы агенту (ответ сериализуется без
    ``response_model``)."""

    def stored(version: int, limit: int) -> NodeComposition:
        return NodeComposition(
            users_seq=0,
            resync_required=False,
            rows=[],
            config_version=1,
            config=None,
            agent_version=version,
            max_conn_per_ip=limit,
        )

    section = node_agent(stored(3, 8))
    assert (section.version, section.max_conn_per_ip) == (3, 8)
    for version, limit in ((0, 8), (INT4_MAX + 1, 8), (3, 0), (3, -5), (3, INT4_MAX + 1)):
        with pytest.raises(ValidationError):
            node_agent(stored(version, limit))
