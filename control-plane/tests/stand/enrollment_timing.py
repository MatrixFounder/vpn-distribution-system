"""Время апстрима (``urt``) путей enrollment через nginx стенда для гейта Н-4 (001.25).

Запуск из ``control-plane`` с переменными набора тестов (``skills/vm-deploy`` §4: ``PG_DSN`` и
``REDIS_URL`` — туннелем ``deploy/scripts/stand-tunnel.sh``; адрес nginx стенда — ``STAND_HOST``
или ``ssh -G vm``)::

    .venv/bin/python -m tests.stand.enrollment_timing [--order interleaved|blocks]
        [--tokens ahead|before-accept]

Пять путей ``enrollment_cpu`` по 100 запросов с шагом 1,1 с: предел прокси — 1 r/s с адреса, и
очередь ``limit_req`` не должна входить в замер. Порядок: ``interleaved`` (по умолчанию) — пути по
кругу, фон VM (чужие проекты) распределяется между ними без систематического перекоса, но не
поровну; ``blocks`` — серия за серией. Токены: ``ahead`` (по умолчанию) — выпущены заранее через
панель, и запрос приёма не идёт сразу за вызовом панели, будящим базу; ``before-accept`` — токен
выпускается прямо перед приёмом. Режимы серий раунда 1 001.25 — ``blocks`` + ``before-accept``,
``interleaved`` + ``before-accept``, ``interleaved`` + ``ahead``; раунд 1 мерил три первых пути, а
скрипт всегда шлёт все пять — повтор раунда 1 точен по порядку и выдаче токенов, но не по набору
путей (два новых пути занимают свою долю шагов). Раунд 2 — настройки по умолчанию. Метка пути —
в ``User-Agent``; ``urt`` берётся из журнала nginx (``docker logs`` через ``ssh vm``, только
чтение). Enrollment-server проверяется якорем из ответа выдачи токена — так, как это обязан делать
агент (security.md §7.1).

``urt`` на VM — мера среды, а не пути: контрольный путь (``junk``, отказ по форме, код тот же, что
мерила 001.33 на тихом стенде — ``MEASURED_ENROLL_JUNK_P95_MS``) в 001.25 стоил 10…35 мс против 2.
Скрипт печатает и сырой p95, и p95, приведённый контролем своей серии (``p95(путь) ×
MEASURED_ENROLL_JUNK_P95_MS / p95(junk)``). Приведённое — сверка, а не граница сверху: при
аддитивном фоне оно занижает дорогой путь. Консервативную оценку гейт берёт из замера в процессе
(``enrollment_cpu``) — ``tests/unit/test_proxy_contract.py``. Уборка — по префиксу ``t25-urt-`` в
начале и в конце, сессия администратора отзывается и при сбое."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import os
import re
import secrets
import ssl
import statistics
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
import httpx
from app.security.sessions import SessionStore
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from redis import asyncio as redis_async
from tests.stand.enrollment_cpu import (
    PATHS,
    RSA_BITS,
    csr,
    enroll_body,
    junk_body,
    p95,
    widest_csr,
)
from tests.unit.test_proxy_contract import MEASURED_ENROLL_JUNK_P95_MS

PREFIX = "t25-urt-"
ROUNDS = 100
STEP_S = 1.1
NGINX = "control-plane-nginx-1"
NODE_REFERENCES = (
    "bootstrap_tokens",
    "node_identities",
    "node_ip_history",
    "node_billing_assignments",
)
URT = re.compile(r'"(?P<label>t25-urt-[0-9a-f]{8}-\w+)" rt=\S+ urt=(?P<urt>[\d.]+)$')


def stand_host() -> str:
    """Адрес nginx стенда (443, 9443, 9444): ``STAND_HOST`` или ``hostname`` из ``ssh -G vm``.
    База и Redis стенда — только на 127.0.0.1 VM, тесты ходят к ним туннелем
    (``deploy/scripts/stand-tunnel.sh``), и хост ``PG_DSN`` — не адрес nginx."""
    host = os.environ.get("STAND_HOST") or next(
        (
            line.split()[1]
            for line in subprocess.run(  # noqa: S603 — фиксированная команда только на чтение
                ["ssh", "-G", "vm"],  # noqa: S607
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            if line.startswith("hostname ")
        ),
        "",
    )
    assert host, "нет адреса стенда: STAND_HOST или ssh -G vm"
    return host


def unverified() -> httpx.AsyncClient:
    """Панель стенда — самоподписанный dev-сертификат; замер смотрит на время апстрима, а не на
    цепочку панели."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return httpx.AsyncClient(verify=context, trust_env=False, timeout=30)


