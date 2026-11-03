"""Inbound ноды и ключи REALITY (постановка §4.4 «Сущность Inbound», «Профили MVP», «Ротация
ключевого материала REALITY»; data-model.md §4.2.3 ``inbounds``, ``inbound_secrets``; R-03;
UC-01, UC-03, UC-12).

Задача 001.26: модель ``Inbound``, ``InboundService`` с заглушками и настоящая ``gen_x25519``.
Запись строк ``inbounds`` и ``inbound_secrets`` при создании ноды, ротации ``short_ids`` с окном
перекрытия Н-33, замена ключевой пары при компрометации с событием пользователям ноды (В-41),
смена цели с проверкой её свойств и включение inbound — 001.27.

Что в заглушке настоящее: генерация ключевой пары x25519 (библиотека ``cryptography``) и запись
ключа в форме, которую принимает Xray, — base64url без дополнения (``xray_key``). Фиксированные
inbound заглушки согласованы с фиксированной конфигурацией ``XrayConfigBuilder``: порт, тег,
``short_ids``, цель и открытый ключ каждого из них соответствуют серверной части документа.

Закрытого ключа в модели нет: он живёт в ``inbound_secrets`` зашифрованным (§7.2), и панель,
читающая ``Inbound``, его не получает ни в каком виде.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import uuid
from typing import Annotated, Any, Literal

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.domain.plans import InboundProfile
from app.errors import not_implemented

# Набор портов профиля ``vless-xhttp`` (§4.4 «Профили MVP»): порт inbound выбирается из него.
XHTTP_PORTS = (2053, 2083, 2087, 2096, 8443)
# ``short_ids`` REALITY: hex, чётное число символов, не больше 16 (§4.4); пустое значение Xray
# допускает — клиент без ``sid``.
ShortId = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{2}){0,8}$")]
# Имя SNI — доменное имя: не длиннее 253 символов (RFC 1035 §2.3.4 в текстовой записи). Цель —
# «имя:порт», то есть не длиннее 253 + 1 + 5 = 259.
ServerName = Annotated[str, StringConstraints(min_length=1, max_length=253)]
Target = Annotated[str, StringConstraints(min_length=1, max_length=259)]
# ``sockopt.trustedXForwardedFor`` Xray v26.10.21 — имена заголовков, а не адреса
# (``SocketConfig.TrustedXForwardedFor []string``): ``X-Forwarded-For`` принимается, только если в
# запросе есть один из этих заголовков; при пустом списке ядро его игнорирует и берёт настоящий
# адрес соединения. Имя заголовка — token HTTP (RFC 9110 §5.1).
HeaderName = Annotated[str, StringConstraints(pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")]
# Основания смены ключевой пары по таблице §4.4 «Ротация ключевого материала REALITY»: она одна —
# компрометация или изъятие ноды. Плановая ротация меняет только ``short_ids``, недоступность цели
# — цель и ``server_names``, пересоздание по иным причинам ключевую пару сохраняет.
KeypairRotationReason = Literal["compromise"]


def gen_x25519() -> tuple[bytes, bytes]:
    """Ключевая пара REALITY: закрытый и открытый ключи x25519 по 32 байта (§4.4 — пару
    генерирует Control Plane, а не нода: пересоздание ноды не должно менять ``pbk`` клиентов).
    Открытый ключ выводится из закрытого; источник случайности — ОС (через ``cryptography``)."""
    private = X25519PrivateKey.generate()
    return private.private_bytes_raw(), private.public_key().public_bytes_raw()


def xray_key(raw: bytes) -> str:
    """Ключ x25519 в записи Xray: base64url без дополнения (``realitySettings.privateKey``,
    ``pbk`` клиентской ссылки). Ключ другой длины — ошибка вызывающего кода, а не данных."""
    if len(raw) != 32:
        raise ValueError("ключ x25519 — ровно 32 байта")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class LimitFallback(BaseModel):
    """Предел fallback зондирующих соединений на направление (``limitFallbackUpload`` и
    ``limitFallbackDownload`` Xray): после ``after_bytes`` скорость ограничивается
    ``bytes_per_sec`` с всплеском ``burst_bytes_per_sec``. Целые до 2⁵³ — граница канонической
    записи документа (``contracts/agent_v1/README.md``, «Контрольная сумма конфигурации»)."""

    model_config = ConfigDict(extra="forbid")

    after_bytes: int = Field(ge=0, le=2**53)
    bytes_per_sec: int = Field(ge=0, le=2**53)
    burst_bytes_per_sec: int = Field(ge=0, le=2**53)


class InboundParams(BaseModel):
    """Колонка ``inbounds.params`` (jsonb): поля профиля §4.4 с перечислениями источника. Набор
    закрыт (``extra="forbid"``): ``params`` уходит в ответ панели целиком, и лишний ключ — тот же
    закрытый ключ под любым именем — был бы выдан администратору. Поле, которого у профиля нет,
    — ``None``: ``flow`` у ``vless-xhttp`` (§4.4: none — в документе поля нет) и у Trojan,
    поля XHTTP у остальных профилей."""

    model_config = ConfigDict(extra="forbid")

    flow: Literal["xtls-rprx-vision"] | None = None
    decryption: Literal["none"] | None = None
    xhttp_path: str | None = None
    xhttp_mode: Literal["auto", "packet-up", "stream-up", "stream-one"] | None = None
    xhttp_host: str | None = None
    x_padding_bytes: Annotated[str, StringConstraints(pattern=r"^\d+(-\d+)?$")] | None = None
    x_padding_obfs_mode: bool | None = None
    x_padding_key: str | None = None
    x_padding_header: str | None = None
    x_padding_placement: Literal["cookie", "header", "query", "queryInHeader"] | None = None
    x_padding_method: Literal["repeat-x", "tokenish"] | None = None
    # GET допустим только в режиме packet-up (§4.4); перекрёстную проверку вводит сборка 001.27.
    uplink_http_method: Literal["POST", "GET"] | None = None
    session_id_placement: Literal["path", "cookie", "header", "query"] | None = None
    session_id_key: str | None = None
    limit_fallback_upload: LimitFallback | None = None
    limit_fallback_download: LimitFallback | None = None


class Inbound(BaseModel):
    """Строка ``inbounds`` (data-model.md §4.2.3): параметры подключения пары «нода × профиль».
    Поля профиля §4.4 (``flow``, ``decryption``, ``xhttp_*``, ``x_padding_*``,
    ``uplink_http_method``, ``session_id_*``, ``limit_fallback_*``) — в ``params``, как в колонке
    jsonb; их сборку в конфигурацию Xray ведёт ``XrayConfigBuilder``."""

    id: uuid.UUID
    node_id: uuid.UUID
    profile: InboundProfile
    port: int = Field(ge=1, le=65535)
    tag: str = Field(description="тег inbound в конфигурации Xray")
    enabled: bool
    reality_public_key: str = Field(description="выдаётся клиенту как pbk")
    reality_short_ids: list[ShortId] = Field(description="выдаются клиенту как sid")
    reality_target: str = Field(description="сайт-цель REALITY, host:port")
    reality_server_names: list[ServerName]
    reality_min_client_ver: str
    reality_max_client_ver: str | None = None
    reality_xver: Literal[0] = Field(
        default=0, description="0 на всех нодах: сайт-цель PROXY protocol не принимает (В-17)"
    )
    client_fingerprint: str = Field(description="uTLS-профиль клиента, fp")
    client_spider_x: str = Field(description="spx клиентской ссылки, своё значение у профиля")
    trusted_x_forwarded_for: list[HeaderName] = Field(
        default_factory=list,
        description="sockopt.trustedXForwardedFor: имена заголовков CDN, при которых ядро "
        "доверяет X-Forwarded-For (Д-14); пусто — не доверяет",
    )
    params: InboundParams = Field(default_factory=InboundParams, description="поля профиля §4.4")
    error_state: str | None = Field(default=None, description="недоступность цели (Н-30)")
    error_since: dt.datetime | None = None


class InboundPatch(BaseModel):
    """Тело ``PATCH /admin/inbounds/{id}``: цель вместе с ``server_names`` (смена цели при её
    недоступности, ключевая пара сохраняется — §4.4) и включение inbound. Цель и имена
    меняются только вместе: ``server_names`` — допустимые SNI именно этой цели."""

    model_config = ConfigDict(extra="forbid")

    target: Target | None = None
    server_names: list[ServerName] | None = Field(default=None, min_length=1)
    enabled: bool | None = None

    @model_validator(mode="after")
    def _target_with_its_names(self) -> InboundPatch:
        if (self.target is None) is not (self.server_names is None):
            raise ValueError("цель и server_names меняются вместе")
        if self.target is None and self.enabled is None:
            raise ValueError("нечего менять")
        return self


# --- фиксированные значения заглушки ------------------------------------------------------------

STUB_INBOUND_IDS: dict[InboundProfile, uuid.UUID] = {
    "vless_raw_vision": uuid.UUID("00000000-0000-7000-8000-0000000000e1"),
    "vless_xhttp": uuid.UUID("00000000-0000-7000-8000-0000000000e2"),
    "trojan_reality": uuid.UUID("00000000-0000-7000-8000-0000000000e3"),
}
# Порты — из примера ноды постановки (§3.4 «Desired State модель»): основной 443, XHTTP из
# набора, Trojan — отдельный. Порт XHTTP в бою выбирает 001.27.
STUB_PORTS: dict[InboundProfile, int] = {
    "vless_raw_vision": 443,
    "vless_xhttp": 2087,
    "trojan_reality": 8443,
}
STUB_SHORT_IDS: dict[InboundProfile, str] = {
    "vless_raw_vision": "6ba85179e30d4fc2",
    "vless_xhttp": "a1c3e5f70b2d4f68",
    "trojan_reality": "3e9b21d07c5a84f1",
}
STUB_TARGET = "www.example.com:443"
STUB_SERVER_NAMES = ["www.example.com"]
STUB_MIN_CLIENT_VER = "1.8.1"  # версия, которую передаёт sing-box (WI-36, §4.4)
STUB_FINGERPRINT = "chrome"  # набор допустимых отпечатков на v26.10.21 (WI-36)
STUB_XHTTP_PATH = "/9f3c1a7e"
# Пределы fallback для зондирующих соединений: в бою рандомизируются на ноду (001.27), чтобы парк
# не опознавался по одинаковым значениям; здесь — фиксированные.
STUB_LIMIT_FALLBACK_UPLOAD = {
    "after_bytes": 1048576,
    "bytes_per_sec": 65536,
    "burst_bytes_per_sec": 131072,
}
STUB_LIMIT_FALLBACK_DOWNLOAD = {
    "after_bytes": 2097152,
    "bytes_per_sec": 131072,
    "burst_bytes_per_sec": 262144,
}


def stub_private_key(profile: InboundProfile) -> bytes:
    """Закрытый ключ заглушки: выведен из кода профиля и потому публичен — секретом он не
    является и в бою не встречается. Настоящий ключ — только ``gen_x25519`` (001.27)."""
    return hashlib.sha256(f"control-plane stub reality key:{profile}".encode()).digest()


def stub_public_key(profile: InboundProfile) -> str:
    """Открытый ключ заглушки в записи Xray — пара к ``stub_private_key``."""
    private = X25519PrivateKey.from_private_bytes(stub_private_key(profile))
    return xray_key(private.public_key().public_bytes_raw())


def stub_params(profile: InboundProfile) -> dict[str, Any]:
    """Поля профиля §4.4 в форме колонки ``params``. ``flow`` профиля ``vless-xhttp`` по §4.4 —
    none, то есть поля нет: Xray v26.10.21 принимает в ``flow`` только ``xtls-rprx-vision`` или
    пустое значение. У Trojan ``flow`` нет вовсе."""
    fallback = {
        "limit_fallback_upload": dict(STUB_LIMIT_FALLBACK_UPLOAD),
        "limit_fallback_download": dict(STUB_LIMIT_FALLBACK_DOWNLOAD),
    }
    if profile == "vless_raw_vision":
        return {"flow": "xtls-rprx-vision", "decryption": "none", **fallback}
    if profile == "vless_xhttp":
        return {
            "decryption": "none",
            "xhttp_path": STUB_XHTTP_PATH,
            "xhttp_mode": "auto",
            "x_padding_bytes": "100-1000",
            "x_padding_obfs_mode": False,
            "x_padding_key": "x_padding",
            "x_padding_header": "X-Padding",
            "x_padding_placement": "queryInHeader",
            "x_padding_method": "repeat-x",
            "uplink_http_method": "POST",
            "session_id_placement": "path",
            "session_id_key": "",
            **fallback,
        }
    return fallback


def stub_inbounds(node_id: uuid.UUID) -> list[Inbound]:
    """Три inbound профилей MVP (§4.4) на разных портах: основной ``vless-raw-vision`` на 443,
    ``vless-xhttp`` — порт из набора XHTTP, ``trojan-reality`` — отдельный порт."""
    profiles: tuple[InboundProfile, ...] = ("vless_raw_vision", "vless_xhttp", "trojan_reality")
    return [
        Inbound(
            id=STUB_INBOUND_IDS[profile],
            node_id=node_id,
            profile=profile,
            port=STUB_PORTS[profile],
            tag=profile,
            enabled=True,
            reality_public_key=stub_public_key(profile),
            reality_short_ids=[STUB_SHORT_IDS[profile]],
            reality_target=STUB_TARGET,
            reality_server_names=list(STUB_SERVER_NAMES),
            reality_min_client_ver=STUB_MIN_CLIENT_VER,
            client_fingerprint=STUB_FINGERPRINT,
            client_spider_x=f"/{profile.split('_')[-1]}",
            params=InboundParams.model_validate(stub_params(profile)),
        )
        for profile in profiles
    ]


class InboundService:
    """Inbound нод поверх пула asyncpg. Заглушка 001.26: база не читается и не пишется."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def create_defaults(self, conn: Any, node_id: uuid.UUID) -> list[Inbound]:
        """Три inbound профилей §4.4 для новой ноды — в транзакции её создания (``conn``
        вызывающего). Заглушка возвращает фиксированные inbound и подключение не трогает;
        запись ``inbounds`` и ``inbound_secrets`` с зашифрованным ключом — 001.27."""
        return stub_inbounds(node_id)

    async def rotate_short_ids(self, inbound_id: uuid.UUID) -> Inbound:
        """Не реализовано — 501. Плановая ротация ``short_ids`` с окном перекрытия Н-33: новое
        значение добавляется, прежнее удаляется задачей через 24 часа (001.27)."""
        raise not_implemented("admin.inbounds.rotate_short_ids")

    async def rotate_keypair(self, inbound_id: uuid.UUID, reason: KeypairRotationReason) -> Inbound:
        """Не реализовано — 501. Новая ключевая пара и новые ``short_ids`` без окна перекрытия;
        прежний ключ повторно не используется; пользователям ноды — событие «Адрес или параметры
        ноды изменены» (В-41; 001.27)."""
        raise not_implemented("admin.inbounds.rotate_keypair")

    async def set_enabled(self, inbound_id: uuid.UUID, enabled: bool) -> Inbound:
        """Не реализовано — 501. Включение и отключение inbound — новая версия конфигурации
        (001.27)."""
        raise not_implemented("admin.inbounds.set_enabled")

    async def set_target(
        self, inbound_id: uuid.UUID, target: str, server_names: list[str]
    ) -> Inbound:
        """Не реализовано — 501. Смена цели после проверки её свойств с Control Plane (TLS 1.3,
        HTTP/2, без перенаправления); при отказе цель не меняется, ключевая пара сохраняется
        (§4.4; 001.27)."""
        raise not_implemented("admin.inbounds.set_target")
