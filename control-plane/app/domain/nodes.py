"""Парк нод и enrollment (постановка §3.2, §4.5, §4.6; data-model.md §4.2.3 ``nodes``,
``bootstrap_tokens``, ``node_identities``; security.md §7.1; UC-01, UC-12; R-02, R-44).

Задача 001.25 — ввод ноды и её identity на базе: запись ноды (UC-01 шаг 1), bootstrap-токен
(шаги 2–3, A1; Н-24), обмен токена на identity (шаг 5), состояние для сверки признаков (шаг 6),
подтверждение (шаг 7), отзыв identity (UC-12 шаг 1, UC-01 A2; Н-31) и поиск ноды по предъявленной
identity для раздела ``/agent/v1``. Изменение записи, вывод из эксплуатации и ручные статусы не
реализованы — 501 ``not_implemented``, а не фиксированная карточка: при настоящих чтении и
подтверждении ответ «успех» без записи говорил бы администратору неправду (владельцы — отчёт
001.25); статусы по heartbeat — 001.30, публикация состава — 001.29.

Правила модуля:

- bootstrap-токен и токен identity хранятся только хешами SHA-256 (§7.2) и показываются один раз;
- все, кто пишет токены и identity ноды (выдача, обмен, отзыв), и подтверждение, которое читает
  действующую identity, сначала берут строку ноды ``FOR NO KEY UPDATE`` — один порядок
  блокировок (страж — ``tests/unit/domain/test_nodes.py``): встречные выдача и обмен ждут друг
  друга, а не ловят взаимную блокировку, и два одновременных обмена одним токеном не выпустят
  двух identity (второй увидит погашение первого). ``NO KEY``: ключ ноды не меняется, и вставки
  строк со ссылкой на ноду (``KEY SHARE``) эта блокировка не держит;
- сертификат подписывается только после проверок токена и в той же транзакции: отказ CA
  откатывает погашение, и токен не сгорает на негодном CSR;
- повторная выдача токена аннулирует прежний неиспользованный, отзыв identity — тоже: иначе
  токен, утёкший вместе с VPS, выпустил бы identity уже после отзыва. Аннулирование, погашение и
  отзыв — отметки (``annulled_at``, ``used_at``, ``revoked_at``), и годность решают они, а не
  сравнение моментов: часы базы могут шагнуть назад (NTP, возобновление VM), отметка — нет;
  часы сравниваются только со сроком жизни токена и identity;
- новый обмен отзывает прежние identity ноды (§4.5 «Пересоздание ноды»); ротация с окном
  перекрытия Н-28 — отдельный путь 001.31;
- время одно — базы, и берётся **после** блокировки ноды (``statement_timestamp()``
  следующего оператора), а не ``now()`` — момент начала транзакции: иначе обмен, дождавшийся
  ноды после отзыва, записал бы ``used_at`` и ``issued_at`` раньше ``revoked_at`` отзыва, которого
  ждал, и сверил бы срок токена с моментом до ожидания. Так у писателей одной ноды моменты идут
  в порядке блокировок (пока часы не шагнули назад), и этот момент задаёт срок токена,
  погашение, срок сертификата и ``issued_at``/``expires_at``/``revoked_at`` identity (часы
  приложения и базы расходятся, а nginx проверяет срок сертификата по часам хоста базы и
  прокси).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import secrets
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

import asyncpg
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    IPvAnyAddress,
    StringConstraints,
    field_validator,
)

from app.db.pool import transaction
from app.domain.users import hash_token
from app.errors import ApiError, not_implemented
from app.security.ca import InternalCA

NodeStatus = Literal[
    "pending",
    "provisioning",
    "active",
    "degraded",
    "offline",
    "maintenance",
    "disabled",
    "suspended",
]
# Ручные статусы §4.6: ставит и снимает только администратор; имеют приоритет над автоматикой.
ManualStatus = Literal["maintenance", "disabled", "suspended"]

BOOTSTRAP_TOKEN_TTL = dt.timedelta(minutes=60)  # Н-24: не более 60 минут
BOOTSTRAP_TOKEN_BYTES = 32  # 256 бит (§7.2)
IDENTITY_TOKEN_BYTES = 32  # токен identity — та же стойкость, что у bootstrap-токена

Code = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{1,63}$")]
Country = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Name = Annotated[str, StringConstraints(min_length=1, max_length=100)]
Fqdn = Annotated[str, StringConstraints(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.-]+$")]
# Версию агента и Xray администратор сверяет глазами (UC-01 шаг 6), а присылает их
# недоверенная нода (§11.3): алфавит закрыт печатаемым ASCII без управляющих символов —
# U+202E внутри строки перерисовал бы «0.0.1» как «1.0.0» в панели.
Version = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[0-9A-Za-z][0-9A-Za-z.+_-]*$")
]


class NodeIn(BaseModel):
    """Запись ноды (UC-01 шаг 1): код, имя, география, провайдер, адреса, группы доступа и
    тарифицируемая группа (R-18: ровно одна, NOT NULL), пропускная способность как порог
    аномалии (§5.9), лимит соединений с адреса (Н-29), юридический профиль (Р-10), экономика
    (§4.15, необязательно)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    code: Code = Field(description="уникальный код, например JP-Tokyo-01")
    name: Name
    country: Country = Field(description="ISO 3166-1 alpha-2")
    city: Name
    provider: Name
    public_ipv4: ipaddress.IPv4Address
    public_ipv6: ipaddress.IPv6Address | None = None
    fqdn: Fqdn | None = None
    billing_group_id: uuid.UUID
    access_group_ids: list[uuid.UUID] = Field(default_factory=list)
    bandwidth_mbps: int = Field(gt=0, description="порт VPS; порог аномалии отчётов §5.9")
    max_conn_per_ip: int = Field(gt=0, description="лимит одновременных соединений с адреса Н-29")
    legal_profile: dict[str, Any] = Field(default_factory=dict)
    monthly_cost: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    currency: Currency | None = None
    provider_account: Name | None = None
    cost_valid_from: dt.date | None = None
    cost_valid_to: dt.date | None = None
    traffic_included_bytes: int | None = Field(default=None, ge=0)
    traffic_overage_cost: Decimal | None = Field(
        default=None, ge=0, max_digits=12, decimal_places=4
    )


