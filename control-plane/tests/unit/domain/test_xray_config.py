"""Конфигурация Xray заглушки 001.26 против эталона ``golden/xray-config.json``: форма серверного
документа трёх профилей §4.4 в именах Xray-core v26.10.21 (WI-36), ограничения канонической
записи (``contracts/agent_v1/README.md``, «Контрольная сумма конфигурации») и согласованность с
фиксированными inbound заглушки (``inbounds.stub_inbounds``).

Эталон правится людьми: изменение документа обязано быть видно в diff эталона, тест его только
читает. Свойства критериев приёмки проверяются по самому эталону — значит, и по выдаче
``build``, которая ему равна."""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.domain.inbounds import stub_inbounds
from app.domain.nodes import stub_node
from app.domain.xray_config import XrayConfigBuilder
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

GOLDEN = Path(__file__).parent / "golden" / "xray-config.json"
JSON_SAFE_INTEGER = 2**53
LIMIT_FALLBACK_KEYS = {"afterBytes", "bytesPerSec", "burstBytesPerSec"}
# Ключи, которые декодер Xray-core v26.10.21 знает, — теги ``json`` структур ``infra/conf`` тега
# (``xray.go`` Config/InboundDetourConfig/OutboundDetourConfig, ``api.go``, ``policy.go``,
# ``router.go``, ``vless.go``, ``trojan.go``, ``transport_internet.go`` StreamConfig,
# ``transport_security.go`` REALITYConfig, ``transport_method.go`` SplitHTTPConfig,
# ``transport_sockopt.go`` SocketConfig). Декодер пропускает неизвестный ключ молча, и опечатка
# или устаревшее имя превращаются в значение по умолчанию — поэтому каждый ключ документа обязан
# быть в списке своего объекта. Списки — литералы: при пересмотре версии (§17.3) их сверяют с
# исходниками нового тега заново.
CORE_KEYS: dict[str, set[str]] = {
    "config": set(
        "transport env log routing dns inbounds outbounds policy api metrics stats reverse "
        "fakeDns observatory burstObservatory version geodata".split()
    ),
    "api": set("tag listen services".split()),
    "policy_level": set(
        "handshake connIdle uplinkOnly downlinkOnly statsUserUplink statsUserDownlink "
        "statsUserOnline bufferSize".split()
    ),
    "routing": set("rules domainStrategy balancers script".split()),
    "inbound": set("protocol port listen settings tag streamSettings sniffing".split()),
    "outbound": set(
        "protocol sendThrough tag settings streamSettings proxySettings mux targetStrategy".split()
    ),
    "vless": set("users clients decryption fallbacks flow testseed".split()),
    "trojan": set("users clients fallbacks".split()),
    "stream": set(
        "address port method network security finalmask tlsSettings realitySettings rawSettings "
        "tcpSettings xhttpSettings splithttpSettings kcpSettings grpcSettings wsSettings "
        "httpupgradeSettings hysteriaSettings masqueSettings xdriveSettings sockopt".split()
    ),
    "reality": set(
        "masterKeyLog show target dest type xver serverNames privateKey minClientVer maxClientVer "
        "maxTimeDiff shortIds mldsa65Seed limitFallbackUpload limitFallbackDownload fingerprint "
        "serverName password publicKey shortId mldsa65Verify spiderX".split()
    ),
    "xhttp": set(
        "host path mode headers xPaddingBytes xPaddingObfsMode xPaddingKey xPaddingHeader "
        "xPaddingPlacement xPaddingMethod uplinkHTTPMethod sessionIDPlacement sessionIDKey "
        "sessionIDTable sessionIDLength seqPlacement seqKey uplinkDataPlacement uplinkDataKey "
        "uplinkChunkSize noGRPCHeader noSSEHeader scMaxEachPostBytes scMinPostsIntervalMs "
        "scMaxBufferedPosts scStreamUpServerSecs serverMaxHeaderBytes xmux downloadSettings "
        "extra".split()
    ),
    "sockopt": set(
        "mark tcpFastOpen tproxy acceptProxyProtocol domainStrategy dialerProxy "
        "tcpKeepAliveInterval tcpKeepAliveIdle tcpCongestion tcpWindowClamp tcpMaxSeg penetrate "
        "tcpUserTimeout v6only interface tcpMptcp customSockopt addressPortStrategy happyEyeballs "
        "trustedXForwardedFor".split()
    ),
}
# Псевдонимы и прежние имена, которые ядро ещё знает, но которыми документ не пишется (WI-36):
# ``clients`` при обоих ключах берётся вместо ``users``, ``tcpSettings`` и ``splithttpSettings``
# — прежние имена ``rawSettings`` и ``xhttpSettings``; клиентские поля REALITY серверу не нужны.
ALIASES_AND_CLIENT_FIELDS = set(
    "clients network dest tcpSettings splithttpSettings fingerprint "
    "serverName password publicKey shortId mldsa65Verify spiderX".split()
)


def golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def built() -> dict[str, Any]:
    node = stub_node()
    return XrayConfigBuilder().build(node, stub_inbounds(node.id))


def walk(value: Any, path: str = "$") -> Iterator[tuple[str, Any]]:
    """Все узлы документа с путём: ключи объектов — тоже узлы (строки)."""
    yield path, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield f"{path}.{key}#key", key
            yield from walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk(item, f"{path}[{index}]")


def keys(document: dict[str, Any]) -> set[str]:
    return {node for path, node in walk(document) if path.endswith("#key")}


def inbound(document: dict[str, Any], tag: str) -> dict[str, Any]:
    (found,) = [item for item in document["inbounds"] if item["tag"] == tag]
    return found  # type: ignore[no-any-return]


def x25519_public(private_b64: str) -> str:
    """Открытый ключ к закрытому в записи Xray. Запись проверяется строго — base64url без
    дополнения, 43 символа: ядро разбирает ключ ``base64.RawURLEncoding`` и ключ с ``=``,
    ``+`` или ``/`` отвергает (``invalid "privateKey"``), а снисходительный декодер принял бы
    его."""
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", private_b64), private_b64
    raw = base64.urlsafe_b64decode(private_b64 + "=")
    assert len(raw) == 32, private_b64
    public = X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw()
    return base64.urlsafe_b64encode(public).rstrip(b"=").decode()


def test_build_returns_the_golden_document() -> None:
    assert built() == golden()


def test_build_hands_out_a_copy() -> None:
    """Вызывающий код (001.29 запишет документ в ``node_config_versions``) вправе менять
    полученный словарь — фиксированный документ заглушки от этого меняться не должен."""
    first = built()
    first["inbounds"][0]["streamSettings"]["realitySettings"]["shortIds"].append("ff")
    first["log"]["loglevel"] = "debug"
    assert built() == golden()


def test_three_profiles_on_distinct_ports_with_reality() -> None:
    """Набор ключей inbound и его ``settings`` — равенством: ключ, который ядро знает, но
    документ задавать не должен (``listen`` по образцу ``api`` закрыл бы профиль для клиентов),
    прошёл бы список допустимых имён."""
    document = golden()
    for item in document["inbounds"]:
        assert set(item) == {"tag", "port", "protocol", "settings", "streamSettings"}, item["tag"]
    assert [set(item["settings"]) for item in document["inbounds"]] == [
        {"users", "decryption", "flow"},
        {"users", "decryption"},
        {"users"},
    ]
    tags = [item["tag"] for item in document["inbounds"]]
    assert tags == ["vless_raw_vision", "vless_xhttp", "trojan_reality"]
    assert len({item["port"] for item in document["inbounds"]}) == 3, "UNIQUE (node_id, port)"
    assert [item["protocol"] for item in document["inbounds"]] == ["vless", "vless", "trojan"]
    assert [item["streamSettings"]["method"] for item in document["inbounds"]] == [
        "raw",
        "xhttp",
        "raw",
    ]
    for item in document["inbounds"]:
        assert item["streamSettings"]["security"] == "reality", item["tag"]


