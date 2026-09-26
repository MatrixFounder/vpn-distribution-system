"""Процессорное время запроса enrollment в процессе Control Plane (001.25) — внутри контейнера api
стенда, вызовом ASGI без сети: ``time.process_time()`` считает время процесса (все потоки, в том
числе пул синхронных зависимостей), а ожидание базы и задержки планировщика VM в него не входят.
Это подтверждение к ``urt`` (``enrollment_timing``): занятость цикла событий по путям.

Запуск из ``control-plane`` (образ содержит только ``app``, поэтому модуль самодостаточен и
читается со stdin; пользователь — ``app``, как у самого процесса api, а не root, которого
``docker exec`` дал бы по умолчанию)::

    ssh vm 'docker exec -i -u app -e HOME=/app control-plane-api-1 python -' \
        < tests/stand/enrollment_cpu.py

Побочные эффекты — на живом ``api`` стенда: ключом CA стенда выпускается около двухсот настоящих
листов на 90 дней (ключи нод не сохраняются, строки identity удаляются уборкой — предъявить эти
листы некому); ключ RSA-16384 генерируется в квоте процессора контейнера ``api`` на общей VM
(десятки секунд одного ядра) — запускать, когда стенд не мерится другим.

Пути, по 100 запросов, по кругу: ``junk`` — отказ по форме самым широким телом (контроль, код тот
же, что в 001.33); ``token`` — неизвестный токен (запрос в базу); ``accept`` — приём (транзакция и
подпись листа); ``accept_wide`` — приём CSR на пределе ``CSR_MAX_CHARS`` (P-256, расширения
добивают PEM до предела); ``refusal_wide`` — отказ CA с годным токеном: CSR на пределе с ключом
RSA-16384 (тип ключа проверяется до подписи; токен при отказе не гасится, поэтому один на всю
серию). Уборка — по префиксу ``t25-cpu-`` в начале и в конце. Строители тел импортирует и
``enrollment_timing``."""

from __future__ import annotations

import asyncio
import json
import math
import secrets
import statistics
import time
from collections.abc import Callable
from typing import Any

from app.agent_api.enroll import CSR_MAX_CHARS
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

PREFIX = "t25-cpu-"
ROUNDS = 100
PATHS = ("junk", "token", "accept", "accept_wide", "refusal_wide")
BODY_LIMIT = 64 * 1024  # client_max_body_size enrollment-server (nginx.conf)
# Наибольший модуль, с которым OpenSSL проверяет подпись (OPENSSL_RSA_MAX_MODULUS_BITS): до
# перестановки проверок (тип ключа — раньше подписи, роаст 001.25) это был самый дорогой отказ CA.
RSA_BITS = 16_384

Key = ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey


def junk_body() -> bytes:
    """Самое широкое тело в пределах предела прокси из минимально коротких лишних ключей — то же,
    что мерила 001.33 (отказ по форме)."""
    parts: list[str] = []
    size, index = 2, 0
    while True:
        key = f'"{index:x}":0'
        if size + len(key) + 1 > BODY_LIMIT - 64:
            break
        parts.append(key)
        size += len(key) + 1
        index += 1
    return ("{" + ",".join(parts) + "}").encode()


def _csr(key: Key, names: int) -> str:
    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "stand")])
    )
    if names:
        san = x509.SubjectAlternativeName(
            [x509.DNSName(f"n{i:05d}.stand.example") for i in range(names)]
        )
        builder = builder.add_extension(san, critical=False)
    return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode()


def csr(key: Key | None = None) -> str:
    """Обычный CSR ноды: P-256, без расширений."""
    return _csr(key or ec.generate_private_key(ec.SECP256R1()), 0)


def widest_csr(key: Key) -> str:
    """CSR ключа ``key`` на пределе ``CSR_MAX_CHARS``: наибольшее число имён в расширении, при
    котором PEM ещё не длиннее предела (двоичный поиск)."""
    low, high = 0, 1
    while len(_csr(key, high)) <= CSR_MAX_CHARS:
        low, high = high, high * 2
    while high - low > 1:
        middle = (low + high) // 2
        if len(_csr(key, middle)) <= CSR_MAX_CHARS:
            low = middle
        else:
            high = middle
    return _csr(key, low)


def enroll_body(token: str, csr_pem: str) -> bytes:
    body = {"bootstrap_token": token, "csr_pem": csr_pem}
    return json.dumps({**body, "agent_version": "0.1.0", "xray_version": "26.9.1"}).encode()