class NodePatch(BaseModel):
    """Частичное изменение: любое подмножество полей ``NodeIn``; ``null`` сбрасывает
    необязательное поле, для NOT NULL полей — 422; непереданное поле не меняется."""

    model_config = ConfigDict(str_strip_whitespace=True)

    code: Code | None = None
    name: Name | None = None
    country: Country | None = None
    city: Name | None = None
    provider: Name | None = None
    public_ipv4: ipaddress.IPv4Address | None = None
    public_ipv6: ipaddress.IPv6Address | None = None
    fqdn: Fqdn | None = None
    billing_group_id: uuid.UUID | None = None
    access_group_ids: list[uuid.UUID] | None = None
    bandwidth_mbps: int | None = Field(default=None, gt=0)
    max_conn_per_ip: int | None = Field(default=None, gt=0)
    legal_profile: dict[str, Any] | None = None
    monthly_cost: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    currency: Currency | None = None
    provider_account: Name | None = None
    cost_valid_from: dt.date | None = None
    cost_valid_to: dt.date | None = None
    traffic_included_bytes: int | None = Field(default=None, ge=0)
    traffic_overage_cost: Decimal | None = Field(
        default=None, ge=0, max_digits=12, decimal_places=4
    )

    @field_validator(
        "code",
        "name",
        "country",
        "city",
        "provider",
        "public_ipv4",
        "billing_group_id",
        "access_group_ids",
        "bandwidth_mbps",
        "max_conn_per_ip",
        "legal_profile",
    )
    @classmethod
    def _not_null(cls, value: object) -> object:
        """Поля NOT NULL в базе можно не передавать, но нельзя передать как ``null``."""
        if value is None:
            raise ValueError("поле не может быть null")
        return value


class Node(NodeIn):
    """Карточка ноды: запись плюс состояние §4.6 и версии, предъявленные агентом (UC-01 шаг 6)."""

    id: uuid.UUID
    status: NodeStatus
    status_changed_at: dt.datetime
    last_heartbeat_at: dt.datetime | None
    agent_version: Version | None
    xray_version: Version | None
    multiplier_milli: int | None = Field(default=None, description="действующее переопределение")
    resync_required: bool
    created_at: dt.datetime
    decommissioned_at: dt.datetime | None


class BootstrapToken(BaseModel):
    """Одноразовый bootstrap-токен (Н-24): показывается один раз, привязан к ноде; повторная
    выдача аннулирует прежний. ``ca_pem`` — сертификат внутреннего CA, якорь доверия
    bootstrap-команды (001.61): администратор переносит его на VPS своим каналом вместе с
    токеном, и агент проверяет им серверный сертификат enrollment-server до того, как отдать
    токен (security.md §7.1) — иначе токен и сам CA пришли бы по непроверенному TLS."""

    node_id: uuid.UUID
    token: str = Field(description="256 бит, base64url; в базе хранится только хеш")
    expires_at: dt.datetime
    ca_pem: str = Field(description="сертификат CA (PEM): им агент проверяет enrollment-server")