def test_reality_carries_the_server_fields_of_section_4_4() -> None:
    """Критерий приёмки: поля §4.4 источника, включая ``minClientVer`` и ``xver`` = 0 (В-17);
    ``limitFallbackUpload`` и ``limitFallbackDownload`` — объекты из трёх полей, а не числа
    колонок ``reality_limit_fb_*``."""
    for item in golden()["inbounds"]:
        reality = item["streamSettings"]["realitySettings"]
        assert set(reality) == {
            "target",
            "xver",
            "serverNames",
            "privateKey",
            "minClientVer",
            "shortIds",
            "limitFallbackUpload",
            "limitFallbackDownload",
        }, item["tag"]
        assert reality["xver"] == 0, item["tag"]
        assert reality["minClientVer"] == "1.8.1", "версия sing-box (WI-36)"
        assert reality["serverNames"] and reality["shortIds"], item["tag"]
        for direction in ("limitFallbackUpload", "limitFallbackDownload"):
            assert set(reality[direction]) == LIMIT_FALLBACK_KEYS, (item["tag"], direction)
            assert all(isinstance(v, int) for v in reality[direction].values())


def test_client_only_fields_stay_out_of_the_server_config() -> None:
    """``spiderX`` и ``fingerprint`` — параметры клиента: они идут в клиентские ссылки (001.43).
    Проверяется по всем ключам документа, а не по одному месту, где их ждёшь."""
    found = keys(golden())
    for name in ("spiderX", "fingerprint", "publicKey", "shortId", "serverName", "password"):
        assert name not in found, name


def test_every_key_is_one_the_core_reads() -> None:
    """Критерий приёмки и WI-36: каждый ключ каждого объекта документа — из имён, которые знает
    декодер v26.10.21; переименованный или устаревший ключ (``sessionPlacement``,
    ``xPaddingObfuscationMode``) ядро пропустило бы молча и применило бы умолчание."""
    document = golden()

    def known(obj: dict[str, Any], kind: str, where: str) -> None:
        unknown = set(obj) - CORE_KEYS[kind]
        assert not unknown, (where, unknown)
        assert not set(obj) & ALIASES_AND_CLIENT_FIELDS, (
            where,
            set(obj) & ALIASES_AND_CLIENT_FIELDS,
        )

    known(document, "config", "$")
    known(document["api"], "api", "api")
    known(document["routing"], "routing", "routing")
    for level, policy in document["policy"]["levels"].items():
        known(policy, "policy_level", f"policy.levels.{level}")
    for item in document["outbounds"]:
        known(item, "outbound", item["tag"])
    for item in document["inbounds"]:
        known(item, "inbound", item["tag"])
        known(item["settings"], item["protocol"], f"{item['tag']}.settings")
        stream = item["streamSettings"]
        known(stream, "stream", f"{item['tag']}.streamSettings")
        known(stream["realitySettings"], "reality", f"{item['tag']}.realitySettings")
        known(stream["sockopt"], "sockopt", f"{item['tag']}.sockopt")
        if "xhttpSettings" in stream:
            known(stream["xhttpSettings"], "xhttp", f"{item['tag']}.xhttpSettings")


