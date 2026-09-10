"""Помощники сквозных тестов панели (001.18): сессия администратора создаётся прямо в Redis
через ``SessionStore`` (вход администратора с TOTP — 001.47), клиент со своим адресом источника,
cookie ``sid`` и маркер CSRF; все операции панели — ``ADMIN_OPERATIONS`` с валидными телами."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from app.security.sessions import SessionStore

from ._auth import AuthStand, auth_stand

PLAN_ID = "00000000-0000-7000-8000-0000000000c1"
GROUP_ID = "00000000-0000-7000-8000-0000000000f1"
BATCH_ID = "00000000-0000-7000-8000-0000000000d2"
CODE_ID = "00000000-0000-7000-8000-0000000000d1"
NODE_ID = "00000000-0000-7000-8000-0000000000b1"
# Идентификаторы заглушек в путях операций → параметры пути в OpenAPI.
PATH_PARAMS = {
    PLAN_ID: "{plan_id}",
    GROUP_ID: "{group_id}",
    BATCH_ID: "{batch_id}",
    CODE_ID: "{code_id}",
    NODE_ID: "{node_id}",
}
VALID_NODE: dict[str, Any] = {
    "code": "JP-Tokyo-01",
    "name": "Tokyo 1",
    "country": "JP",
    "city": "Tokyo",
    "provider": "Example Hosting",
    "public_ipv4": "203.0.113.10",
    "billing_group_id": GROUP_ID,
    "access_group_ids": [GROUP_ID],
    "bandwidth_mbps": 1000,
    "max_conn_per_ip": 32,
    "legal_profile": {"jurisdiction": "JP"},
}
VALID_PLAN: dict[str, Any] = {
    "name": "Basic",
    "price_amount": "4.99",
    "price_currency": "EUR",
    "duration_days": 30,
    "traffic_limit_bytes": 100 * 1024**3,
    "device_limit": 2,
    "access_group_ids": [GROUP_ID],
    "profiles": ["vless_raw_vision"],
}
ADMIN_OPERATIONS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/api/v1/admin/dashboard", None),
    ("GET", "/api/v1/admin/plans", None),
    ("POST", "/api/v1/admin/plans", VALID_PLAN),
    ("PATCH", f"/api/v1/admin/plans/{PLAN_ID}", {"name": "Basic+"}),
    ("DELETE", f"/api/v1/admin/plans/{PLAN_ID}", None),
    ("GET", "/api/v1/admin/groups/access", None),
    ("POST", "/api/v1/admin/groups/access", {"name": "Asia"}),
    ("PATCH", f"/api/v1/admin/groups/access/{GROUP_ID}", {"description": "APAC"}),
    ("DELETE", f"/api/v1/admin/groups/access/{GROUP_ID}", None),
    ("GET", "/api/v1/admin/groups/billing", None),
    ("POST", "/api/v1/admin/groups/billing", {"name": "premium"}),
    ("PATCH", f"/api/v1/admin/groups/billing/{GROUP_ID}", {"name": "premium+"}),
    ("DELETE", f"/api/v1/admin/groups/billing/{GROUP_ID}", None),
    ("POST", f"/api/v1/admin/groups/billing/{GROUP_ID}/multiplier", {"multiplier": "2.0"}),
    ("POST", "/api/v1/admin/codes", {"kind": "redeem", "max_uses": 5}),
    ("POST", "/api/v1/admin/codes/batch", {"count": 3, "spec": {"kind": "redeem"}}),
    ("GET", f"/api/v1/admin/codes/batch/{BATCH_ID}/export", None),
    ("GET", f"/api/v1/admin/codes/{CODE_ID}/redemptions", None),
    ("GET", "/api/v1/admin/nodes", None),
    ("POST", "/api/v1/admin/nodes", VALID_NODE),
    ("GET", f"/api/v1/admin/nodes/{NODE_ID}", None),
    ("PATCH", f"/api/v1/admin/nodes/{NODE_ID}", {"name": "Tokyo 1a"}),
    ("DELETE", f"/api/v1/admin/nodes/{NODE_ID}", None),
    ("POST", f"/api/v1/admin/nodes/{NODE_ID}/bootstrap-token", None),
    ("POST", f"/api/v1/admin/nodes/{NODE_ID}/approve", None),
    ("POST", f"/api/v1/admin/nodes/{NODE_ID}/status", {"status": "maintenance"}),
    ("POST", f"/api/v1/admin/nodes/{NODE_ID}/revoke-identity", None),
    ("GET", f"/api/v1/admin/nodes/{NODE_ID}/state", None),
]
MUTATIONS = [(m, p, b) for m, p, b in ADMIN_OPERATIONS if m != "GET"]


def openapi_path(path: str) -> str:
    """Путь операции с идентификаторами заглушек → шаблон пути OpenAPI."""
    for value, param in PATH_PARAMS.items():
        path = path.replace(value, param)
    return path


@dataclass
class AdminStand:
    stand: AuthStand
    client: httpx.AsyncClient
    admin_id: uuid.UUID
    csrf: str

    @property
    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf}


@asynccontextmanager
async def admin_stand(pg_dsn: str, redis_url: str) -> AsyncIterator[AdminStand]:
    """Стенд с сессией администратора (вид ``admin``, субъект — случайный UUID)."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        store = SessionStore(stand.redis)
        admin_id = uuid.uuid4()
        session = await store.create("admin", str(admin_id), "203.0.113.200", "test", 600)
        try:
            async with stand.client("203.0.113.200") as client:
                client.cookies.set("sid", session.id)
                yield AdminStand(stand, client, admin_id, session.csrf)
        finally:
            await store.revoke_all(str(admin_id))