class ManualStatusIn(BaseModel):
    """Ручной статус §4.6; ``null`` — снять ручной статус (далее автоматика: ``active`` или
    ``offline`` по heartbeat)."""

    status: ManualStatus | None = Field(description="maintenance | disabled | suspended | null")
    reason: Annotated[str, StringConstraints(max_length=500)] | None = None


class ApproveIn(BaseModel):
    """Подтверждение ноды (UC-01 шаг 7) — того, что администратор сверил на шаге 6: отпечаток
    identity из ``GET …/state``. Другая identity к этому моменту (новый обмен) — 409."""

    model_config = ConfigDict(extra="forbid")

    cert_fingerprint: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="отпечаток SHA-256 сверенной identity"
    )


class Identity(BaseModel):
    """Identity ноды (§5.3, ``node_identities``): поколение, отпечаток, сроки, отзыв и адрес, с
    которого нода обменяла токен (UC-01 шаг 6: администратор сверяет его до подтверждения)."""

    generation: int = Field(ge=1)
    cert_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$", description="SHA-256 DER, hex")
    cert_serial: str = Field(
        pattern=r"^(?:[0-9A-F]{2})+$",
        description="серийный номер листа в форме $ssl_client_serial nginx (карта отказа 001.66)",
    )
    issued_at: dt.datetime
    expires_at: dt.datetime
    revoked_at: dt.datetime | None
    enrolled_from: IPvAnyAddress = Field(description="адрес источника обмена токена")


class Enrollment(Identity):
    """Результат обмена bootstrap-токена (UC-01 шаг 5): сертификат, CA, токен identity."""

    node_id: uuid.UUID
    client_cert_pem: str
    ca_pem: str
    identity_token: str


class Cursors(BaseModel):
    desired_config_version: int = Field(ge=0)
    applied_config_version: int = Field(ge=0)
    desired_users_seq: int = Field(ge=0)
    applied_users_seq: int = Field(ge=0)
    applied_at: dt.datetime | None


class Heartbeat(BaseModel):
    last_at: dt.datetime | None
    missed: int = Field(ge=0, description="подряд пропущенных интервалов, Н-15")
    ok: int = Field(ge=0, description="подряд успешных интервалов, Н-15")


class NodeState(BaseModel):
    """Состояние ноды для сверки UC-01 шаг 6 и разбора UC-07: статус, курсоры потоков §5.2,
    heartbeat, версии, последняя identity (отозванная тоже — видно, когда отозвана) и последний
    bootstrap-токен."""

    node_id: uuid.UUID
    status: NodeStatus
    status_changed_at: dt.datetime
    cursors: Cursors
    resync_required: bool
    heartbeat: Heartbeat
    agent_version: Version | None
    xray_version: Version | None
    identity: Identity | None
    bootstrap_token_expires_at: dt.datetime | None
    bootstrap_token_used_at: dt.datetime | None
    bootstrap_token_annulled_at: dt.datetime | None


def generate_bootstrap_token() -> str:
    """256 случайных бит из ОС в base64url (43 символа). Источник — ``secrets`` (``os.urandom``),
    не ``random``: токен даёт identity ноды (§7.2, Н-24)."""
    return secrets.token_urlsafe(BOOTSTRAP_TOKEN_BYTES)


def generate_identity_token() -> str:
    """Токен identity ноды — те же 256 бит из ОС, что у bootstrap-токена (§7.1)."""
    return secrets.token_urlsafe(IDENTITY_TOKEN_BYTES)


# Отказы обмена токена (UC-01 A1). Статус один — 401, коды разные: подобрать 256-битный токен по
# коду отказа нельзя, а оператору код говорит, что делать — выпустить новый токен (истёк или
# аннулирован новым токеном либо отзывом) или разобраться, кто обменял токен (использован: сам
# агент, чей ответ потерян, или кто-то другой — адрес источника в состоянии, шаг 6, A2).
def token_invalid() -> ApiError:
    return ApiError("token_invalid", "bootstrap-токен неизвестен", status=401)


def token_used() -> ApiError:
    return ApiError("token_used", "bootstrap-токен уже обменян на identity", status=401)


def token_expired() -> ApiError:
    return ApiError(
        "token_expired",
        "срок bootstrap-токена истёк или он аннулирован (новым токеном или отзывом)",
        status=401,
    )


def _not_found() -> ApiError:
    return ApiError("not_found", "нода не найдена", status=404)


def _conflict(message: str) -> ApiError:
    return ApiError("conflict", message, status=409)


