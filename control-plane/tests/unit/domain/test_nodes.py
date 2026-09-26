"""``app.domain.nodes`` без базы: источник и стойкость bootstrap-токена и токена identity (Н-24,
§7.2), срок токена литералом, фиксированная карточка для заглушек других служб, передача причины
ручного статуса. Поведение на данных (хеши, погашение, отзыв, поиск по identity) —
``tests/e2e/test_nodes.py`` на живой базе (001.25)."""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib
import ipaddress
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from typing import cast, get_args, get_type_hints
from unittest import mock

import pytest
from app.api.admin.nodes import set_manual_status
from app.domain import nodes
from app.domain.statuses import ON_REENROLLMENT, ON_REVOCATION, StatusService
from app.errors import ApiError
from app.security.ca import InternalCA
from app.security.deps import CurrentAdmin
from app.security.sessions import Session
from pydantic import ValidationError

from tests._pki import make_ca, make_csr


def test_bootstrap_token_comes_from_the_os_source_not_the_module_generator() -> None:
    """Токен даёт identity ноды, поэтому берётся из ``secrets`` (``os.urandom``), а не из
    ``random``: подмена источника делает выдачу предсказуемой — страж ловит и обход
    ``secrets.token_urlsafe``, и уменьшение числа байт (ревью 001.24; тот же класс, что C-1
    задачи 001.18 для кодов)."""
    with mock.patch.object(secrets, "token_urlsafe", return_value="предсказуемо") as source:
        assert nodes.generate_bootstrap_token() == "предсказуемо", "выдача идёт через secrets"
    assert source.call_args.args == (nodes.BOOTSTRAP_TOKEN_BYTES,), source.call_args
    assert nodes.BOOTSTRAP_TOKEN_BYTES * 8 == 256, "§7.2: 256 бит"
    # Генератор Мерсенна в выдаче не участвует: его getrandbits подменён на ошибку, боевая
    # выдача её не задевает, а random.choice / randbelow — задели бы.
    with mock.patch("random.Random.getrandbits", side_effect=AssertionError("mt19937")):
        issued = {nodes.generate_bootstrap_token() for _ in range(64)}
    assert len(issued) == 64, "боевая выдача не повторяется"
    assert all(len(token) == 43 for token in issued), "base64url от 32 байт — 43 символа"
    alphabet = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert set().union(*issued) <= alphabet


def test_identity_token_is_as_strong_as_the_bootstrap_token() -> None:
    """Токен identity предъявляется в каждом запросе ноды и хранится хешем (§7.1): тот же
    источник ОС и те же 256 бит, что у bootstrap-токена, — литералом, а не ссылкой на соседнюю
    константу, чтобы её уменьшение не уменьшило молча и этот."""
    with mock.patch.object(secrets, "token_urlsafe", return_value="предсказуемо") as source:
        assert nodes.generate_identity_token() == "предсказуемо"
    assert source.call_args.args == (32,), source.call_args
    issued = {nodes.generate_identity_token() for _ in range(64)}
    assert len(issued) == 64 and all(len(token) == 43 for token in issued)


def test_bootstrap_token_lives_sixty_minutes() -> None:
    """Н-24: не более 60 минут — литералом; срок считает база от момента выдачи
    (``tests/e2e/test_nodes.py``)."""
    assert dt.timedelta(minutes=60) == nodes.BOOTSTRAP_TOKEN_TTL


def test_the_fixed_card_for_other_stubs_does_not_contradict_the_model() -> None:
    """Фиксированная карточка, на которой проверяют себя заглушки состава, учёта и отчётов, не
    смешивает сущности: тарифицируемая группа (R-18, ровно одна) и группа доступа — разные
    таблицы §4.2.3, значит и разные идентификаторы; до обмена токена агент ничего не
    предъявил."""
    assert nodes.STUB_ACCESS_GROUP_ID != nodes.STUB_BILLING_GROUP_ID
    assert nodes.STUB_NODE_IN.billing_group_id == nodes.STUB_BILLING_GROUP_ID
    assert nodes.STUB_NODE_IN.access_group_ids == [nodes.STUB_ACCESS_GROUP_ID]
    pending = nodes.stub_node(status="pending")
    assert pending.agent_version is None and pending.last_heartbeat_at is None


