"""Стенд приёма отчётов на живой базе (задача 001.34): нода с назначением и действующей identity,
пользователи в её составе, клиент раздела ``/agent/v1`` через настоящие зависимости и строитель
частей отчёта во времени базы.

Раздел опознаёт ноду настоящим ``current_node`` (отпечаток сертификата → ``node_identities`` →
токен): identity заводится строкой таблицы с самоподписанным листом, потому что подлинность
цепочки проверяет прокси, а не приложение (security.md §7.1). Время отчётов берётся из часов
базы, а не машины тестов: допуск §5.9 (5 с) меньше шага часов VM стенда (prltimesync), и метка с
часов Mac давала бы ложные отказы по времени.

Всё, что стенд заводит, живёт под префиксом уборки каталога (``_catalog.PREFIX``) и удаляется
по нему — до и после теста; строки ``traffic_lines`` (без DELETE у роли приложения, R-23)
убирает подключение владельца.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import secrets
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import httpx
from app.domain.nodes import NodeStatus
from app.domain.users import hash_token
from app.main import create_app
from app.security.ca import InternalCA
from cryptography import x509

from tests._pki import escaped, make_ca

from ._catalog import PREFIX, billing_group, catalog, unique_name

USER_EMAIL = f"{PREFIX}r34-%@example.test"
AGENT_IP = "203.0.113.34"


@dataclass(frozen=True, slots=True)
class Line:
    user_id: uuid.UUID
    uplink_bytes: int = 0
    downlink_bytes: int = 0


@dataclass
class ReportingStand:
    """Нода, её пользователи и клиент раздела. ``base`` — момент базы на старте теста, от него
    строятся границы периодов (``at``)."""

    conn: asyncpg.Connection
    app: Any
    node_id: uuid.UUID
    users: list[uuid.UUID]
    headers: dict[str, str]
    fingerprint: str
    base: dt.datetime
    epoch: uuid.UUID = field(default_factory=uuid.uuid4)

    def at(self, seconds: float) -> dt.datetime:
        return self.base + dt.timedelta(seconds=seconds)

    def part(
        self,
        seq: int,
        start: float,
        end: float | None = None,
        *,
        total: int = 1,
        lines: Sequence[Line] = (),
        ips: Sequence[tuple[uuid.UUID, str]] = (),
        rx: int = 0,
        tx: int = 0,
        epoch: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Часть отчёта: границы — секунды от ``base`` (конец по умолчанию — через 60 с)."""
        period_start = self.at(start)
        period_end = self.at(start + 60 if end is None else end)
        return {
            "counter_epoch": str(epoch or self.epoch),
            "report_seq": seq,
            "parts_total": total,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "lines": [
                {
                    "user_id": str(line.user_id),
                    "uplink_bytes": line.uplink_bytes,
                    "downlink_bytes": line.downlink_bytes,
                }
                for line in lines
            ],
            "online_ips": [
                {"user_id": str(user), "ip": ip, "last_seen": period_start.isoformat()}
                for user, ip in ips
            ],
            "node_rx_bytes": rx,
            "node_tx_bytes": tx,
        }

    async def post(
        self, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> httpx.Response:
        """Часть отчёта от ноды стенда; ``headers`` — заголовки целиком вместо заголовков
        стенда (воспроизведение фикстур контракта)."""
        transport = httpx.ASGITransport(
            app=self.app, raise_app_exceptions=False, client=(AGENT_IP, 4000)
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.post(
                "/agent/v1/reports", json=body, headers=headers or self.headers
            )

    async def report_row(self, seq: int, epoch: uuid.UUID | None = None) -> asyncpg.Record | None:
        row: asyncpg.Record | None = await self.conn.fetchrow(
            "select status, parts_total, period_start, period_end, node_rx_bytes, node_tx_bytes "
            "from traffic_reports where node_id = $1 and counter_epoch = $2 and report_seq = $3",
            self.node_id,
            epoch or self.epoch,
            seq,
        )
        return row

    async def interval(
        self, start: float, end: float | None = None, epoch: uuid.UUID | None = None
    ) -> asyncpg.Record | None:
        row: asyncpg.Record | None = await self.conn.fetchrow(
            "select * from report_intervals where node_id = $1 and counter_epoch = $2 "
            "and period_start = $3 and period_end = $4",
            self.node_id,
            epoch or self.epoch,
            self.at(start),
            self.at(start + 60 if end is None else end),
        )
        return row

    async def statuses(self) -> list[str]:
        rows = await self.conn.fetch(
            "select status::text from traffic_reports where node_id = $1 order by received_at, "
            "report_seq",
            self.node_id,
        )
        return [r["status"] for r in rows]


async def db_now(conn: asyncpg.Connection) -> dt.datetime:
    moment: dt.datetime = await conn.fetchval("select clock_timestamp()")
    return moment


async def _purge_lines(owner: asyncpg.Connection) -> None:
    """Строки ``traffic_lines`` нод под префиксом: внешнего ключа у них нет, и после уборки
    каталога ноду по коду уже не найти — поэтому до неё."""
    await owner.execute(
        "delete from traffic_lines where node_id in (select id from nodes where code like $1)",
        f"{PREFIX}%",
    )


async def insert_identity(
    conn: asyncpg.Connection,
    node_id: uuid.UUID,
    issued_at: dt.datetime,
    generation: int = 1,
) -> tuple[dict[str, str], str]:
    """Действующая identity ноды: самоподписанный лист (цепочку проверяет прокси, не
    приложение), токен base64url. Возвращает заголовки раздела и отпечаток."""
    _, cert_pem = make_ca(f"node-{node_id}", ca=False)
    cert = x509.load_pem_x509_certificate(cert_pem)
    token = secrets.token_urlsafe(32)
    fingerprint = InternalCA.fingerprint(cert_pem.decode())
    await conn.execute(
        "insert into node_identities (node_id, cert_fingerprint, token_hash, generation, "
        "issued_at, expires_at, enrolled_from, cert_serial) "
        "values ($1, $2, $3, $4, $5, $5::timestamptz + interval '90 days', '203.0.113.34', $6)",
        node_id,
        fingerprint,
        hash_token(token),
        generation,
        issued_at,
        format(cert.serial_number, "x"),
    )
    headers = {
        "X-Agent-Version": "0.1.0",
        "X-Client-Cert": escaped(cert_pem.decode()),
        "X-Node-Identity": token,
    }
    return headers, fingerprint


@asynccontextmanager
async def reporting(
    pg_dsn: str,
    migrate_env: dict[str, str],
    *,
    status: NodeStatus = "active",
    bandwidth_mbps: int = 1000,
    users: int = 3,
    age: dt.timedelta = dt.timedelta(days=2),
    node_id: uuid.UUID | None = None,
    member_ids: Sequence[uuid.UUID] = (),
) -> AsyncIterator[ReportingStand]:
    """Нода в ``status`` с полосой ``bandwidth_mbps``, введённая ``age`` назад (назначение и
    identity — с того же момента), и ``users`` пользователей в её составе. ``node_id`` и
    ``member_ids`` — заданные идентификаторы ноды и дополнительных пользователей состава (для
    воспроизведения фикстур контракта, где они записаны). Пул приложения создаётся заново:
    глобальный пул прошлого теста привязан к закрытому циклу событий.

    Порядок уборки: строки ``traffic_lines`` — до уборки каталога (после неё ноду по коду не
    найти), пользователи — после (их держит состав ноды); и то и другое — подключением
    владельца, которое живёт дольше подключения каталога."""
    from app.db import pool as pool_module

    await pool_module.close_pool()
    await pool_module.get_pool()
    owner = await asyncpg.connect(
        migrate_env["MIGRATE_DSN"].replace("postgresql+psycopg://", "postgresql://", 1)
    )
    await owner.execute("set role app_owner")
    await owner.execute("set search_path to control_plane")
    try:
        await _purge_lines(owner)
        async with catalog(pg_dsn, migrate_env) as conn:
            await owner.execute("delete from users where email like $1", USER_EMAIL)
            try:
                yield await _stand(
                    conn, owner, status, bandwidth_mbps, users, age, node_id, member_ids
                )
            finally:
                await _purge_lines(owner)
        await owner.execute("delete from users where email like $1", USER_EMAIL)
    finally:
        await owner.close()
        await pool_module.close_pool()


async def _stand(
    conn: asyncpg.Connection,
    owner: asyncpg.Connection,
    status: NodeStatus,
    bandwidth_mbps: int,
    users: int,
    age: dt.timedelta,
    fixed_node: uuid.UUID | None,
    member_ids: Sequence[uuid.UUID],
) -> ReportingStand:
    await conn.fetchval("select ensure_partitions(1)")
    introduced = await db_now(conn) - age
    group = await billing_group(conn, "r34")
    node_id: uuid.UUID = await conn.fetchval(
        "insert into nodes (id, code, name, country, city, provider, public_ipv4, "
        "billing_group_id, bandwidth_mbps, max_conn_per_ip, status, created_at) "
        "values (coalesce($6, uuidv7()), $1, $1, 'JP', 'Tokyo', 'probe', '203.0.113.34', $2, $3, "
        "8, $4, $5) returning id",
        unique_name("r34"),
        group,
        bandwidth_mbps,
        status,
        introduced,
        fixed_node,
    )
    await conn.execute(
        "insert into node_billing_assignments (node_id, billing_group_id, valid_from) "
        "values ($1, $2, $3)",
        node_id,
        group,
        introduced,
    )
    headers, fingerprint = await insert_identity(conn, node_id, introduced)
    members = []
    wanted: list[uuid.UUID | None] = [None] * users + list(member_ids)
    for n, wanted_id in enumerate(wanted):
        members.append(await insert_user(conn, node_id, n + 1, wanted_id))
    now = await db_now(conn)
    base = now.replace(microsecond=0) - dt.timedelta(minutes=10)
    if base.astimezone(dt.UTC).date() != now.astimezone(dt.UTC).date():
        # Первые десять минут суток UTC: строка на ``base`` ложится во вчерашнюю партицию, а
        # ``ensure_partitions`` создаёт их от текущих суток — вчерашнюю создаёт владелец.
        day = base.astimezone(dt.UTC).date()
        name = f"traffic_lines_p{day:%Y%m%d}"
        await owner.execute(
            f"create table if not exists {name} partition of traffic_lines "  # noqa: S608 — имя из даты
            f"for values from ('{day} 00:00+00') to ('{day + dt.timedelta(days=1)} 00:00+00')"
        )
        await owner.execute(f"revoke update, delete on {name} from app_rw")
    return ReportingStand(
        conn=conn,
        app=create_app(),
        node_id=node_id,
        users=members,
        headers=headers,
        fingerprint=fingerprint,
        base=base,
    )


async def insert_user(
    conn: asyncpg.Connection,
    member_of: uuid.UUID | None = None,
    seq: int = 1,
    wanted_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Пользователь стенда (адрес под префиксом уборки) и, если задана нода ``member_of``, —
    строка её состава с номером ``seq``; без ноды — пользователь, не состоящий ни в одной."""
    user_id: uuid.UUID = await conn.fetchval(
        "insert into users (id, email, password_hash, aup_version, aup_accepted_at) "
        "values (coalesce($2, uuidv7()), $1, 'x', '2026-09', now()) returning id",
        USER_EMAIL.replace("%", uuid.uuid4().hex[:10]),
        wanted_id,
    )
    if member_of is not None:
        await conn.execute(
            "insert into node_user_state (node_id, user_id, state, updated_seq) "
            "values ($1, $2, 'active', $3)",
            member_of,
            user_id,
            seq,
        )
    return user_id


async def insert_node(conn: asyncpg.Connection, like: uuid.UUID) -> uuid.UUID:
    """Ещё одна нода стенда — копия ``like`` под своим кодом (префикс уборки), без назначения и
    identity: владелец чужого состава."""
    node_id: uuid.UUID = await conn.fetchval(
        "insert into nodes (id, code, name, country, city, provider, public_ipv4, "
        "billing_group_id, bandwidth_mbps, max_conn_per_ip, status, created_at) "
        "select uuidv7(), $2, $2, country, city, provider, public_ipv4, billing_group_id, "
        "bandwidth_mbps, max_conn_per_ip, status, created_at from nodes where id = $1 "
        "returning id",
        like,
        unique_name("r34"),
    )
    return node_id


async def wait_blocked_by(conn: asyncpg.Connection, holder_pid: int) -> None:
    """Дождаться подключения, ждущего блокировку подключения ``holder_pid``: порядок гонки задаёт
    база, а не пауза цикла событий."""
    for _ in range(400):
        blocked = await conn.fetchval(
            "select count(*) from pg_stat_activity where $1 = any(pg_blocking_pids(pid))",
            holder_pid,
        )
        if blocked:
            return
        await asyncio.sleep(0.025)
    raise AssertionError(f"за подключением {holder_pid} никто не встал")