# Карточка ноды одним выражением: группы доступа — подзапросом, в порядке идентификаторов, чтобы
# ответ не зависел от порядка вставки.
_NODE_COLUMNS = (
    "n.id, n.code, n.name, n.country, n.city, n.provider, n.public_ipv4, n.public_ipv6, n.fqdn, "
    "n.billing_group_id, n.bandwidth_mbps, n.max_conn_per_ip, n.legal_profile, n.monthly_cost, "
    "n.currency, n.provider_account, n.cost_valid_from, n.cost_valid_to, "
    "n.traffic_included_bytes, n.traffic_overage_cost, n.status, n.status_changed_at, "
    "n.last_heartbeat_at, n.agent_version, n.xray_version, n.multiplier_milli, "
    "n.resync_required, n.created_at, n.decommissioned_at, "
    "array(select g.access_group_id from node_access_groups g where g.node_id = n.id "
    "order by g.access_group_id) as access_group_ids"
)
# Части запросов ниже — постоянные строки модуля, значения идут параметрами ($1): S608 ложен.
_NODE_BY_ID = f"select {_NODE_COLUMNS} from nodes n where n.id = $1"  # noqa: S608
_NODES = f"select {_NODE_COLUMNS} from nodes n order by n.code"  # noqa: S608
# Поиск ноды по отпечатку: строка identity и карточка одним запросом (раздел агента зовёт его на
# каждый запрос). Годность identity считается в базе — её часами, как и при выпуске.
_IDENTITY_USABLE = (
    "i.token_hash, (i.revoked_at is null and i.expires_at > now() "
    "and n.decommissioned_at is null) as usable"
)
_NODE_BY_FINGERPRINT = (
    f"select {_NODE_COLUMNS}, {_IDENTITY_USABLE} from node_identities i "  # noqa: S608
    "join nodes n on n.id = i.node_id where i.cert_fingerprint = $1"
)
_LAST_IDENTITY = (
    "select generation, cert_fingerprint, cert_serial, issued_at, expires_at, revoked_at, "
    "enrolled_from from node_identities where node_id = $1 order by generation desc limit 1"
)
# Последний токен для сверки на шаге 6 — последний выданный: наибольший ``issue_seq``
# (миграция 131). Номер даёт последовательность базы, а не часы, и выдачи одной ноды идут под
# блокировкой её строки — токен, выданный после миграции, всегда с большим номером (строки, что
# были в таблице при её применении, миграция нумерует порядком ``id``). uuidv7 в ``id`` растёт с
# часами базы: после их шага назад «последним» оказался бы прежний токен — и до обмена, и после
# него, когда живого токена нет (роаст 001.25, раунды 3–4 и 6).
_LAST_ISSUE = (
    "select expires_at, used_at, annulled_at from bootstrap_tokens where node_id = $1 "
    "order by issue_seq desc limit 1"
)
# Блокировка ноды перед записью её токенов и identity (порядок блокировок — докстринг модуля).
# Момент операции берётся следующим оператором — после того, как блокировка получена.
_LOCK_NODE = (
    "select decommissioned_at is not null as gone, status from nodes "
    "where id = $1 for no key update"
)
_MOMENT = "select statement_timestamp()"
# Аннулирование — отметка, а не укороченный срок: сравнение срока с часами базы держалось бы на
# том, что часы не идут назад (шаг NTP, возобновление VM), а отметку обмен читает без часов —
# как погашение (``used_at``) и отзыв identity (``revoked_at``). Роаст 001.25, раунд 2.
_ANNUL_TOKENS = (
    "update bootstrap_tokens set annulled_at = $2 "
    "where node_id = $1 and used_at is null and annulled_at is null"
)


def _node(row: asyncpg.Record) -> Node:
    fields = dict(row)
    fields["legal_profile"] = json.loads(fields["legal_profile"])
    fields["access_group_ids"] = list(fields["access_group_ids"])
    return Node.model_validate({name: fields[name] for name in Node.model_fields})


def _identity(row: asyncpg.Record) -> Identity:
    return Identity.model_validate(dict(row))


# --- карточка для заглушек других служб --------------------------------------------------------
# Заглушки учёта, состава и отчётов (001.28, 001.33) проверяют себя на фиксированной ноде, не
# заводя её в базе; служба нод этих значений больше не выдаёт.

STUB_NODE_ID = uuid.UUID("00000000-0000-7000-8000-0000000000b1")
STUB_BILLING_GROUP_ID = uuid.UUID("00000000-0000-7000-8000-0000000000f1")
STUB_ACCESS_GROUP_ID = uuid.UUID("00000000-0000-7000-8000-0000000000f2")  # ≠ тарифицируемой
STUB_CREATED_AT = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
STUB_NODE_IN = NodeIn(
    code="JP-Tokyo-01",
    name="Tokyo 1",
    country="JP",
    city="Tokyo",
    provider="Example Hosting",
    public_ipv4=ipaddress.IPv4Address("203.0.113.10"),
    billing_group_id=STUB_BILLING_GROUP_ID,
    access_group_ids=[STUB_ACCESS_GROUP_ID],
    bandwidth_mbps=1000,
    max_conn_per_ip=32,
)
STUB_AGENT_VERSION = "0.1.0"
STUB_XRAY_VERSION = "26.9.1"


