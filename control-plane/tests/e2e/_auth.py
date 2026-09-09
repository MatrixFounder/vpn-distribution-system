"""Помощники сквозных тестов аутентификации (001.14) на живом стенде.

Каждый тест работает под собственным адресом клиента (``ASGITransport(client=…)``) — счётчики
частоты по адресу источника не пересекаются между тестами; адреса почты — уникальные под доменом
``test-auth.local``. Уборка: пользователи (каскадом токены), их ``auth_events``, задачи
``send_email`` с их адресами, сессии в Redis, счётчики ``rl:*`` с адресами и почтой теста.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import httpx
import redis.asyncio as redis_async
from app.main import create_app
from app.security.sessions import SessionStore, subject_sessions_key

DOMAIN = "test-auth.local"
PASSWORD = "correct horse battery staple"  # noqa: S105 — тестовый пароль


@dataclass
class AuthStand:
    """Ресурсы одного теста: пул, Redis, приложение и учёт следов для уборки."""

    pool: asyncpg.Pool
    redis: redis_async.Redis
    app: Any
    emails: set[str] = field(default_factory=set)
    ips: set[str] = field(default_factory=set)
    _ip_counter: int = 1

    def email(self, label: str = "user") -> str:
        address = f"{label}-{uuid.uuid4().hex[:10]}@{DOMAIN}"
        self.emails.add(address)
        return address

    def client(self, ip: str | None = None) -> httpx.AsyncClient:
        """Клиент с собственным адресом источника (203.0.113.0/24 — документационный диапазон)."""
        if ip is None:
            ip = f"203.0.113.{self._ip_counter}"
            self._ip_counter += 1
        self.ips.add(ip)
        transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=False, client=(ip, 4000))
        return httpx.AsyncClient(transport=transport, base_url="http://t")

    async def mail_token(self, address: str, kind: str) -> str:
        """Токен из письма, стоящего в очереди (обработчик писем — 001.52)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "select payload from jobs where type = 'send_email' and payload->>'to' = $1 "
                "and payload->>'kind' = $2 order by id desc",
                address,
                kind,
            )
        assert rows, f"письма {kind} для {address} в очереди нет"
        payload = rows[0]["payload"]
        data = json.loads(payload) if isinstance(payload, str) else payload
        token: str = data["token"]
        return token

    async def mail_count(self, address: str, kind: str) -> int:
        async with self.pool.acquire() as conn:
            count: int = await conn.fetchval(
                "select count(*) from jobs where type = 'send_email' and payload->>'to' = $1 "
                "and payload->>'kind' = $2",
                address,
                kind,
            )
        return count

    async def user(self, address: str) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "select id, email, email_verified_at, status, aup_version, aup_accepted_at, "
                "language from users where email = $1",
                address,
            )

    async def auth_events(self, user_id: uuid.UUID) -> list[tuple[str, str]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "select kind::text, result from auth_events where user_id = $1 order by ts, id",
                user_id,
            )
        return [(r["kind"], r["result"]) for r in rows]

    async def set_setting(self, key: str, value: Any) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "insert into settings (key, value) values ($1, $2::jsonb) "
                "on conflict (key) do update set value = excluded.value, updated_at = now()",
                key,
                json.dumps(value),
            )

    async def register_verified(self, address: str, ip: str | None = None) -> uuid.UUID:
        """Зарегистрировать и подтвердить адрес — общий пролог для входа и восстановления."""
        async with self.client(ip) as client:
            response = await client.post(
                "/api/v1/auth/register",
                json={"email": address, "password": PASSWORD, "aup_version": "2026-09"},
            )
            assert response.status_code == 201, response.text
            token = await self.mail_token(address, "verify")
            verified = await client.post("/api/v1/auth/verify", json={"token": token})
            assert verified.status_code == 200, verified.text
        user = await self.user(address)
        assert user is not None
        user_id: uuid.UUID = user["id"]
        return user_id


@asynccontextmanager
async def auth_stand(
    pg_dsn: str, redis_url: str, settings_overrides: dict[str, Any] | None = None
) -> AsyncIterator[AuthStand]:
    """Стенд теста: партиции на сегодня (журнал auth_events), настройки по умолчанию, уборка."""
    from app.db import pool as pool_module
    from app.redis import close_redis, get_redis

    await pool_module.close_pool()
    await close_redis()
    pool = await pool_module.get_pool()
    client = await get_redis()
    stand = AuthStand(pool=pool, redis=client, app=create_app())
    defaults = {
        "registration_mode": "open",
        "disposable_email_domains": [],
        "captcha": {"enabled": False},
    }
    async with pool.acquire() as conn:
        await conn.fetchval("select ensure_partitions(1)")  # auth_events сегодня и завтра
    for key, value in {**defaults, **(settings_overrides or {})}.items():
        await stand.set_setting(key, value)
    try:
        yield stand
    finally:
        for key, value in defaults.items():
            await stand.set_setting(key, value)
        async with pool.acquire() as conn:
            ids = [
                r["id"]
                for r in await conn.fetch(
                    "select id from users where email ilike $1", f"%@{DOMAIN}"
                )
            ]
            if ids:
                await conn.execute("delete from auth_events where user_id = any($1::uuid[])", ids)
                await conn.execute("delete from users where id = any($1::uuid[])", ids)
            await conn.execute(
                "delete from jobs where type = 'send_email' and payload->>'to' ilike $1",
                f"%@{DOMAIN}",
            )
        store = SessionStore(client)
        for user_id in ids:
            await store.revoke_all(str(user_id))
            await client.delete(subject_sessions_key(str(user_id)))
        domains = {email.rsplit("@", 1)[1] for email in stand.emails}
        for needle in stand.ips | stand.emails | domains | {DOMAIN}:
            async for key in client.scan_iter(match=f"rl:*{needle}*"):
                await client.delete(key)
        await pool_module.close_pool()
        await close_redis()