class NoPool:
    """Пул, которого нет: обращение к нему — провал теста."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"обращение к базе: {name}")


async def test_enrollment_without_a_ca_fails_before_touching_the_database() -> None:
    """Служба без ключа CA — такая, как у панели (только сертификат, ``anchor_pem``), — обменять
    токен не может: отказ до базы, иначе погашение токена началось бы раньше, чем выяснилось,
    что подписать лист нечем. Служба и без сертификата не выдаёт токен: ответу нечего было бы
    дать агенту в качестве якоря (роаст 001.25, раунды 2–3)."""
    panel = nodes.NodeService(NoPool(), anchor_pem="-----BEGIN CERTIFICATE-----")
    with pytest.raises(RuntimeError, match="без CA"):
        await panel.enroll(
            "x" * 43, "csr", "0.1.0", "26.9.1", ipaddress.IPv4Address("198.51.100.7")
        )
    bare = nodes.NodeService(NoPool())
    with pytest.raises(RuntimeError, match="без сертификата CA"):
        await bare.issue_bootstrap_token(uuid.uuid4(), uuid.uuid4())


async def test_manual_status_reason_reaches_the_domain() -> None:
    """Причина ручного перевода объявлена контрактом (``ManualStatusIn.reason``) и обязана
    дойти до домена: в ``audit_log`` её пишет 001.48, но маршрут не вправе её выбрасывать."""
    seen: dict[str, object] = {}

    class RecordingNodes(nodes.NodeService):
        async def set_manual_status(
            self,
            node_id: uuid.UUID,
            status: nodes.ManualStatus | None,
            admin_id: uuid.UUID,
            reason: str | None = None,
        ) -> nodes.Node:
            seen.update(node_id=node_id, status=status, admin_id=admin_id, reason=reason)
            return nodes.stub_node(status=status or "active", node_id=node_id)

    node_id, admin_id = uuid.uuid4(), uuid.uuid4()
    admin = CurrentAdmin(id=admin_id, session=cast(Session, None))
    body = nodes.ManualStatusIn(status="maintenance", reason="замена диска")
    answer = await set_manual_status(node_id, body, admin, RecordingNodes(pool=None))
    assert seen == {
        "node_id": node_id,
        "status": "maintenance",
        "admin_id": admin_id,
        "reason": "замена диска",
    }, seen
    assert answer.id == node_id and answer.status == "maintenance"


class _Transaction:
    """Транзакция подменного подключения: пока открыта, операторы записываются с её параметрами."""

    def __init__(self, owner: Recorder, options: dict[str, object]) -> None:
        self._owner = owner
        self._options = options

    async def __aenter__(self) -> Recorder:
        self._owner.open.append(self._options)
        return self._owner

    async def __aexit__(self, *exc: object) -> None:
        self._owner.open.pop()


class Recorder:
    """Подключение, записывающее SQL и связанные параметры по порядку и отвечающее заготовками по
    тексту запроса; у каждого оператора записаны параметры транзакции, в которой он выполнен
    (``None`` — вне транзакции)."""

    def __init__(self, answers: Callable[[str], object]) -> None:
        self.sql: list[str] = []
        self.args: list[tuple[object, ...]] = []
        self.scopes: list[dict[str, object] | None] = []
        self.open: list[dict[str, object]] = []
        self._answers = answers

    def transaction(self, **options: object) -> _Transaction:
        return _Transaction(self, options)

    async def __aenter__(self) -> Recorder:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def acquire(self) -> Recorder:
        return self

    async def _answer(self, query: str, args: tuple[object, ...]) -> object:
        self.sql.append(query)
        self.args.append(args)
        self.scopes.append(self.open[-1] if self.open else None)
        return self._answers(query)

    async def fetchrow(self, query: str, *args: object) -> object:
        return await self._answer(query, args)

    async def fetchval(self, query: str, *args: object) -> object:
        return await self._answer(query, args)

    async def fetch(self, query: str, *args: object) -> object:
        return await self._answer(query, args)

    async def execute(self, query: str, *args: object) -> object:
        return await self._answer(query, args)

    def order_is_lock_then_moment(self, *, lookups: int = 0) -> None:
        """Первые ``lookups`` операторов — чтения без блокировки; затем строка ноды под
        блокировкой, затем момент операции; ни одной записи раньше блокировки."""
        lock = self.sql.index(nodes._LOCK_NODE)
        assert lock == lookups, self.sql[: lock + 1]
        assert all(q.lstrip().lower().startswith("select") for q in self.sql[:lock]), self.sql
        assert "statement_timestamp()" in self.sql[lock + 1], "момент — после блокировки"
        assert "now()" not in self.sql[lock], "у блокировки нет своего момента"


MOMENT = dt.datetime(2026, 9, 23, 12, tzinfo=dt.UTC)
SOURCE = ipaddress.ip_address("198.51.100.7")


def answers(query: str) -> object:
    if query == nodes._LOCK_NODE:
        return {"gone": False, "status": "pending"}
    if query == nodes._MOMENT:
        return MOMENT
    if query.startswith("insert into bootstrap_tokens"):
        return MOMENT + nodes.BOOTSTRAP_TOKEN_TTL
    if query.startswith("select node_id from bootstrap_tokens"):
        return uuid.uuid4()
    if "statement_timestamp() as at" in query:
        return {"id": uuid.uuid4(), "used": True, "annulled": False, "expired": False, "at": MOMENT}
    if query.startswith("select cert_fingerprint from node_identities"):
        return []
    return None  # записи; чтения состояния — «нет ноды»


async def test_every_writer_locks_the_node_before_anything_else() -> None:
    """Порядок, на котором держится обмен (роаст 001.25, раунд 1): выдача токена, отзыв и
    подтверждение первым оператором берут строку ноды, вторым — момент операции
    (``statement_timestamp()``), и ни одна запись не идёт раньше блокировки; обмен до неё только
    читает, какой ноде принадлежит токен. Аннулирование до блокировки дало бы взаимную
    блокировку со встречным обменом, момент до неё — сравнение с устаревшими часами. Сквозные
    тесты ловят это на гонках, этот — на каждом вызове."""
    ca_key, ca_cert = make_ca()
    calls: list[tuple[Callable[[nodes.NodeService], Awaitable[object]], int]] = [
        (lambda s: s.issue_bootstrap_token(uuid.uuid4(), uuid.uuid4()), 0),
        (lambda s: s.revoke_identity(uuid.uuid4(), uuid.uuid4()), 0),
        (lambda s: s.approve(uuid.uuid4(), uuid.uuid4(), "0" * 64), 0),
        (lambda s: s.enroll("x" * 43, make_csr(), "0.1.0", "26.9.1", SOURCE), 1),
    ]
    for call, lookups in calls:
        conn = Recorder(answers)
        service = nodes.NodeService(conn, InternalCA.from_pem(ca_key, ca_cert))
        with contextlib.suppress(ApiError):
            await call(service)
        conn.order_is_lock_then_moment(lookups=lookups)


async def test_expiry_is_judged_at_the_operation_moment() -> None:
    """Срок токена и identity сверяется с моментом операции после блокировки ноды, а не с
    ``now()`` — моментом начала транзакции: обмен, начавший транзакцию до истечения токена и
    дождавшийся ноды после, иначе принял бы истёкший токен, подтверждение — истёкшую identity
    (роаст 001.25, раунд 4: страж порядка видел лишь, что ``statement_timestamp()`` в операторе
    есть, и ``expires_at <= now()`` рядом с ним проходил). Чтение токена обмена сравнивает срок
    со ``statement_timestamp()``, подтверждение — с моментом, прочитанным после блокировки
    (параметр), и ни один оператор писателей после блокировки не зовёт ``now()``."""

    def approvable(query: str) -> object:
        if query.startswith("select cert_fingerprint from node_identities"):
            return [{"cert_fingerprint": "0" * 64}]
        return answers(query)

    ca = InternalCA.from_pem(*make_ca())
    calls: list[tuple[Callable[[nodes.NodeService], Awaitable[object]], Callable[[str], object]]]
    calls = [
        (lambda s: s.issue_bootstrap_token(uuid.uuid4(), uuid.uuid4()), answers),
        (lambda s: s.revoke_identity(uuid.uuid4(), uuid.uuid4()), answers),
        (lambda s: s.approve(uuid.uuid4(), uuid.uuid4(), "0" * 64), approvable),
        (lambda s: s.enroll("x" * 43, make_csr(), "0.1.0", "26.9.1", SOURCE), answers),
    ]
    recorded = []
    for call, answer in calls:
        conn = Recorder(answer)
        with contextlib.suppress(ApiError, TypeError, ValidationError):
            await call(nodes.NodeService(conn, ca))
        lock = conn.sql.index(nodes._LOCK_NODE)
        assert not [q for q in conn.sql[lock:] if "now()" in q], conn.sql[lock:]
        recorded.append(conn)
    approve, enroll = recorded[2], recorded[3]
    token_read = next(q for q in enroll.sql if "statement_timestamp() as at" in q)
    assert "expires_at <= statement_timestamp() as expired" in " ".join(token_read.split())
    liveness = [
        (query, args)
        for query, args in zip(approve.sql, approve.args, strict=True)
        if query.startswith("select cert_fingerprint from node_identities")
    ]
    assert len(liveness) == 1, approve.sql
    query, args = liveness[0]
    assert "expires_at > $2" in query and args[1] == MOMENT, (query, args)


async def test_a_token_is_consumed_once_even_by_a_path_that_skipped_the_lock() -> None:
    """Одноразовость держит и сама запись погашения, а не только порядок блокировок (роаст
    001.25, раунд 2): погашается лишь непогашенный токен, и если строка уже погашена (путь,
    забывший блокировку ноды, или гонка, которой порядок не закрыл) — ``UPDATE 0`` и отказ
    ``token_used``, а не вторая identity по одному токену."""

    def consumed_elsewhere(query: str) -> object:
        if "statement_timestamp() as at" in query:
            return {
                "id": uuid.uuid4(),
                "used": False,
                "annulled": False,
                "expired": False,
                "at": MOMENT,
            }
        if query.startswith("update bootstrap_tokens set used_at"):
            return "UPDATE 0"
        return answers(query)

    conn = Recorder(consumed_elsewhere)
    service = nodes.NodeService(conn, InternalCA.from_pem(*make_ca()))
    with pytest.raises(ApiError) as refused:
        await service.enroll("x" * 43, make_csr(), "0.1.0", "26.9.1", SOURCE)
    assert refused.value.code == "token_used"
    consume = next(q for q in conn.sql if q.startswith("update bootstrap_tokens set used_at"))
    assert "and used_at is null" in consume, consume
    assert not any(q.startswith("insert into node_identities") for q in conn.sql), conn.sql


async def test_the_state_is_read_in_one_snapshot() -> None:
    """Состояние — три чтения (нода, последняя identity, последний токен); обмен, зафиксированный
    между ними, иначе показал бы администратору на шаге 6 сочетание статуса и identity, которого
    в базе не было (роаст 001.25, раунд 2). Поэтому все три — в одной транзакции ``repeatable
    read`` только на чтение."""

    def node_row(query: str) -> object:
        if query.lstrip().startswith("select id, status, status_changed_at"):
            return {
                "id": uuid.uuid4(),
                "status": "pending",
                "status_changed_at": MOMENT,
                "desired_config_version": 0,
                "applied_config_version": 0,
                "desired_users_seq": 0,
                "applied_users_seq": 0,
                "applied_at": None,
                "resync_required": False,
                "last_heartbeat_at": None,
                "missed_heartbeats": 0,
                "ok_heartbeats": 0,
                "agent_version": None,
                "xray_version": None,
            }
        return None

    conn = Recorder(node_row)
    await nodes.NodeService(conn).state(uuid.uuid4())
    snapshot = {"isolation": "repeatable_read", "readonly": True}
    assert len(conn.sql) == 3, conn.sql
    assert conn.scopes == [snapshot] * 3, "все три чтения — внутри одной транзакции-снимка"


async def test_status_writes_follow_the_status_rules() -> None:
    """Каждый статус, который пишет служба нод, — по правилам §4.6 (роаст 001.25, раунд 3; строки
    событий identity — решение заказчика 2026-09-24): обмен токена — ``ON_REENROLLMENT``, отзыв —
    ``ON_REVOCATION`` (оба из любого статуса), подтверждение — переход матрицы из ``pending``.
    Статус читается из оператора записи — литералом в SQL или связанным параметром: статус,
    разошедшийся с правилами, красит тест (роаст 001.25, раунд 4: константы событий передаются
    параметром, а не повторяются литералом)."""
    identity_row = {
        "generation": 1,
        "cert_fingerprint": "0" * 64,
        "cert_serial": "0A1B2C",
        "issued_at": MOMENT,
        "expires_at": MOMENT + dt.timedelta(days=90),
        "revoked_at": None,
        "enrolled_from": SOURCE,
    }

    def full_path(query: str) -> object:
        if "statement_timestamp() as at" in query:
            return {
                "id": uuid.uuid4(),
                "used": False,
                "annulled": False,
                "expired": False,
                "at": MOMENT,
            }
        if query.startswith("update bootstrap_tokens set used_at"):
            return "UPDATE 1"
        if query.startswith("insert into node_identities"):
            return identity_row
        if query.startswith("select cert_fingerprint from node_identities"):
            return [{"cert_fingerprint": "0" * 64}]
        return answers(query)

    def written(conn: Recorder) -> list[str]:
        found = []
        for query, args in zip(conn.sql, conn.args, strict=True):
            match = re.match(r"update nodes set status = (?:'(\w+)'|\$(\d+))", query)
            if match:
                found.append(match.group(1) or str(args[int(match.group(2)) - 1]))
        return found

    ca = InternalCA.from_pem(*make_ca())
    writes = {}
    calls: dict[str, Callable[[nodes.NodeService], Awaitable[object]]] = {
        "enroll": lambda s: s.enroll("x" * 43, make_csr(), "0.1.0", "26.9.1", SOURCE),
        "revoke": lambda s: s.revoke_identity(uuid.uuid4(), uuid.uuid4()),
        "approve": lambda s: s.approve(uuid.uuid4(), uuid.uuid4(), "0" * 64),
    }
    for name, call in calls.items():
        conn = Recorder(full_path)
        # После записи статуса подменное подключение не отвечает на чтение карточки и состояния —
        # то, что идёт следом, здесь не проверяется.
        with contextlib.suppress(ApiError, TypeError, ValidationError):
            await call(nodes.NodeService(conn, ca))
        writes[name] = written(conn)
    assert writes["enroll"] == [ON_REENROLLMENT]
    assert writes["revoke"] == [ON_REVOCATION]
    assert len(writes["approve"]) == 1
    assert StatusService.can_transition("pending", cast(nodes.NodeStatus, writes["approve"][0]))


async def test_the_panel_service_holds_the_ca_certificate_not_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Панели ключ CA не нужен: выдаче токена — только сертификат, якорь bootstrap-команды.
    Зависимость маршрутов панели — ``get_ca_pem`` (строка), и служба нод панели собирается без
    ``ca`` — объекта, умеющего подписывать (роаст 001.25, раунд 3)."""
    from app.security.ca import get_ca_pem

    # ``app.api.admin`` отдаёт под именем ``nodes`` роутер — модуль берётся из реестра импорта.
    admin_nodes = importlib.import_module("app.api.admin.nodes")

    async def no_pool() -> object:
        return NoPool()

    monkeypatch.setattr(admin_nodes, "db_pool", no_pool)
    service = await admin_nodes.get_node_service(anchor_pem="PEM")
    assert (service._ca, service._anchor_pem) == (None, "PEM")
    hints = get_type_hints(admin_nodes.get_node_service, include_extras=True)
    assert set(hints) == {"anchor_pem", "return"}, hints
    assert [meta.dependency for meta in get_args(hints["anchor_pem"])[1:]] == [get_ca_pem]
