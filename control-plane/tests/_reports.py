"""Тела отчётов о трафике для тестов задачи 001.33 — один строитель на три набора (сквозные,
модульные, страж предела прокси), чтобы потолок и самая широкая форма каждого поля были
записаны в одном месте и менялись вместе с моделью.

``widest_report`` — самое широкое тело среди тех, что описывает контракт: столько строк и
адресов, сколько разрешают пределы, счётчики шириной ``bigint``, UUID в канонической записи
(иных модель не принимает), IPv6 с хвостом IPv4 (45 символов — самая длинная запись без zone
id), метки с шестью знаками дробной части и смещением (``TIMESTAMP_MAX_CHARS``), период ровно в
час, ``parts_total`` у предела и ``report_seq`` такой, что последняя часть ложится ровно на
предел ``bigint``. Экранированная запись тех же значений (``\u0030…``) шире, но контракт её не
описывает (README).
"""

from __future__ import annotations

import uuid
from typing import Any

from app.accounting.service import REPORT_MAX_LINES, REPORT_MAX_ONLINE_IPS, REPORT_MAX_PARTS
from app.domain.statuses import INT8_MAX

STAMP = "2026-09-10T12:00:57.123456+00:00"  # ровно TIMESTAMP_MAX_CHARS
USER_A = "00000000-0000-7000-8000-0000000000a1"
USER_B = "00000000-0000-7000-8000-0000000000a4"
VALID_REPORT: dict[str, Any] = {
    "counter_epoch": "00000000-0000-7000-8000-0000000000e1",
    "report_seq": 3,
    "parts_total": 1,
    "period_start": "2026-09-10T12:00:00Z",
    "period_end": "2026-09-10T12:01:00Z",
    "lines": [
        {"user_id": USER_A, "uplink_bytes": 1048576, "downlink_bytes": 10485760},
        {"user_id": USER_B, "uplink_bytes": 0, "downlink_bytes": 2048},
    ],
    "online_ips": [
        {"user_id": USER_A, "ip": "198.51.100.23", "last_seen": "2026-09-10T12:00:57Z"},
        {"user_id": USER_A, "ip": "2001:db8::17", "last_seen": "2026-09-10T12:00:40Z"},
    ],
    "node_rx_bytes": 12582912,
    "node_tx_bytes": 2097152,
}


def report_with(lines: int, ips: int, *, bad_lines: bool = False) -> dict[str, Any]:
    """Отчёт с заданным числом строк и адресов: пользователи и адреса различны."""
    users = [str(uuid.UUID(int=n + 1)) for n in range(max(lines, 1))]
    return {
        **VALID_REPORT,
        "lines": [
            {"user_id": users[n], "uplink_bytes": -1 if bad_lines else 1, "downlink_bytes": 1}
            for n in range(lines)
        ],
        "online_ips": [
            {
                "user_id": users[n % len(users)],
                "ip": f"10.{(n >> 16) & 255}.{(n >> 8) & 255}.{n & 255}",
                "last_seen": "2026-09-10T12:00:57Z",
            }
            for n in range(ips)
        ],
    }


def widest_report() -> dict[str, Any]:
    """Самое большое допустимое тело — из самих пределов модели и самой широкой формы каждого
    поля (см. докстринг модуля)."""
    users = [str(uuid.UUID(int=n + 1)) for n in range(REPORT_MAX_LINES)]
    return {
        "node_id": str(uuid.UUID(int=7)),
        "counter_epoch": str(uuid.UUID(int=9)),
        "report_seq": INT8_MAX - REPORT_MAX_PARTS + 1,
        "parts_total": REPORT_MAX_PARTS,
        "period_start": "2026-09-10T12:00:00.123456+00:00",
        "period_end": "2026-09-10T13:00:00.123456+00:00",
        "lines": [
            {"user_id": u, "uplink_bytes": INT8_MAX, "downlink_bytes": INT8_MAX} for u in users
        ],
        "online_ips": [
            {
                "user_id": users[n % REPORT_MAX_LINES],
                "ip": f"ffff:ffff:ffff:ffff:{n >> 16:04x}:{n & 0xFFFF:04x}:255.255.255.255",
                "last_seen": STAMP,
            }
            for n in range(REPORT_MAX_ONLINE_IPS)
        ],
        "node_rx_bytes": INT8_MAX,
        "node_tx_bytes": INT8_MAX,
    }