def stub_node(
    spec: NodeIn = STUB_NODE_IN,
    status: NodeStatus = "active",
    node_id: uuid.UUID = STUB_NODE_ID,
) -> Node:
    """Фиксированная карточка для заглушек других служб: ``pending`` — нода до enrollment
    (версий и heartbeat нет), остальные статусы — нода после обмена токена."""
    enrolled = status != "pending"
    return Node(
        **spec.model_dump(),
        id=node_id,
        status=status,
        status_changed_at=STUB_CREATED_AT,
        last_heartbeat_at=STUB_CREATED_AT if status == "active" else None,
        agent_version=STUB_AGENT_VERSION if enrolled else None,
        xray_version=STUB_XRAY_VERSION if enrolled else None,
        resync_required=False,
        created_at=STUB_CREATED_AT,
        decommissioned_at=None,
    )


class NodeService:
    """Ноды поверх пула asyncpg. Ключ CA (``ca``) нужен только обмену токена — он подписывает лист.
    Выдаче токена нужен лишь сертификат CA (``anchor_pem``, якорь bootstrap-команды): панели он
    передаётся строкой, и её служба не может ничего подписать (роаст 001.25, раунд 3). Без
    ``anchor_pem`` берётся сертификат из ``ca``; без обоих выдача токена — ``RuntimeError``."""

    def __init__(
        self, pool: Any, ca: InternalCA | None = None, *, anchor_pem: str | None = None
    ) -> None:
        self._pool = pool
        self._ca = ca
        self._anchor_pem = anchor_pem if anchor_pem is not None else (ca.ca_pem if ca else None)

    async def list(self) -> list[Node]:
        async with self._pool.acquire() as conn:
            return [_node(row) for row in await conn.fetch(_NODES)]

    async def create(self, spec: NodeIn, created_by: uuid.UUID) -> Node:
        """Запись ноды (UC-01 шаг 1) в статусе ``pending``: строка ``nodes``, группы доступа,
        первый интервал тарифицируемой группы (R-18: учёт берёт коэффициент по интервалу) и
        первый интервал публичного адреса (§4.17) — от момента создания. Занятый код — 409,
        неизвестная тарифицируемая группа или группа доступа — 422. Inbound с ключами REALITY
        (шаг 2) — 001.26; ``created_by`` уйдёт в ``audit_log`` с 001.48."""
        async with transaction(self._pool) as conn:
            try:
                row = await conn.fetchrow(
                    "insert into nodes (code, name, country, city, provider, public_ipv4, "
                    "public_ipv6, fqdn, billing_group_id, bandwidth_mbps, max_conn_per_ip, "
                    "legal_profile, monthly_cost, currency, provider_account, cost_valid_from, "
                    "cost_valid_to, traffic_included_bytes, traffic_overage_cost) values ($1, "
                    "$2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13, $14, $15, $16, "
                    "$17, $18, $19) returning id, created_at",
                    spec.code,
                    spec.name,
                    spec.country,
                    spec.city,
                    spec.provider,
                    spec.public_ipv4,
                    spec.public_ipv6,
                    spec.fqdn,
                    spec.billing_group_id,
                    spec.bandwidth_mbps,
                    spec.max_conn_per_ip,
                    json.dumps(spec.legal_profile),
                    spec.monthly_cost,
                    spec.currency,
                    spec.provider_account,
                    spec.cost_valid_from,
                    spec.cost_valid_to,
                    spec.traffic_included_bytes,
                    spec.traffic_overage_cost,
                )
            except asyncpg.UniqueViolationError as exc:
                raise _conflict("нода с таким кодом уже есть") from exc
            except asyncpg.ForeignKeyViolationError as exc:
                raise ApiError(
                    "unknown_billing_group", "тарифицируемая группа не найдена", status=422
                ) from exc
            node_id, at = row["id"], row["created_at"]
            await conn.execute(
                "insert into node_billing_assignments (node_id, billing_group_id, valid_from) "
                "values ($1, $2, $3)",
                node_id,
                spec.billing_group_id,
                at,
            )
            await conn.execute(
                "insert into node_ip_history (node_id, public_ipv4, valid_from) "
                "values ($1, $2, $3)",
                node_id,
                spec.public_ipv4,
                at,
            )
            try:
                await conn.executemany(
                    "insert into node_access_groups (node_id, access_group_id) values ($1, $2)",
                    [(node_id, group) for group in dict.fromkeys(spec.access_group_ids)],
                )
            except asyncpg.ForeignKeyViolationError as exc:
                raise ApiError(
                    "unknown_access_group", "группа доступа не найдена", status=422
                ) from exc
            return _node(await conn.fetchrow(_NODE_BY_ID, node_id))

    async def get(self, node_id: uuid.UUID) -> Node:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_NODE_BY_ID, node_id)
        if row is None:
            raise _not_found()
        return _node(row)

    async def update(self, node_id: uuid.UUID, patch: NodePatch) -> Node:
        """Не реализовано — 501. Изменение записи ведёт за собой группы доступа, историю адреса
        (§4.17) и смену тарифицируемой группы с закрытием часа учёта (``set_billing_group``,
        001.19); владельца в плане нет (отчёт 001.25)."""
        raise not_implemented("admin.nodes.update")

    async def decommission(self, node_id: uuid.UUID, by: uuid.UUID) -> None:
        """Не реализовано — 501. Вывод из эксплуатации (§4.6 «Удаление», UC-12 A2) — это и
        команды агенту (буфер, счётчики, затирание секретов), а не одна отметка в базе;
        владельца в плане нет (отчёт 001.25). Отрезать ноду сейчас — ``revoke_identity``."""
        raise not_implemented("admin.nodes.decommission")

    async def issue_bootstrap_token(
        self, node_id: uuid.UUID, created_by: uuid.UUID
    ) -> BootstrapToken:
        """Новый одноразовый токен (Н-24, UC-01 шаги 2–3): в базе — хеш и срок
        ``BOOTSTRAP_TOKEN_TTL`` от момента выдачи; прежний неиспользованный токен ноды
        аннулируется тем же моментом (UC-01 A1, §4.5 «Повторная выдача»). Строка ноды
        блокируется: две одновременные выдачи иначе оставили бы в живых оба токена. Нет ноды —
        404, нода выведена из эксплуатации — 409."""
        if self._anchor_pem is None:
            raise RuntimeError("выдача токена без сертификата CA: NodeService без ca и anchor_pem")
        anchor_pem = self._anchor_pem
        token = generate_bootstrap_token()
        async with transaction(self._pool) as conn:
            node = await conn.fetchrow(_LOCK_NODE, node_id)
            if node is None:
                raise _not_found()
            if node["gone"]:
                raise _conflict("нода выведена из эксплуатации")
            at: dt.datetime = await conn.fetchval(_MOMENT)
            await conn.execute(_ANNUL_TOKENS, node_id, at)
            expires_at: dt.datetime = await conn.fetchval(
                "insert into bootstrap_tokens (node_id, token_hash, expires_at, created_by) "
                "values ($1, $2, $3, $4) returning expires_at",
                node_id,
                hash_token(token),
                at + BOOTSTRAP_TOKEN_TTL,
                created_by,
            )
        return BootstrapToken(
            node_id=node_id, token=token, expires_at=expires_at, ca_pem=anchor_pem
        )

    async def enroll(
        self,
        token: str,
        csr_pem: str,
        agent_version: str,
        xray_version: str,
        source: ipaddress.IPv4Address | ipaddress.IPv6Address,
    ) -> Enrollment:
        """Обмен токена на identity (UC-01 шаг 5). Токен неизвестен или нода выведена — 401
        ``token_invalid``, уже обменян — ``token_used``, истёк или аннулирован —
        ``token_expired`` (A1). Затем, в той же транзакции: лист ноды по CSR (``ValueError`` CA
        откатывает всё, токен остаётся годным), погашение токена, отзыв прежних identity ноды
        (§4.5 «Пересоздание»), новая identity со следующим поколением и адресом ``source``,
        нода — в ``pending`` с версиями, которые предъявил агент (шаг 6 сверяет их)."""
        if self._ca is None:
            raise RuntimeError("обмен токена без CA: NodeService создан без ca")
        token_hash = hash_token(token)
        identity_token = generate_identity_token()
        async with transaction(self._pool) as conn:
            # Нода токена неизменна, поэтому её можно узнать без блокировки; погашение и срок
            # читаются уже под блокировкой ноды — после неё запрос видит то, что успел
            # зафиксировать встречный обмен или выдача.
            node_id = await conn.fetchval(
                "select node_id from bootstrap_tokens where token_hash = $1", token_hash
            )
            if node_id is None:
                raise token_invalid()
            node = await conn.fetchrow(_LOCK_NODE, node_id)
            row = await conn.fetchrow(
                "select id, used_at is not null as used, annulled_at is not null as annulled, "
                "expires_at <= statement_timestamp() as expired, statement_timestamp() as at "
                "from bootstrap_tokens where token_hash = $1",
                token_hash,
            )
            if node is None or row is None or node["gone"]:
                raise token_invalid()
            if row["used"]:
                raise token_used()
            if row["annulled"] or row["expired"]:
                raise token_expired()
            at = row["at"]
            issued = self._ca.sign_csr(csr_pem, node_id=node_id, now=at)
            # Одноразовость держит и сама запись, а не только порядок блокировок: погашается лишь
            # непогашенный токен, и путь, забывший блокировку ноды, получит отказ, а не вторую
            # identity (так же, как UNIQUE (node_id, generation) миграции 130).
            consumed = await conn.execute(
                "update bootstrap_tokens set used_at = $2 "
                "where id = $1 and used_at is null and annulled_at is null",
                row["id"],
                at,
            )
            if consumed != "UPDATE 1":
                # Сюда приходит только путь, обошедший блокировку ноды: отказ — по тому, что
                # сделал встречный писатель, а не 500 от CHECK миграции 131.
                raced = await conn.fetchrow(
                    "select annulled_at is not null as annulled from bootstrap_tokens "
                    "where id = $1",
                    row["id"],
                )
                raise token_expired() if raced is not None and raced["annulled"] else token_used()
            await conn.execute(
                "update node_identities set revoked_at = $2 "
                "where node_id = $1 and revoked_at is null",
                node_id,
                at,
            )
            identity = await conn.fetchrow(
                "insert into node_identities (node_id, cert_fingerprint, cert_serial, token_hash, "
                "generation, issued_at, expires_at, enrolled_from) select $1, $2, $3, $4, "
                "coalesce(max(generation), 0) + 1, $5, $6, $7 from node_identities "
                "where node_id = $1 returning generation, cert_fingerprint, cert_serial, "
                "issued_at, expires_at, revoked_at, enrolled_from",
                node_id,
                issued.fingerprint,
                issued.serial,
                hash_token(identity_token),
                issued.not_before,
                issued.not_after,
                source,
            )
            # Статус события identity — строка §4.6 (statuses.py); statuses импортирует NodeStatus
            # отсюда, поэтому импорт на месте, а не в заголовке модуля.
            from app.domain.statuses import ON_REENROLLMENT

            await conn.execute(
                "update nodes set status = $5, status_changed_at = $2, "
                "agent_version = $3, xray_version = $4 where id = $1",
                node_id,
                at,
                agent_version,
                xray_version,
                ON_REENROLLMENT,
            )
        return Enrollment(
            **_identity(identity).model_dump(),
            node_id=node_id,
            client_cert_pem=issued.pem,
            ca_pem=self._ca.ca_pem,
            identity_token=identity_token,
        )

    async def approve(self, node_id: uuid.UUID, admin_id: uuid.UUID, cert_fingerprint: str) -> Node:
        """Подтверждение (UC-01 шаг 7): ``pending`` → ``provisioning`` — ровно той identity,
        которую администратор сверил по ``state`` на шаге 6 (адрес источника, отпечаток,
        версии): её отпечаток приходит в теле. Между сверкой и подтверждением мог пройти новый
        обмен (второй токен другого администратора) — тогда действующая identity уже другая, и
        подтверждать её, не видев, нельзя: 409 ``identity_changed``. Без действующей identity —
        409, выведенная нода — 409, из любого статуса, кроме ``pending``, — 409.
        ``admin_id`` уйдёт в ``audit_log`` с 001.48."""
        async with transaction(self._pool) as conn:
            node = await conn.fetchrow(_LOCK_NODE, node_id)
            if node is None:
                raise _not_found()
            if node["gone"]:
                raise _conflict("нода выведена из эксплуатации")
            if node["status"] != "pending":
                raise _conflict(f"подтверждается только нода в pending, а не в {node['status']}")
            at: dt.datetime = await conn.fetchval(_MOMENT)
            live = await conn.fetch(
                "select cert_fingerprint from node_identities where node_id = $1 "
                "and revoked_at is null and expires_at > $2",
                node_id,
                at,
            )
            if not live:
                raise _conflict(
                    "у ноды нет действующей identity: токен не обменян, "
                    "identity истекла или отозвана"
                )
            if [row["cert_fingerprint"] for row in live] != [cert_fingerprint]:
                raise ApiError(
                    "identity_changed",
                    "действующая identity ноды — не та, что сверена: сверьте состояние заново",
                    status=409,
                )
            await conn.execute(
                "update nodes set status = 'provisioning', status_changed_at = $2 where id = $1",
                node_id,
                at,
            )
            return _node(await conn.fetchrow(_NODE_BY_ID, node_id))

    async def set_manual_status(
        self,
        node_id: uuid.UUID,
        status: ManualStatus | None,
        admin_id: uuid.UUID,
        reason: str | None = None,
    ) -> Node:
        """Не реализовано — 501: ручной статус §4.6 записывает матрица переходов с
        инициатором (``StatusService``, 001.30). Тело проверяется моделью до вызова — 422 на
        неручной статус остаётся контрактом."""
        raise not_implemented("admin.nodes.status")

    async def revoke_identity(self, node_id: uuid.UUID, admin_id: uuid.UUID) -> NodeState:
        """Отзыв identity (Н-31; UC-12 шаг 1, UC-01 A2) — синхронно, в одной транзакции: все
        неотозванные identity ноды получают ``revoked_at``, неиспользованный bootstrap-токен
        аннулируется (иначе утёкший с VPS токен выпустил бы identity после отзыва), нода — в
        ``disabled``. Раздел ``/agent/v1`` сверяет identity с базой на каждом запросе, поэтому
        следующий же запрос ноды — 401. Повторный отзыв ничего не меняет и не сдвигает
        ``status_changed_at``. Запись ноды остаётся: новый токен выдаётся отдельно."""
        async with transaction(self._pool) as conn:
            node = await conn.fetchrow(_LOCK_NODE, node_id)
            if node is None:
                raise _not_found()
            at: dt.datetime = await conn.fetchval(_MOMENT)
            await conn.execute(
                "update node_identities set revoked_at = $2 "
                "where node_id = $1 and revoked_at is null",
                node_id,
                at,
            )
            await conn.execute(_ANNUL_TOKENS, node_id, at)
            # Статус события identity — строка §4.6 (statuses.py; импорт на месте — см. enroll).
            from app.domain.statuses import ON_REVOCATION

            await conn.execute(
                "update nodes set status = $3, status_changed_at = $2 "
                "where id = $1 and status <> $3",
                node_id,
                at,
                ON_REVOCATION,
            )
            return await self._state(conn, node_id)

    async def state(self, node_id: uuid.UUID) -> NodeState:
        """Состояние одним снимком (``repeatable read``): статус, identity и токен читаются
        тремя операторами, и обмен, зафиксированный между ними, иначе показал бы сочетание,
        которого в базе не было."""
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            return await self._state(conn, node_id)

    async def by_identity(self, fingerprint: str, identity_token: str) -> Node | None:
        """Нода, которой принадлежит сертификат с отпечатком ``fingerprint``, если identity не
        отозвана и не истекла, нода не выведена из эксплуатации и токен identity совпадает
        (``secrets.compare_digest`` по хешам). Иначе ``None`` — раздел отвечает одним 401 на все
        случаи (§5.2)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_NODE_BY_FINGERPRINT, fingerprint)
        if row is None:
            return None
        token_matches = secrets.compare_digest(hash_token(identity_token), row["token_hash"])
        if not (token_matches and row["usable"]):
            return None
        return _node(row)

    async def _state(self, conn: asyncpg.Connection, node_id: uuid.UUID) -> NodeState:
        node = await conn.fetchrow(
            "select id, status, status_changed_at, desired_config_version, "
            "applied_config_version, desired_users_seq, applied_users_seq, applied_at, "
            "resync_required, last_heartbeat_at, missed_heartbeats, ok_heartbeats, "
            "agent_version, xray_version from nodes where id = $1",
            node_id,
        )
        if node is None:
            raise _not_found()
        identity = await conn.fetchrow(_LAST_IDENTITY, node_id)
        token = await conn.fetchrow(_LAST_ISSUE, node_id)
        return NodeState(
            node_id=node["id"],
            status=node["status"],
            status_changed_at=node["status_changed_at"],
            cursors=Cursors(
                desired_config_version=node["desired_config_version"],
                applied_config_version=node["applied_config_version"],
                desired_users_seq=node["desired_users_seq"],
                applied_users_seq=node["applied_users_seq"],
                applied_at=node["applied_at"],
            ),
            resync_required=node["resync_required"],
            heartbeat=Heartbeat(
                last_at=node["last_heartbeat_at"],
                missed=node["missed_heartbeats"],
                ok=node["ok_heartbeats"],
            ),
            agent_version=node["agent_version"],
            xray_version=node["xray_version"],
            identity=_identity(identity) if identity is not None else None,
            bootstrap_token_expires_at=token["expires_at"] if token is not None else None,
            bootstrap_token_used_at=token["used_at"] if token is not None else None,
            bootstrap_token_annulled_at=token["annulled_at"] if token is not None else None,
        )