def test_names_are_those_of_xray_v26_10_21() -> None:
    """Критерий приёмки и WI-36: состав — ``users``, транспорт — ``method``, XHTTP —
    ``sessionIDPlacement``/``sessionIDKey``. Прежние имена ядро либо принимает как псевдонимы
    (``clients``, ``network``), либо молча игнорирует (``sessionPlacement``, ``sessionKey``) —
    и в документе их нет нигде. Набор полей XHTTP — равенством: поле §4.4, выпавшее из
    документа, тоже молча стало бы умолчанием ядра."""
    document = golden()
    found = keys(document)
    for old in ("clients", "network", "sessionPlacement", "sessionKey", "dest"):
        assert old not in found, old
    xhttp = inbound(document, "vless_xhttp")["streamSettings"]["xhttpSettings"]
    assert set(xhttp) == {
        "path",
        "mode",
        "xPaddingBytes",
        "xPaddingObfsMode",
        "xPaddingKey",
        "xPaddingHeader",
        "xPaddingPlacement",
        "xPaddingMethod",
        "uplinkHTTPMethod",
        "sessionIDPlacement",
        "sessionIDKey",
    }, "поля профиля vless-xhttp §4.4 (xhttp_host необязателен и не задан)"
    assert xhttp["sessionIDPlacement"] in {"path", "cookie", "header", "query"}
    assert xhttp["xPaddingPlacement"] in {"cookie", "header", "query", "queryInHeader"}
    assert xhttp["xPaddingMethod"] in {"repeat-x", "tokenish"}
    assert xhttp["mode"] in {"auto", "packet-up", "stream-up", "stream-one"}
    assert xhttp["uplinkHTTPMethod"] == "POST"


def test_flow_is_set_where_the_core_accepts_it() -> None:
    """Vision — только у ``vless-raw-vision``; ``flow`` none у XHTTP — это отсутствие поля:
    значение ``none`` Xray v26.10.21 отвергает (допустимы ``xtls-rprx-vision`` и пусто). У
    Trojan ``flow`` нет."""
    document = golden()
    assert inbound(document, "vless_raw_vision")["settings"]["flow"] == "xtls-rprx-vision"
    assert "flow" not in inbound(document, "vless_xhttp")["settings"]
    assert "flow" not in inbound(document, "trojan_reality")["settings"]
    for tag in ("vless_raw_vision", "vless_xhttp"):
        assert inbound(document, tag)["settings"]["decryption"] == "none", tag


def test_no_user_credentials_in_the_config_stream() -> None:
    """Инвариант §5.2: credentials пользователей приходят потоком состава, не конфигурацией."""
    for item in golden()["inbounds"]:
        assert item["settings"]["users"] == [], item["tag"]


def test_api_stats_and_blocking_outbound() -> None:
    document = golden()
    assert document["api"]["listen"] == "127.0.0.1:10085", "gRPC API только на петле"
    assert set(document["api"]["services"]) == {"HandlerService", "StatsService", "RoutingService"}
    assert document["stats"] == {}
    assert document["policy"]["levels"]["0"] == {
        "statsUserUplink": True,
        "statsUserDownlink": True,
        "statsUserOnline": True,
    }
    assert document["outbounds"] == [
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "blackhole", "protocol": "blackhole"},
    ], "трафик без правил идёт в первый outbound — это direct, а не blackhole"
    # Без раздела routing ядро ставит маршрутизатор-пустышку, и AddRule RoutingService отвечает
    # ErrNoClue: блокировку адресов R-27 и route_block ставить некуда.
    assert "RoutingService" in document["api"]["services"]
    assert document["routing"] == {"rules": []}


def test_numbers_and_strings_survive_both_canonical_writers() -> None:
    """Критерий приёмки: числа — только целые в пределах ±2⁵³ (Go через ``map[string]any`` пишет
    ``1.0`` как ``1``, а за ±2⁵³ искажает значение), строки и ключи — без U+2028, U+2029 и
    управляющих символов (их Python и Go экранируют по-разному)."""
    checked = 0
    for path, node in walk(golden()):
        if isinstance(node, bool):
            continue
        if isinstance(node, float):
            raise AssertionError(f"{path}: дробное число {node!r}")
        if isinstance(node, int):
            assert -JSON_SAFE_INTEGER <= node <= JSON_SAFE_INTEGER, path
            checked += 1
        if isinstance(node, str):
            for char in node:
                assert char not in "\u2028\u2029", (path, node)
                assert unicodedata.category(char) != "Cc", (path, node)
    assert checked >= 20, "в документе есть числа: порты, xver, пределы fallback"