def anchored(ca_pem: str) -> httpx.AsyncClient:
    """Enrollment-server — по якорю из ответа выдачи токена; dev CA стенда без ``keyUsage`` —
    снят только ``VERIFY_X509_STRICT`` (находка 001.25 для 001.66)."""
    context = ssl.create_default_context(cadata=ca_pem)
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return httpx.AsyncClient(verify=context, trust_env=False, timeout=30)


async def cleanup(conn: Any) -> None:
    like = f"{PREFIX}%"
    # Всё, что ссылается на ноду без каскада, — до самой ноды (как tests/e2e/_catalog.py).
    for table in NODE_REFERENCES:
        await conn.execute(
            f"delete from {table} where node_id in (select id from nodes where code like $1)",  # noqa: S608 — имя из списка
            like,
        )
    await conn.execute("delete from nodes where code like $1", like)
    await conn.execute("delete from billing_groups where name like $1", like)
    await conn.execute("delete from admin_users where email like $1", like)


def nginx_urt(since: str, run: str) -> dict[str, list[float]]:
    """``urt`` запросов этого прогона из журнала nginx, мс, по путям."""
    remote = f"docker logs --since {since} {NGINX} 2>/dev/null"
    log = subprocess.run(  # noqa: S603 — фиксированная команда только на чтение
        ["ssh", "vm", remote],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found: dict[str, list[float]] = {path: [] for path in PATHS}
    for line in log.splitlines():
        match = URT.search(line)
        if match and match["label"].startswith(f"{PREFIX}{run}-"):
            found[match["label"].removeprefix(f"{PREFIX}{run}-")].append(float(match["urt"]) * 1000)
    return found


async def _ready(body: bytes) -> bytes:
    return body


async def _body(token: Awaitable[str], csr_pem: str) -> bytes:
    return enroll_body(await token, csr_pem)


async def main(order: str, tokens_mode: str) -> None:
    host = stand_host()
    run = uuid.uuid4().hex[:8]
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_BITS)
    wide_rsa = widest_csr(rsa_key)
    wide_p256 = widest_csr(ec.generate_private_key(ec.SECP256R1()))
    statuses: dict[str, dict[int, int]] = {path: {} for path in PATHS}
    # Уборка, отзыв сессии и закрытие подключений — через стек выхода: каждое выполняется и при
    # сбое замера, и при сбое соседнего (роаст 001.25, раунды 2–3).
    async with contextlib.AsyncExitStack() as stack:
        conn = await asyncpg.connect(os.environ["PG_DSN"])
        stack.push_async_callback(conn.close)
        redis = redis_async.from_url(os.environ["REDIS_URL"])
        stack.push_async_callback(redis.aclose)
        store = SessionStore(redis)
        await cleanup(conn)
        stack.push_async_callback(cleanup, conn)
        admin_id = await conn.fetchval(
            "insert into admin_users (email, password_hash, role) values ($1, 'x', 'admin') "
            "returning id",
            f"{PREFIX}admin@example.test",
        )
        billing = await conn.fetchval(
            "insert into billing_groups (name) values ($1) returning id", f"{PREFIX}bg"
        )
        session = await store.create("admin", str(admin_id), "203.0.113.200", "timing", 3600)
        stack.push_async_callback(store.revoke_all, str(admin_id))
        admin = {"X-CSRF-Token": session.csrf, "Cookie": f"sid={session.id}"}
        async with unverified() as panel:

            async def issue(node_id: str) -> dict[str, Any]:
                issued = await panel.post(
                    f"https://{host}/api/v1/admin/nodes/{node_id}/bootstrap-token", headers=admin
                )
                assert issued.status_code == 201, issued.text
                body: dict[str, Any] = issued.json()
                return body

            nodes = []
            for i in range(2 * ROUNDS + 1):
                created = await panel.post(
                    f"https://{host}/api/v1/admin/nodes",
                    headers=admin,
                    json={
                        "code": f"{PREFIX}{i:03d}",
                        "name": "m",
                        "country": "JP",
                        "city": "Tokyo",
                        "provider": "stand",
                        "public_ipv4": "203.0.113.10",
                        "billing_group_id": str(billing),
                        "bandwidth_mbps": 1000,
                        "max_conn_per_ip": 32,
                    },
                )
                assert created.status_code == 201, created.text
                nodes.append(created.json()["id"])
            # Отказ CA токен не гасит: один токен на всю серию отказов. Его ответ — и якорь 9444.
            refusal = await issue(nodes[-1])
            ahead = (
                [(await issue(node))["token"] for node in nodes[:-1]]
                if tokens_mode == "ahead"
                else []
            )

            async def token_for(index: int) -> str:
                if ahead:
                    return str(ahead[index])
                return str((await issue(nodes[index]))["token"])

            junk = junk_body()
            bodies: dict[str, Callable[[int], Awaitable[bytes]]] = {
                "junk": lambda _: _ready(junk),
                "token": lambda _: _ready(enroll_body(secrets.token_urlsafe(32), csr())),
                "accept": lambda i: _body(token_for(i), csr()),
                "accept_wide": lambda i: _body(token_for(ROUNDS + i), wide_p256),
                "refusal_wide": lambda _: _ready(enroll_body(refusal["token"], wide_rsa)),
            }
            await asyncio.sleep(3)
            # Часы VM и Mac могут расходиться: минута запаса, чужие строки отсекает метка прогона.
            since = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            schedule = (
                [(i, path) for i in range(ROUNDS) for path in PATHS]
                if order == "interleaved"
                else [(i, path) for path in PATHS for i in range(ROUNDS)]
            )
            async with anchored(refusal["ca_pem"]) as enroll:
                for i, path in schedule:
                    started = time.monotonic()
                    answer = await enroll.post(
                        f"https://{host}:9444/agent/v1/enroll",
                        content=await bodies[path](i),
                        headers={
                            "Content-Type": "application/json",
                            "User-Agent": f"{PREFIX}{run}-{path}",
                        },
                    )
                    statuses[path][answer.status_code] = (
                        statuses[path].get(answer.status_code, 0) + 1
                    )
                    await asyncio.sleep(max(0.0, STEP_S - (time.monotonic() - started)))
    urt = nginx_urt(since, run)
    control = p95(urt["junk"])
    print(
        f"прогон {run} ({order}, токены {tokens_mode}); контроль junk p95 {control:.0f} мс "
        f"(тихий стенд 001.33 — {MEASURED_ENROLL_JUNK_P95_MS} мс)"
    )
    for path in PATHS:
        values = urt[path]
        assert len(values) == ROUNDS, (path, len(values), "строки журнала потеряны")
        normalized = p95(values) * MEASURED_ENROLL_JUNK_P95_MS / control
        print(
            f"{path}: статусы {statuses[path]}; urt p50 {statistics.median(values):.0f} p95 "
            f"{p95(values):.0f} max {max(values):.0f} мс; приведённый контролем p95 "
            f"{normalized:.2f} мс"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="urt путей enrollment через nginx стенда")
    parser.add_argument("--order", choices=("interleaved", "blocks"), default="interleaved")
    parser.add_argument("--tokens", choices=("ahead", "before-accept"), default="ahead")
    arguments = parser.parse_args()
    asyncio.run(main(arguments.order, arguments.tokens))
