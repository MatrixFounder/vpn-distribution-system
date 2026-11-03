"""Генератор конфигурации Xray для ноды (постановка §4.4; interfaces.md §5.2 «Поток
конфигурации»; data-model.md §4.2.3 ``node_config_versions``; R-03; UC-01).

Задача 001.26: ``XrayConfigBuilder.build`` возвращает фиксированный документ — форму серверной
конфигурации трёх профилей MVP. Сборка по строкам ``inbounds`` с рандомизацией
``limitFallback*`` на ноду — 001.27; запись версии в ``node_config_versions`` и выдача потоком
конфигурации — 001.29.

Имена полей — Xray-core v26.10.21 (§17.3, WI-36): состав inbound — ``settings.users`` (прежнее
``clients`` ядро принимает как псевдоним, но при обоих ключах берёт ``clients``), транспорт —
``streamSettings.method`` (прежнее ``network`` — псевдоним), место идентификатора сессии XHTTP —
``sessionIDPlacement`` и ``sessionIDKey``: прежние ``sessionPlacement`` и ``sessionKey`` ядро
пропускает без ошибки и молча применяет значение по умолчанию.

Что документ несёт и чего не несёт:

- credentials пользователей — нет: ``users`` каждого inbound пуст, состав нода получает потоком
  состава и добавляет пользователей через ``HandlerService`` (§5.2);
- ``spiderX`` и ``fingerprint`` — нет: это параметры клиента, они уходят в клиентские ссылки
  (001.43), а серверу не нужны;
- закрытый ключ REALITY — да: без ``realitySettings.privateKey`` сервер REALITY не стартует. Это
  секрет §7.2, и в бою документ с ним не хранится открытым (заметка для 001.27 и 001.29 — в их
  файлах задач); здесь ключи заглушки выведены из кода профиля и публичны
  (``inbounds.stub_private_key``);
- раздел ``routing`` — да, пусть и без правил: без него ядро подставляет маршрутизатор-пустышку
  (``routing.DefaultRouter``), чей ``AddRule`` отвечает ``ErrNoClue``, и правила
  ``RoutingService`` (блокировка адресов R-27, стратегия ``route_block``) ставить некуда; трафик
  без правил идёт в первый outbound — ``direct``;
- числа — только целые в пределах ±2⁵³, строки — без U+2028, U+2029 и управляющих символов:
  иначе каноническая запись Python и Go расходится и ``config.checksum`` не сходится на ноде
  (``contracts/agent_v1/README.md``, «Контрольная сумма конфигурации»).
"""

from __future__ import annotations

import copy
from typing import Any

from app.domain.inbounds import (
    STUB_LIMIT_FALLBACK_DOWNLOAD,
    STUB_LIMIT_FALLBACK_UPLOAD,
    STUB_MIN_CLIENT_VER,
    STUB_PORTS,
    STUB_SERVER_NAMES,
    STUB_SHORT_IDS,
    STUB_TARGET,
    STUB_XHTTP_PATH,
    Inbound,
    stub_private_key,
    xray_key,
)
from app.domain.nodes import Node
from app.domain.plans import InboundProfile

# gRPC API Xray только на петле: агент ходит в него локально (§5.2), снаружи порта нет.
API_LISTEN = "127.0.0.1:10085"
API_SERVICES = ["HandlerService", "StatsService", "RoutingService"]


def _limit_fallback(values: dict[str, int]) -> dict[str, int]:
    """Объект ``limitFallback*`` Xray из полей ``params`` (snake_case → имена ядра)."""
    return {
        "afterBytes": values["after_bytes"],
        "bytesPerSec": values["bytes_per_sec"],
        "burstBytesPerSec": values["burst_bytes_per_sec"],
    }