def test_the_config_matches_the_stub_inbounds() -> None:
    """Фиксированные inbound заглушки и фиксированный документ описывают одну ноду: порт,
    ``short_ids``, цель, имена, минимальная версия и поля профиля из ``params`` совпадают, а
    открытый ключ inbound — пара к закрытому ключу документа (запись ключа — строгая). Иначе
    генераторы подписки на заглушках (001.41, 001.43) выдали бы клиенту ``pbk``, ``sid`` и
    параметры XHTTP, которых сервер не ждёт."""
    document = golden()
    for item in stub_inbounds(stub_node().id):
        config = inbound(document, item.tag)
        reality = config["streamSettings"]["realitySettings"]
        assert config["port"] == item.port, item.tag
        assert reality["shortIds"] == item.reality_short_ids, item.tag
        assert reality["target"] == item.reality_target, item.tag
        assert reality["serverNames"] == item.reality_server_names, item.tag
        assert reality["minClientVer"] == item.reality_min_client_ver, item.tag
        assert x25519_public(reality["privateKey"]) == item.reality_public_key, item.tag
        params = item.params.model_dump()
        for direction in ("upload", "download"):
            assert reality[f"limitFallback{direction.capitalize()}"] == {
                "afterBytes": params[f"limit_fallback_{direction}"]["after_bytes"],
                "bytesPerSec": params[f"limit_fallback_{direction}"]["bytes_per_sec"],
                "burstBytesPerSec": params[f"limit_fallback_{direction}"]["burst_bytes_per_sec"],
            }, (item.tag, direction)
        settings = config["settings"]
        assert settings.get("flow") == params.get("flow"), (item.tag, "flow — Vision клиента")
        assert settings.get("decryption") == params.get("decryption"), item.tag
        if item.profile == "vless_xhttp":
            xhttp = config["streamSettings"]["xhttpSettings"]
            assert {
                "path": xhttp["path"],
                "mode": xhttp["mode"],
                "xPaddingBytes": xhttp["xPaddingBytes"],
                "xPaddingObfsMode": xhttp["xPaddingObfsMode"],
                "xPaddingKey": xhttp["xPaddingKey"],
                "xPaddingHeader": xhttp["xPaddingHeader"],
                "xPaddingPlacement": xhttp["xPaddingPlacement"],
                "xPaddingMethod": xhttp["xPaddingMethod"],
                "uplinkHTTPMethod": xhttp["uplinkHTTPMethod"],
                "sessionIDPlacement": xhttp["sessionIDPlacement"],
                "sessionIDKey": xhttp["sessionIDKey"],
            } == {
                "path": params["xhttp_path"],
                "mode": params["xhttp_mode"],
                "xPaddingBytes": params["x_padding_bytes"],
                "xPaddingObfsMode": params["x_padding_obfs_mode"],
                "xPaddingKey": params["x_padding_key"],
                "xPaddingHeader": params["x_padding_header"],
                "xPaddingPlacement": params["x_padding_placement"],
                "xPaddingMethod": params["x_padding_method"],
                "uplinkHTTPMethod": params["uplink_http_method"],
                "sessionIDPlacement": params["session_id_placement"],
                "sessionIDKey": params["session_id_key"],
            }, "клиентская ссылка (001.43) строится из params — сервер обязан ждать того же"
        assert config["streamSettings"]["sockopt"] == {
            "trustedXForwardedFor": item.trusted_x_forwarded_for
        }, item.tag
    keys_by_tag = [
        i["streamSettings"]["realitySettings"]["privateKey"] for i in document["inbounds"]
    ]
    assert len(set(keys_by_tag)) == 3, "у каждого inbound своя ключевая пара (inbound_secrets)"