def p95(values: list[float]) -> float:
    """95-й процентиль ближайшего ранга — как в замерах 001.33."""
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


async def _call(app: Any, body: bytes) -> int:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/agent/v1/enroll",
        "raw_path": b"/agent/v1/enroll",
        "query_string": b"",
        "root_path": "",
        "client": ("198.51.100.7", 4000),
        "server": ("api", 8000),
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    }
    sent = False
    status = 0

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    await app(scope, receive, send)
    return status


async def _cleanup(conn: Any) -> None:
    like = f"{PREFIX}%"
    for table in ("bootstrap_tokens", "node_identities"):  # ноды — прямой вставкой, без истории
        await conn.execute(
            f"delete from {table} where node_id in (select id from nodes where code like $1)",  # noqa: S608 — имя из списка
            like,
        )
    await conn.execute("delete from nodes where code like $1", like)
    await conn.execute("delete from billing_groups where name like $1", like)
    await conn.execute("delete from admin_users where email like $1", like)


async def main() -> None:
    from app.db.pool import get_pool
    from app.domain.nodes import NodeService
    from app.main import create_app
    from app.security.ca import get_ca

    started = time.perf_counter()
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_BITS)
    wide_rsa = widest_csr(rsa_key)
    wide_p256 = widest_csr(ec.generate_private_key(ec.SECP256R1()))
    print(
        f"тела: junk {len(junk_body())} байт, CSR на пределе — P-256 {len(wide_p256)}, "
        f"RSA-{RSA_BITS} {len(wide_rsa)} символов (предел {CSR_MAX_CHARS}); "
        f"подготовка {time.perf_counter() - started:.0f} с",
        flush=True,
    )
    app = create_app()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _cleanup(conn)
    cpu: dict[str, list[float]] = {path: [] for path in PATHS}
    wall: dict[str, list[float]] = {path: [] for path in PATHS}
    statuses: dict[str, dict[int, int]] = {path: {} for path in PATHS}
    # Всё, что пишет в базу, — внутри try: сбой на любом шаге не оставляет строк до следующего
    # прогона (роаст 001.25, раунд 2).
    try:
        async with pool.acquire() as conn:
            admin = await conn.fetchval(
                "insert into admin_users (email, password_hash, role) values ($1, 'x', 'admin') "
                "returning id",
                f"{PREFIX}admin@example.test",
            )
            billing = await conn.fetchval(
                "insert into billing_groups (name) values ($1) returning id", f"{PREFIX}bg"
            )
            nodes = [
                await conn.fetchval(
                    "insert into nodes (code, name, country, city, provider, public_ipv4, "
                    "billing_group_id, bandwidth_mbps, max_conn_per_ip) values ($1, 'n', 'JP', "
                    "'T', 'p', '203.0.113.9', $2, 1000, 8) returning id",
                    f"{PREFIX}{i:03d}",
                    billing,
                )
                for i in range(2 * ROUNDS + 1)
            ]
        service = NodeService(pool, get_ca())
        tokens = [(await service.issue_bootstrap_token(node, admin)).token for node in nodes]
        accept, accept_wide, refusal = tokens[:ROUNDS], tokens[ROUNDS : 2 * ROUNDS], tokens[-1]
        junk = junk_body()
        bodies: dict[str, Callable[[int], bytes]] = {
            "junk": lambda _: junk,
            "token": lambda _: enroll_body(secrets.token_urlsafe(32), csr()),
            "accept": lambda i: enroll_body(accept[i], csr()),
            "accept_wide": lambda i: enroll_body(accept_wide[i], wide_p256),
            "refusal_wide": lambda _: enroll_body(refusal, wide_rsa),
        }
        for i in range(ROUNDS):
            for path in PATHS:
                body = bodies[path](i)
                cpu0, wall0 = time.process_time(), time.perf_counter()
                status = await _call(app, body)
                cpu[path].append((time.process_time() - cpu0) * 1000)
                wall[path].append((time.perf_counter() - wall0) * 1000)
                statuses[path][status] = statuses[path].get(status, 0) + 1
    finally:
        async with pool.acquire() as conn:
            await _cleanup(conn)
    for path in PATHS:
        print(
            f"{path}: статусы {statuses[path]}; процессор p50 {statistics.median(cpu[path]):.2f} "
            f"p95 {p95(cpu[path]):.2f} max {max(cpu[path]):.2f} мс; стена p50 "
            f"{statistics.median(wall[path]):.2f} p95 {p95(wall[path]):.2f} мс",
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
