"""Парк нод и enrollment (постановка §3.2, §4.6; data-model.md §4.2.3 ``nodes``,
``bootstrap_tokens``, ``node_identities``; UC-01, UC-12; R-02).

Задача 001.24 — схемы и заглушка ``NodeService`` с фиксированными значениями: карточка ноды,
bootstrap-токен, обмен токена на identity, подтверждение, ручные статусы, отзыв identity, вывод
из эксплуатации. Логика (база, хеши токенов Н-24, подпись CSR ключом CA, проверка признаков
UC-01 шаг 6, отзыв Н-31 и аудит) — 001.25; статусы по heartbeat — 001.30.

Что уже настоящее в заглушке: bootstrap-токен — 256 бит из ``secrets`` (§7.2: показывается
однократно, в базе будет только хеш), его срок — ``BOOTSTRAP_TOKEN_TTL`` (Н-24), отпечаток
identity — SHA-256 выданного сертификата (``InternalCA.fingerprint``).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import secrets
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

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

Code = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{1,63}$")]
Country = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Name = Annotated[str, StringConstraints(min_length=1, max_length=100)]
Fqdn = Annotated[str, StringConstraints(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.-]+$")]
Version = Annotated[str, StringConstraints(min_length=1, max_length=64)]


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
    выдача аннулирует прежний. Команда bootstrap с адресом enrollment — 001.61."""

    node_id: uuid.UUID
    token: str = Field(description="256 бит, base64url; в базе хранится только хеш")
    expires_at: dt.datetime


class ManualStatusIn(BaseModel):
    """Ручной статус §4.6; ``null`` — снять ручной статус (далее автоматика: ``active`` или
    ``offline`` по heartbeat)."""

    status: ManualStatus | None = Field(description="maintenance | disabled | suspended | null")
    reason: Annotated[str, StringConstraints(max_length=500)] | None = None