def _reality(profile: InboundProfile) -> dict[str, Any]:
    """``realitySettings`` сервера: поля §4.4 источника без клиентских ``spiderX`` и
    ``fingerprint``; ``xver`` — 0 (В-17), ``maxClientVer`` не задан (необязателен)."""
    return {
        "target": STUB_TARGET,
        "xver": 0,
        "serverNames": list(STUB_SERVER_NAMES),
        "privateKey": xray_key(stub_private_key(profile)),
        "minClientVer": STUB_MIN_CLIENT_VER,
        "shortIds": [STUB_SHORT_IDS[profile]],
        "limitFallbackUpload": _limit_fallback(STUB_LIMIT_FALLBACK_UPLOAD),
        "limitFallbackDownload": _limit_fallback(STUB_LIMIT_FALLBACK_DOWNLOAD),
    }


STUB_XRAY_CONFIG: dict[str, Any] = {
    "log": {"loglevel": "warning"},
    "api": {"tag": "api", "listen": API_LISTEN, "services": list(API_SERVICES)},
    # Счётчики пользователей включаются уровнем политики и самим разделом ``stats``: без него
    # ``StatsService`` отвечает пустым набором, и учёт трафика §5.9 читать нечего.
    "stats": {},
    "policy": {
        "levels": {
            "0": {"statsUserUplink": True, "statsUserDownlink": True, "statsUserOnline": True}
        }
    },
    "inbounds": [
        {
            "tag": "vless_raw_vision",
            "port": STUB_PORTS["vless_raw_vision"],
            "protocol": "vless",
            "settings": {"users": [], "decryption": "none", "flow": "xtls-rprx-vision"},
            "streamSettings": {
                "method": "raw",
                "security": "reality",
                "realitySettings": _reality("vless_raw_vision"),
                "sockopt": {"trustedXForwardedFor": []},
            },
        },
        {
            "tag": "vless_xhttp",
            "port": STUB_PORTS["vless_xhttp"],
            "protocol": "vless",
            "settings": {"users": [], "decryption": "none"},
            "streamSettings": {
                "method": "xhttp",
                "security": "reality",
                "realitySettings": _reality("vless_xhttp"),
                "xhttpSettings": {
                    "path": STUB_XHTTP_PATH,
                    "mode": "auto",
                    "xPaddingBytes": "100-1000",
                    "xPaddingObfsMode": False,
                    "xPaddingKey": "x_padding",
                    "xPaddingHeader": "X-Padding",
                    "xPaddingPlacement": "queryInHeader",
                    "xPaddingMethod": "repeat-x",
                    "uplinkHTTPMethod": "POST",
                    "sessionIDPlacement": "path",
                    "sessionIDKey": "",
                },
                "sockopt": {"trustedXForwardedFor": []},
            },
        },
        {
            "tag": "trojan_reality",
            "port": STUB_PORTS["trojan_reality"],
            "protocol": "trojan",
            "settings": {"users": []},
            "streamSettings": {
                "method": "raw",
                "security": "reality",
                "realitySettings": _reality("trojan_reality"),
                "sockopt": {"trustedXForwardedFor": []},
            },
        },
    ],
    # Первый outbound — назначение трафика без правил, поэтому ``direct`` стоит первым.
    # ``blackhole`` — цель правил блокировки: агент ставит правило ``RoutingService`` на этот тег,
    # а не создаёт outbound на лету — блокировка адресов пользователя R-27 (001.58) и стратегия
    # разрыва ``route_block``, если её выберет PoC-1а (001.88, 001.82).
    "outbounds": [
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "blackhole", "protocol": "blackhole"},
    ],
    # Пустой набор правил включает маршрутизатор ядра: правила приходят от агента по gRPC.
    "routing": {"rules": []},
}


class XrayConfigBuilder:
    """Полная конфигурация Xray ноды без credentials пользователей (§5.2)."""

    def build(self, node: Node, inbounds: list[Inbound]) -> dict[str, Any]:
        """Документ конфигурации для ``node`` и её ``inbounds``. Заглушка 001.26 возвращает
        фиксированный документ (копию: вызывающий код вправе его менять); сборка по
        аргументам — 001.27."""
        return copy.deepcopy(STUB_XRAY_CONFIG)
