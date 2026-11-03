"""Inbound ноды (задача 001.26): три inbound профилей §4.4 при вводе ноды (TC-E2E-01), ключевая
пара x25519 (TC-UNIT-01), форма ключа Xray, тело изменения inbound и заглушки операций панели."""

from __future__ import annotations

import base64
import uuid
from typing import Any

import pytest
from app.domain.inbounds import (
    XHTTP_PORTS,
    Inbound,
    InboundParams,
    InboundPatch,
    InboundService,
    gen_x25519,
    stub_inbounds,
    xray_key,
)
from app.errors import ApiError
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pydantic import ValidationError

NODE_ID = uuid.UUID("00000000-0000-7000-8000-0000000000b1")


class NoConnection:
    """Заглушка в базу не ходит — обращение к подключению валит тест."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"обращение к базе: {name}")


async def test_tc_e2e_01_a_new_node_gets_three_inbounds_without_a_write() -> None:
    """TC-E2E-01: ``create_defaults`` — три inbound профилей MVP на разных портах (443, порт из
    набора XHTTP, отдельный порт Trojan), без записи в базу (запись — 001.27)."""
    service = InboundService(NoConnection())
    created = await service.create_defaults(NoConnection(), NODE_ID)
    assert [item.profile for item in created] == [
        "vless_raw_vision",
        "vless_xhttp",
        "trojan_reality",
    ]
    assert all(item.node_id == NODE_ID for item in created)
    ports = {item.profile: item.port for item in created}
    assert ports["vless_raw_vision"] == 443
    assert ports["vless_xhttp"] in (2053, 2083, 2087, 2096, 8443), "набор XHTTP §4.4"
    assert ports["trojan_reality"] not in (443, ports["vless_xhttp"])
    assert len(set(ports.values())) == 3, "UNIQUE (node_id, port)"
    assert len({item.id for item in created}) == 3
    assert [item.tag for item in created] == [item.profile for item in created]
    for item in created:
        assert item.enabled and item.reality_xver == 0, item.profile
        assert item.reality_min_client_ver == "1.8.1", "версия sing-box (WI-36)"
        assert item.client_fingerprint == "chrome", "набор отпечатков v26.10.31 (WI-36, WI-37)"
        assert item.error_state is None and item.error_since is None
    assert len({item.client_spider_x for item in created}) == 3, "spx — свой у профиля (§4.4)"
    other = await service.create_defaults(NoConnection(), uuid.uuid4())
    assert {item.node_id for item in other} != {NODE_ID}, "inbound принадлежат переданной ноде"


def test_the_xhttp_port_set_is_that_of_section_4_4() -> None:
    """Набор портов профиля ``vless-xhttp`` — литерал §4.4 «Профили MVP»: из него 001.27
    выбирает порт inbound, и выбор вне набора не проверялся бы ничем."""
    assert XHTTP_PORTS == (2053, 2083, 2087, 2096, 8443)


def test_tc_unit_01_x25519_pair() -> None:
    """TC-UNIT-01: 32-байтные ключи, открытый выводится из закрытого; две пары различны."""
    private, public = gen_x25519()
    assert len(private) == 32 and len(public) == 32
    derived = X25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()
    assert derived == public
    again, _ = gen_x25519()
    assert again != private, "ключ из источника случайности, а не константа"


def test_xray_key_is_base64url_without_padding() -> None:
    """Запись Xray: base64url без ``=`` — 43 символа на 32 байта; ``+`` и ``/`` недопустимы."""
    raw = bytes(range(250, 256)) + bytes(26)  # байты 0xfb…0xff дают «-» и «_» в base64url
    written = xray_key(raw)
    assert len(written) == 43 and "=" not in written
    assert "+" not in written and "/" not in written and ("-" in written or "_" in written)
    assert base64.urlsafe_b64decode(written + "=") == raw
    for wrong in (bytes(31), bytes(33)):
        with pytest.raises(ValueError):
            xray_key(wrong)


def test_the_model_holds_the_column_rules() -> None:
    """Ограничения колонок ``inbounds`` (data-model.md §4.2.3) держит модель: порт 1…65535,
    ``short_ids`` — hex чётной длины не длиннее 16, ``xver`` — только 0 (В-17), профиль — из
    перечисления ``inbound_profile``."""
    base = stub_inbounds(NODE_ID)[0].model_dump()
    for field, value in (
        ("port", 0),
        ("port", 65536),
        ("reality_short_ids", ["abc"]),
        ("reality_short_ids", ["0123456789abcdef01"]),
        ("reality_short_ids", ["ABCD"]),
        ("reality_xver", 1),
        ("profile", "vless_ws"),
    ):
        with pytest.raises(ValidationError):
            Inbound(**{**base, field: value})
    assert Inbound(**{**base, "reality_short_ids": ["", "0123456789abcdef"]})


def test_the_panel_model_is_the_column_set_without_the_private_key() -> None:
    """Набор полей ``Inbound`` — колонки ``inbounds`` (data-model.md §4.2.3) равенством: ответ
    панели (``GET /admin/nodes/{id}/inbounds``) строится по этой модели, и поле с закрытым ключом
    под любым именем ушло бы администратору. Ключ живёт в ``inbound_secrets`` (§7.2).
    ``reality_limit_fb_up``/``_down`` (int) в модель не входят: объекты ``limitFallback*`` — в
    ``params`` (001.27)."""
    assert set(Inbound.model_fields) == {
        "id",
        "node_id",
        "profile",
        "port",
        "tag",
        "enabled",
        "reality_public_key",
        "reality_short_ids",
        "reality_target",
        "reality_server_names",
        "reality_min_client_ver",
        "reality_max_client_ver",
        "reality_xver",
        "client_fingerprint",
        "client_spider_x",
        "trusted_x_forwarded_for",
        "params",
        "error_state",
        "error_since",
    }


def test_params_are_a_closed_set_of_profile_fields() -> None:
    """``params`` уходит в ответ панели целиком, поэтому его набор закрыт полями профиля §4.4 —
    лишний ключ (закрытый ключ под любым именем) отклоняется; значения — из перечислений
    источника, пределы fallback — целые до 2⁵³."""
    assert set(InboundParams.model_fields) == {
        "flow",
        "decryption",
        "xhttp_path",
        "xhttp_mode",
        "xhttp_host",
        "x_padding_bytes",
        "x_padding_obfs_mode",
        "x_padding_key",
        "x_padding_header",
        "x_padding_placement",
        "x_padding_method",
        "uplink_http_method",
        "session_id_placement",
        "session_id_key",
        "limit_fallback_upload",
        "limit_fallback_download",
    }
    base = stub_inbounds(NODE_ID)[1].model_dump()
    for bad in (
        {"reality_private_key": "sHJ1FTpexKQm3YlXg142qg4syIYGvl7Bi0yUspWbK1c"},
        {"flow": "none"},
        {"xhttp_mode": "stream"},
        {"session_id_placement": "body"},
        {"x_padding_bytes": "100..1000"},
        {
            "limit_fallback_upload": {
                "after_bytes": 2**53 + 1,
                "bytes_per_sec": 1,
                "burst_bytes_per_sec": 1,
            }
        },
        {"limit_fallback_upload": {"after_bytes": 1, "bytes_per_sec": 1}},
    ):
        with pytest.raises(ValidationError):
            Inbound.model_validate({**base, "params": {**base["params"], **bad}})
    assert Inbound.model_validate(
        {
            **base,
            "params": {
                **base["params"],
                "limit_fallback_upload": {
                    "after_bytes": 2**53,
                    "bytes_per_sec": 0,
                    "burst_bytes_per_sec": 0,
                },
            },
        }
    )


def test_trusted_x_forwarded_for_names_headers() -> None:
    """``sockopt.trustedXForwardedFor`` Xray v26.10.31 — имена заголовков (``[]string``), при
    которых ядро доверяет ``X-Forwarded-For``; адресов и сетей там нет. Имя — token HTTP."""
    base = stub_inbounds(NODE_ID)[1].model_dump()
    assert Inbound(**{**base, "trusted_x_forwarded_for": ["CF-Connecting-IP", "X-Real-IP"]})
    for bad in (["203.0.113.0/24"], ["X Real IP"], ["X-Real-IP:"], [""]):
        with pytest.raises(ValidationError):
            Inbound(**{**base, "trusted_x_forwarded_for": bad})


def test_a_patch_changes_the_target_together_with_its_names() -> None:
    """Цель и ``server_names`` меняются вместе (§4.4: SNI — допустимые имена этой цели); пустое
    тело и лишние поля — отказ. Имя — доменное имя не длиннее 253 символов (RFC 1035), цель —
    «имя:порт», не длиннее 259; границы — литералами, на самой границе и за ней."""
    assert InboundPatch(target="www.example.org:443", server_names=["www.example.org"])
    assert InboundPatch(enabled=False)
    name_253 = "a" * 61 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 63
    assert len(name_253) == 253
    assert InboundPatch(target=name_253 + ":65535", server_names=[name_253])
    oversized: tuple[dict[str, Any], ...] = (
        {"target": name_253 + "x:65535", "server_names": ["www.example.org"]},
        {"target": "www.example.org:443", "server_names": [name_253 + "x"]},
        {"target": "", "server_names": ["www.example.org"]},
        {"target": "www.example.org:443", "server_names": [""]},
    )
    for bad in oversized:
        with pytest.raises(ValidationError):
            InboundPatch.model_validate(bad)
    malformed: tuple[dict[str, Any], ...] = (
        {},
        {"target": "www.example.org:443"},
        {"server_names": ["www.example.org"]},
        {"target": "www.example.org:443", "server_names": []},
        {"enabled": True, "port": 444},
    )
    for bad in malformed:
        with pytest.raises(ValidationError):
            InboundPatch.model_validate(bad)


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        ("rotate_short_ids", ()),
        ("rotate_keypair", ("compromise",)),
        ("set_enabled", (False,)),
        ("set_target", ("www.example.org:443", ["www.example.org"])),
    ],
)
async def test_operations_not_implemented_say_so(operation: str, args: tuple[Any, ...]) -> None:
    """Операции панели над inbound — заглушки: 501 ``not_implemented`` с именем операции, база
    не трогается (логика — 001.27)."""
    service = InboundService(NoConnection())
    with pytest.raises(ApiError) as refused:
        await getattr(service, operation)(uuid.uuid4(), *args)
    assert refused.value.status == 501
    assert refused.value.code == "not_implemented"
    assert refused.value.details == {"operation": f"admin.inbounds.{operation}"}