class Identity(BaseModel):
    """Identity ноды (§5.3, ``node_identities``): поколение, отпечаток, сроки, отзыв."""

    generation: int = Field(ge=1)
    cert_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$", description="SHA-256 DER, hex")
    issued_at: dt.datetime
    expires_at: dt.datetime
    revoked_at: dt.datetime | None


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
    heartbeat, версии, действующая identity и последний bootstrap-токен."""

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


def generate_bootstrap_token() -> str:
    """256 случайных бит из ОС в base64url (43 символа). Источник — ``secrets`` (``os.urandom``),
    не ``random``: токен даёт identity ноды (§7.2, Н-24)."""
    return secrets.token_urlsafe(BOOTSTRAP_TOKEN_BYTES)


# --- фиксированные значения заглушки ------------------------------------------------------------

STUB_NODE_ID = uuid.UUID("00000000-0000-7000-8000-0000000000b1")
STUB_BILLING_GROUP_ID = uuid.UUID("00000000-0000-7000-8000-0000000000f1")
STUB_ACCESS_GROUP_ID = uuid.UUID("00000000-0000-7000-8000-0000000000f2")  # ≠ тарифицируемой
STUB_CREATED_AT = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
STUB_IDENTITY_TOKEN = "stub-identity-token-000000000000000000000000000"  # noqa: S105 — заглушка
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
STUB_CSR_PEM = (
    "-----BEGIN CERTIFICATE REQUEST-----\n"
    "Y29udHJvbC1wbGFuZSBzdHViIENTUiAwMDEuMjQ=\n"
    "-----END CERTIFICATE REQUEST-----\n"
)
STUB_AGENT_VERSION = "0.1.0"
STUB_XRAY_VERSION = "26.9.1"


def stub_node(
    spec: NodeIn = STUB_NODE_IN,
    status: NodeStatus = "active",
    node_id: uuid.UUID = STUB_NODE_ID,
) -> Node:
    """Карточка заглушки: ``pending`` — свежая запись до enrollment (версий и heartbeat нет),
    остальные статусы — нода после обмена токена. ``node_id`` — идентификатор из пути: заглушка
    состояния не хранит, но и не выдаёт карточку чужой ноды."""
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


def stub_identity(revoked: bool = False) -> Identity:
    return Identity(
        generation=1,
        cert_fingerprint=InternalCA.fingerprint(InternalCA().sign_csr(STUB_CSR_PEM)),
        issued_at=STUB_CREATED_AT,
        expires_at=STUB_CREATED_AT + dt.timedelta(days=90),
        revoked_at=STUB_CREATED_AT + dt.timedelta(days=1) if revoked else None,
    )


def stub_state(
    node_id: uuid.UUID = STUB_NODE_ID,
    status: NodeStatus = "active",
    identity: Identity | None = None,
) -> NodeState:
    """Состояние той же ноды, что отдаёт ``stub_node``: статус по умолчанию совпадает с
    карточкой — заглушка не моделирует переходы, их показывают ``create`` и ``approve``."""
    return NodeState(
        node_id=node_id,
        status=status,
        status_changed_at=STUB_CREATED_AT,
        cursors=Cursors(
            desired_config_version=0,
            applied_config_version=0,
            desired_users_seq=0,
            applied_users_seq=0,
            applied_at=None,
        ),
        resync_required=False,
        heartbeat=Heartbeat(last_at=None, missed=0, ok=0),
        agent_version=STUB_AGENT_VERSION,
        xray_version=STUB_XRAY_VERSION,
        identity=identity,
        bootstrap_token_expires_at=STUB_CREATED_AT + BOOTSTRAP_TOKEN_TTL,
        bootstrap_token_used_at=STUB_CREATED_AT + dt.timedelta(minutes=5),
    )


class NodeService:
    """Ноды поверх пула asyncpg и CA. Заглушка 001.24: база не читается и не пишется."""

    def __init__(self, pool: Any, ca: InternalCA | None = None) -> None:
        self._pool = pool
        self._ca = ca or InternalCA()

    async def list(self) -> list[Node]:
        return [stub_node()]

    async def create(self, spec: NodeIn, created_by: uuid.UUID) -> Node:
        """Создать запись (UC-01 шаг 1); статус ``pending`` до подтверждения. Inbound с ключами
        REALITY (шаг 2) — 001.26."""
        return stub_node(spec, status="pending")

    async def get(self, node_id: uuid.UUID) -> Node:
        return stub_node(node_id=node_id)

    async def update(self, node_id: uuid.UUID, patch: NodePatch) -> Node:
        merged = STUB_NODE_IN.model_copy(update=patch.model_dump(exclude_unset=True))
        return stub_node(merged, node_id=node_id)

    async def decommission(self, node_id: uuid.UUID, by: uuid.UUID) -> None:
        """Вывод из эксплуатации (§4.6 «Удаление», UC-12 A2): ``decommissioned_at``."""

    async def issue_bootstrap_token(
        self, node_id: uuid.UUID, created_by: uuid.UUID, now: dt.datetime | None = None
    ) -> BootstrapToken:
        """Новый одноразовый токен (Н-24); прежний аннулируется (UC-01 A1). Заглушка выдаёт
        настоящий случайный токен и срок ``BOOTSTRAP_TOKEN_TTL``, но не сохраняет их."""
        issued = now or dt.datetime.now(dt.UTC)
        return BootstrapToken(
            node_id=node_id,
            token=generate_bootstrap_token(),
            expires_at=issued + BOOTSTRAP_TOKEN_TTL,
        )

    async def enroll(
        self, token: str, csr_pem: str, agent_version: str, xray_version: str
    ) -> Enrollment:
        """Обмен токена на identity (UC-01 шаг 5): сертификат по CSR, токен identity, нода в
        ``pending``. Проверки токена (хеш, срок, одноразовость, привязка к ноде — UC-01 A1) и
        запись ``node_identities`` — 001.25; здесь любой токен принимается."""
        cert = self._ca.sign_csr(csr_pem)
        identity = stub_identity()
        return Enrollment(
            **identity.model_dump(),
            node_id=STUB_NODE_ID,
            client_cert_pem=cert,
            ca_pem=self._ca.ca_pem,
            identity_token=STUB_IDENTITY_TOKEN,
        )

    async def approve(self, node_id: uuid.UUID, admin_id: uuid.UUID) -> Node:
        """Подтверждение (UC-01 шаг 7): ``pending`` → ``provisioning``."""
        return stub_node(status="provisioning", node_id=node_id)

    async def set_manual_status(
        self,
        node_id: uuid.UUID,
        status: ManualStatus | None,
        admin_id: uuid.UUID,
        reason: str | None = None,
    ) -> Node:
        """Ручной статус §4.6 или его снятие (``None``): далее автоматика; заглушка — ``active``.
        ``reason`` — причина перевода: её пишет в ``audit_log`` задача 001.48, здесь она принята
        доменом, чтобы маршрут не выбрасывал объявленное контрактом поле."""
        return stub_node(status=status or "active", node_id=node_id)

    async def revoke_identity(self, node_id: uuid.UUID, admin_id: uuid.UUID) -> NodeState:
        """Отзыв identity (Н-31; UC-12 шаг 1, UC-01 A2): запросы с ней отклоняются, нода
        исключается из выдачи; запись остаётся, новый токен выдаётся отдельно."""
        return stub_state(node_id=node_id, identity=stub_identity(revoked=True))

    async def state(self, node_id: uuid.UUID) -> NodeState:
        return stub_state(node_id=node_id, identity=stub_identity())
