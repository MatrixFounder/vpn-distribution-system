"""Живая проба enrollment и identity через nginx стенда (001.25): панель (443) → токен и якорь
доверия (сертификат CA) → enrollment (9444), проверенный этим якорем, как это обязан делать агент →
раздел агента (9443) с выданным листом; подмена ``X-Client-Cert`` клиентом; привязка
подтверждения к сверенной identity; серийный номер листа в форме OpenSSL стенда; близнец листа по
податливости ECDSA (nginx его принимает, C-01 — нет, слот отчётов у близнецов общий: зоны прокси —
по серийному номеру); отзыв; отказы Node API прокси — под его пределами (тело без длины, 404
enrollment на агентском порту и пути раздела без косой черты, тело длиннее предела, служебные
пути отказов 411 и 413, путь с управляющими символами — и на публичном server; 404 — от самого
nginx, по телу ответа); Н-25 — метки в пути /s/ нет ни в журнале nginx на переведённых запросах
(косая %2F) обоих портов Node API и на запросах, отвергнутых до разбора URI (505, 414, 408), ни в
журнале api, а запрос, не попавший в /s/, в журнал ложится (фрагмент «#/s/», путь, нормализованный
в служебный).

Запуск из ``control-plane`` с переменными набора тестов (``skills/vm-deploy`` §4: ``PG_DSN`` и
``REDIS_URL`` — туннелем ``deploy/scripts/stand-tunnel.sh``; адрес nginx стенда — ``STAND_HOST``
или ``ssh -G vm``)::

    .venv/bin/python -m tests.stand.enrollment_probe

Секретов стенда не касается: CA берётся из ответа enrollment, ключ ноды создаётся здесь, сессия
администратора — прямо в Redis (как ``tests/e2e/_admin.py``). Уборка — по префиксу ``t25-live-``
в начале и в конце. Код выхода 1 — хотя бы одна проверка не сошлась."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import httpx
from app.security.ca import InternalCA
from app.security.sessions import SessionStore
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from redis import asyncio as redis_async
from tests._pki import escaped, make_ca, signature_twin
from tests._reports import VALID_REPORT

PREFIX = "t25-live-"
AGENT_VERSION = "0.1.0"
NODE_REFERENCES = (
    "bootstrap_tokens",
    "node_identities",
    "node_ip_history",
    "node_billing_assignments",
)
STATE = {"config_version": 0, "users_seq": 0, "generation": 1}


class Probe:
    def __init__(self) -> None:
        self.results: list[tuple[str, object, object]] = []

    def check(self, label: str, got: object, want: object) -> None:
        self.results.append((label, got, want))
        verdict = "OK  " if got == want else "FAIL"
        print(verdict, label, "→", got, "" if got == want else f"(ждали {want})", flush=True)


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


def own_address_seen_by_the_stand() -> str:
    """Адрес этой машины со стороны VM — из ``SSH_CLIENT`` сеанса ``ssh vm`` (тот же интерфейс,
    через который идут запросы к nginx)."""
    seen = subprocess.run(  # noqa: S603 — фиксированная команда только на чтение
        ["ssh", "vm", "echo $SSH_CLIENT"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    return str(ipaddress.ip_address(seen))


def stand_log(container: str, since: str = "5m") -> str:
    """Журнал контейнера стенда за последние ``since`` (пять минут), stdout и stderr вместе
    (access.log nginx — stdout, error.log — stderr), только чтение. Метки в пробе уникальны —
    окно их не путает."""
    return subprocess.run(  # noqa: S603 — фиксированная команда только на чтение
        ["ssh", "vm", f"docker logs --since {since} {container} 2>&1"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout


# 404 отказа прокси на server Node API — именованный location @not_found; 404 приложения — «Not
# Found»: по телу видно, какой слой ответил (роаст 001.25, раунд 6).
PROXY_NOT_FOUND = "нет такого пути"


def refused_by(responses: list[httpx.Response]) -> set[str]:
    """Кто ответил 404: сообщение единого формата, ``nginx`` — страница nginx без JSON."""
    layers = set()
    for response in responses:
        if response.status_code != 404:
            continue
        if "<center>nginx</center>" in response.text:
            layers.add("nginx")
        else:
            layers.add(response.json()["error"]["message"])
    return layers


def unverified() -> ssl.SSLContext:
    """Публичный server стенда — самоподписанный dev-сертификат; проба смотрит на поведение
    приложения за прокси, а не на цепочку панели."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def raw_exchange(host: str, payload: bytes) -> str:
    """Сырой обмен с публичным server (443) без httpx: строка запроса, которую nginx отвергает до
    разбора URI (версия HTTP/2.0 поверх HTTP/1, строка длиннее буфера, строка без конца). Ответ —
    код из строки статуса или то, чем кончилось соединение (408 nginx закрывает сбросом без
    ответа: ``reset_timedout_connection``)."""
    with (
        socket.create_connection((host, 443), timeout=20) as sock,
        unverified().wrap_socket(sock, server_hostname="localhost") as tls,
    ):
        tls.sendall(payload)
        try:
            first = tls.recv(200).split(b"\r\n")[0].decode(errors="replace")
        except OSError as exc:
            return type(exc).__name__
    return first.split()[1] if first.startswith("HTTP/") else first or "закрыто"


def anchored(ca_pem: str) -> ssl.SSLContext:
    """TLS к enrollment-server, проверенный якорем из ответа выдачи токена (security.md §7.1):
    агент не отдаёт токен серверу, которого этот CA не подписал. dev CA стенда без ``keyUsage`` —
    снят только флаг ``VERIFY_X509_STRICT`` (находка 001.25 для 001.66), имя и цепочка
    проверяются."""
    context = ssl.create_default_context(cadata=ca_pem)
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def openssl_serial(cert_pem: str) -> str:
    """Серийный номер листа так, как его печатает OpenSSL стенда (той же библиотекой nginx
    получает ``$ssl_client_serial``)."""
    printed = subprocess.run(  # noqa: S603 — фиксированная команда только на чтение
        ["ssh", "vm", "openssl x509 -noout -serial"],  # noqa: S607
        input=cert_pem,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return printed.removeprefix("serial=")


def csr_and_key() -> tuple[str, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "live-probe")]))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode(), key_pem


def agent_context(cert_pem: str, key_pem: bytes, ca_pem: str) -> ssl.SSLContext:
    """mTLS ноды: лист и ключ — клиенту, сервер 9443 сверяется по ``ca_pem`` из ответа
    enrollment. dev CA стенда (``openssl req -x509`` из ``dev-secrets.sh``) без ``keyUsage``:
    строгий режим Python 3.13+ его отвергает (RFC 5280 требует ``keyCertSign`` у CA) — находка
    001.25 для 001.66; снимается только этот флаг, проверка цепочки остаётся."""
    context = ssl.create_default_context(cadata=ca_pem)
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    with tempfile.TemporaryDirectory() as folder:
        cert_file, key_file = Path(folder) / "cert.pem", Path(folder) / "key.pem"
        cert_file.write_text(cert_pem)
        key_file.write_bytes(key_pem)
        context.load_cert_chain(str(cert_file), str(key_file))
    return context


async def cleanup(conn: Any) -> None:
    like = f"{PREFIX}%"
    for table in NODE_REFERENCES:
        await conn.execute(
            f"delete from {table} where node_id in (select id from nodes where code like $1)",  # noqa: S608 — имя из списка
            like,
        )
    await conn.execute("delete from nodes where code like $1", like)
    await conn.execute("delete from billing_groups where name like $1", like)
    await conn.execute("delete from access_groups where name like $1", like)
    await conn.execute("delete from admin_users where email like $1", like)


@contextlib.asynccontextmanager
async def report_slot_held(
    host: str, context: ssl.SSLContext, headers: dict[str, str]
) -> AsyncIterator[None]:
    """Отчёт, чьё тело заливается и не дозаливается: nginx держит слот ``agent_report_conn``
    сертификата с фазы preaccess до конца тела (до ``client_body_timeout``, 10 с)."""
    _, writer = await asyncio.open_connection(host, 9443, ssl=context, server_hostname=host)
    head = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    writer.write(
        (
            f"POST /agent/v1/reports HTTP/1.1\r\nHost: {host}:9443\r\n"
            f"Content-Type: application/json\r\nContent-Length: 100000\r\n{head}\r\n{{"
        ).encode()
    )
    await writer.drain()
    # Частота отчётов сертификата — 2 r/s без nodelay: запрос мог встать в очередь limit_req
    # (он проверяется раньше limit_conn) — слот занят только после неё.
    await asyncio.sleep(1.5)
    try:
        yield
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, ssl.SSLError):
            await writer.wait_closed()


async def main() -> None:
    host = stand_host()
    probe = Probe()
    # Уборка, отзыв сессии и закрытие клиентов и подключений — через стек выхода: каждое
    # выполняется и при сбое пробы, и при сбое соседнего (роаст 001.25, раунды 2–3): строку
    # администратора уборка удаляет, а сессию в Redis проверяет только Redis.
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
        access = await conn.fetchval(
            "insert into access_groups (name) values ($1) returning id", f"{PREFIX}ag"
        )
        session = await store.create("admin", str(admin_id), "203.0.113.200", "live-probe", 600)
        stack.push_async_callback(store.revoke_all, str(admin_id))
        admin = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=f"https://{host}",
                verify=unverified(),
                trust_env=False,
                headers={"X-CSRF-Token": session.csrf, "Cookie": f"sid={session.id}"},
            )
        )
        anchors: set[str] = set()
        public = await stack.enter_async_context(
            httpx.AsyncClient(base_url=f"https://{host}", verify=unverified(), trust_env=False)
        )

        async def new_node(label: str) -> str:
            created = await admin.post(
                "/api/v1/admin/nodes",
                json={
                    "code": f"{PREFIX}{label}-{uuid.uuid4().hex[:6]}",
                    "name": label,
                    "country": "JP",
                    "city": "Tokyo",
                    "provider": "probe",
                    "public_ipv4": "203.0.113.10",
                    "billing_group_id": str(billing),
                    "access_group_ids": [str(access)],
                    "bandwidth_mbps": 1000,
                    "max_conn_per_ip": 32,
                },
            )
            assert created.status_code == 201, created.text
            return str(created.json()["id"])

        async def enroll(node_id: str) -> tuple[dict[str, Any], bytes]:
            issued = (await admin.post(f"/api/v1/admin/nodes/{node_id}/bootstrap-token")).json()
            anchors.add(issued["ca_pem"])
            csr, key_pem = csr_and_key()
            # Enrollment-server — только через TLS, проверенный якорем из команды: сервер, которого
            # этот CA не подписал, токена не получит (рукопожатие упало бы раньше запроса).
            async with httpx.AsyncClient(
                base_url=f"https://{host}:9444", verify=anchored(issued["ca_pem"]), trust_env=False
            ) as enrollment:
                answer = await enrollment.post(
                    "/agent/v1/enroll",
                    json={
                        "bootstrap_token": issued["token"],
                        "csr_pem": csr,
                        "agent_version": AGENT_VERSION,
                        "xray_version": "26.9.1",
                    },
                )
            assert answer.status_code == 200, answer.text
            return answer.json(), key_pem

        first_id, second_id = await new_node("n1"), await new_node("n2")
        first, first_key = await enroll(first_id)
        second, second_key = await enroll(second_id)
        leaf = x509.load_pem_x509_certificate(first["client_cert_pem"].encode())
        twin_pem = signature_twin(leaf).public_bytes(serialization.Encoding.PEM).decode()
        fingerprint = InternalCA.fingerprint(first["client_cert_pem"])
        state = (await admin.get(f"/api/v1/admin/nodes/{first_id}/state")).json()
        probe.check(
            "identity.enrolled_from — адрес клиента, каким его видит nginx (не адрес прокси)",
            state["identity"]["enrolled_from"],
            own_address_seen_by_the_stand(),
        )
        probe.check(
            "отпечаток в состоянии — SHA-256 выданного листа",
            state["identity"]["cert_fingerprint"],
            fingerprint,
        )
        probe.check(
            "enrollment-server проверен якорем из ответа выдачи токена; CA ответа обмена — тот же",
            (len(anchors), {first["ca_pem"], second["ca_pem"]} == anchors),
            (1, True),
        )
        probe.check(
            "серийный номер в состоянии — как его печатает OpenSSL стенда ($ssl_client_serial)",
            state["identity"]["cert_serial"],
            openssl_serial(first["client_cert_pem"]),
        )
        # Отрицательная сторона якоря: с чужим CA клиент рвёт рукопожатие до запроса — токен не
        # уходит серверу, которого якорь не подтверждает.
        foreign = make_ca("foreign CA")[1].decode()
        try:
            async with httpx.AsyncClient(
                base_url=f"https://{host}:9444", verify=anchored(foreign), trust_env=False
            ) as stranger:
                await stranger.post("/agent/v1/enroll", json={"bootstrap_token": "x" * 43})
            refused_by_client = False
        except httpx.ConnectError as exc:
            refused_by_client = isinstance(exc.__cause__, ssl.SSLCertVerificationError) or (
                "CERTIFICATE_VERIFY_FAILED" in str(exc)
            )
        probe.check(
            "якорь другого CA: клиент отвергает сертификат enrollment-server до отправки токена",
            refused_by_client,
            True,
        )
        one = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=f"https://{host}:9443",
                verify=agent_context(first["client_cert_pem"], first_key, first["ca_pem"]),
                trust_env=False,
                timeout=30,  # залпы ниже ждут в очереди limit_req (5 r/s, очередь 20) до 4 с
            )
        )
        two = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=f"https://{host}:9443",
                verify=agent_context(second["client_cert_pem"], second_key, second["ca_pem"]),
                trust_env=False,
            )
        )
        twin = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=f"https://{host}:9443",
                verify=agent_context(twin_pem, first_key, first["ca_pem"]),
                trust_env=False,
            )
        )
        hdr1 = {"X-Agent-Version": AGENT_VERSION, "X-Node-Identity": first["identity_token"]}
        hdr2 = {"X-Agent-Version": AGENT_VERSION, "X-Node-Identity": second["identity_token"]}
        # Рукопожатие само — проверка: клиент сверяет серверный сертификат 9443 по ca_pem из
        # ответа enrollment, nginx — лист ноды по своему ca.crt (глубина 0: лист, подписанный CA
        # напрямую).
        r = await one.get("/agent/v1/state", params=STATE, headers=hdr1)
        probe.check(
            "лист нашего CA через 9443, нода в pending → 403 node_not_approved",
            (r.status_code, r.json()["error"]["code"]),
            (403, "node_not_approved"),
        )
        spoof = {**hdr1, "X-Client-Cert": escaped(second["client_cert_pem"])}
        r = await one.get("/agent/v1/state", params=STATE, headers=spoof)
        probe.check(
            "клиент подставил X-Client-Cert чужой ноды → прокси перезаписал: своя нода (403)",
            r.status_code,
            403,
        )
        r = await one.get(
            "/agent/v1/state",
            params=STATE,
            headers={**hdr2, "X-Client-Cert": escaped(second["client_cert_pem"])},
        )
        probe.check(
            "свой сертификат в TLS + токен и сертификат второй ноды в заголовках → 401",
            (r.status_code, r.json()["error"]["code"]),
            (401, "unauthenticated"),
        )
        r = await two.get("/agent/v1/state", params=STATE, headers=hdr1)
        probe.check("сертификат второй ноды + токен первой → 401", r.status_code, 401)
        r = await admin.post(
            f"/api/v1/admin/nodes/{first_id}/approve", json={"cert_fingerprint": "0" * 64}
        )
        probe.check(
            "подтверждение не той identity, что действует → 409 identity_changed",
            (r.status_code, r.json()["error"]["code"]),
            (409, "identity_changed"),
        )
        r = await admin.post(
            f"/api/v1/admin/nodes/{first_id}/approve",
            json={"cert_fingerprint": state["identity"]["cert_fingerprint"]},
        )
        probe.check(
            "подтверждение сверенной identity → provisioning",
            (r.status_code, r.json()["status"]),
            (200, "provisioning"),
        )
        r = await one.get("/agent/v1/state", params=STATE, headers=hdr1)
        probe.check("после подтверждения раздел отвечает ноде", r.status_code, 200)
        r = await one.post(
            "/agent/v1/reports", json={**VALID_REPORT, "node_id": second_id}, headers=hdr1
        )
        probe.check(
            "отчёт с чужим node_id → node_mismatch с идентификатором из identity",
            (r.status_code, r.json()["error"]["details"].get("node_id")),
            (403, first_id),
        )
        r = await twin.get("/agent/v1/state", params=STATE, headers=hdr1)
        probe.check(
            "близнец листа (r, n−s): nginx его принимает, C-01 identity не находит → 401",
            (r.status_code, r.json()["error"]["code"]),
            (401, "unauthenticated"),
        )
        async with report_slot_held(
            host,
            agent_context(first["client_cert_pem"], first_key, first["ca_pem"]),
            hdr1,
        ):
            report = {**VALID_REPORT, "node_id": first_id}
            r = await twin.post("/agent/v1/reports", json=report, headers=hdr1)
            probe.check(
                "лист заливает отчёт — близнец с тем же серийным номером получает 429 "
                "(слот отчётов у близнецов общий)",
                r.status_code,
                429,
            )
            r = await two.post(
                "/agent/v1/reports", json={**VALID_REPORT, "node_id": second_id}, headers=hdr2
            )
            probe.check(
                "в то же время сертификат другой ноды слот получает (не 429)",
                r.status_code != 429,
                True,
            )
        r = await public.get(
            "/agent/v1/state",
            params=STATE,
            headers={**hdr1, "X-Client-Cert": escaped(first["client_cert_pem"])},
        )
        probe.check(
            "публичный server: /agent/ закрыт даже с подставленным X-Client-Cert",
            r.status_code,
            404,
        )
        async with httpx.AsyncClient(
            base_url=f"https://{host}:9444", verify=anchored(next(iter(anchors))), trust_env=False
        ) as enrollment:
            r = await enrollment.get("/agent/v1/state", params=STATE, headers=hdr1)
        probe.check("enrollment-server: иной путь — 404", r.status_code, 404)
        r = await admin.post(f"/api/v1/admin/nodes/{first_id}/revoke-identity")
        probe.check("отзыв → disabled", (r.status_code, r.json()["status"]), (200, "disabled"))
        r = await one.get("/agent/v1/state", params=STATE, headers=hdr1)
        probe.check(
            "следующий же запрос после отзыва → 401 (TLS nginx пропускает: ssl_crl — 001.66)",
            (r.status_code, r.json()["error"]["code"]),
            (401, "unauthenticated"),
        )
        stored = await conn.fetchval(
            "select count(*) from node_identities where cert_fingerprint = $1", fingerprint
        )
        probe.check("отпечаток SHA-256 выданного листа записан в node_identities", stored, 1)
        # Отказы Node API — после пределов прокси (роаст 001.25, раунд 3: `return` в фазе rewrite
        # давал 30 × 411 и 404 без единого 429). Залпы — последними: они выбирают пределы адреса
        # и сертификата первой ноды.

        async def without_length() -> AsyncIterator[bytes]:
            yield b"{}"

        async with httpx.AsyncClient(
            base_url=f"https://{host}:9444",
            verify=anchored(next(iter(anchors))),
            trust_env=False,
            # Очередь limit_req без nodelay задерживает запросы в пределах burst (1 r/s, очередь 5)
            # — до 5 с; таймаут httpx по умолчанию (5 с) принял бы задержку за сбой.
            timeout=30,
        ) as flood:
            burst = await asyncio.gather(
                *(
                    flood.post(
                        "/agent/v1/enroll",
                        content=without_length(),
                        headers={"Content-Type": "application/json"},
                    )
                    for _ in range(30)
                )
            )
        codes = Counter(r.status_code for r in burst)
        probe.check(
            f"9444, 30 тел без длины разом: 411 под пределами (коды {dict(codes)})",
            (set(codes) <= {411, 429}, codes[411] > 0, codes[429] > 0),
            (True, True, True),
        )
        burst = await asyncio.gather(
            *(one.get("/agent/v1/enroll", headers=hdr1) for _ in range(40))
        )
        codes = Counter(r.status_code for r in burst)
        probe.check(
            f"9443, 40 запросов enrollment разом: 404 под пределами (коды {dict(codes)})",
            (set(codes) <= {404, 429}, codes[404] > 0, codes[429] > 0),
            (True, True, True),
        )
        # Служебный путь отказа 411 — не `internal`: внешний запрос к internal location nginx
        # отвергал 404 в фазе FIND_CONFIG, до пределов (роаст 001.25, раунд 4: 30 × 404 без 429).
        # Теперь он проходит пределы и получает 404 единого формата. Пауза — пока вёдра пределов
        # после залпов выше наполнятся (адрес: 1 r/s с очередью 5; сертификат: 5 r/s с очередью 20).
        await asyncio.sleep(12)
        async with httpx.AsyncClient(
            base_url=f"https://{host}:9444",
            verify=anchored(next(iter(anchors))),
            trust_env=False,
            # Очередь limit_req без nodelay задерживает запросы в пределах burst (1 r/s, очередь 5)
            # — до 5 с; таймаут httpx по умолчанию (5 с) принял бы задержку за сбой.
            timeout=30,
        ) as flood:
            burst = await asyncio.gather(*(flood.get("/__length_required") for _ in range(30)))
        codes = Counter(r.status_code for r in burst)
        not_found = {r.json()["error"]["code"] for r in burst if r.status_code == 404}
        probe.check(
            f"9444, 30 запросов на служебный путь 411 разом: 404 под пределами "
            f"(коды {dict(codes)})",
            (set(codes) <= {404, 429}, codes[404] > 0, codes[429] > 0, not_found),
            (True, True, True, {"not_found"}),
        )
        burst = await asyncio.gather(
            *(one.get("/__length_required", headers=hdr1) for _ in range(40))
        )
        codes = Counter(r.status_code for r in burst)
        not_found = {r.json()["error"]["code"] for r in burst if r.status_code == 404}
        probe.check(
            f"9443, 40 запросов на служебный путь 411 разом: 404 под пределами "
            f"(коды {dict(codes)})",
            (set(codes) <= {404, 429}, codes[404] > 0, codes[429] > 0, not_found),
            (True, True, True, {"not_found"}),
        )
        # Путь раздела без косой черты: prefix-location `^~ /agent/v1/` с proxy_pass получает у
        # nginx auto_redirect — 301 в FIND_CONFIG, до пределов (роаст 001.25, раунд 4: 40 × 301
        # без 429). Точный сосед отвечает 404 после пределов.
        await asyncio.sleep(6)
        burst = await asyncio.gather(*(one.get("/agent/v1", headers=hdr1) for _ in range(40)))
        codes = Counter(r.status_code for r in burst)
        probe.check(
            f"9443, 40 запросов на /agent/v1 разом: 404 под пределами, не 301 (коды {dict(codes)})",
            (set(codes) <= {404, 429}, codes[404] > 0, codes[429] > 0),
            (True, True, True),
        )
        # Тело длиннее предела location: nginx отвергал его 413 в FIND_CONFIG — до пределов, со
        # строкой error.log на каждый (роаст 001.25, раунд 4: 30 × 413 без 429). Теперь карта
        # длины переводит его в служебный location, где 413 даёт try_files после пределов.
        oversized = b"x" * (64 * 1024 + 1)
        await asyncio.sleep(12)
        async with httpx.AsyncClient(
            base_url=f"https://{host}:9444",
            verify=anchored(next(iter(anchors))),
            trust_env=False,
            timeout=30,
        ) as flood:
            burst = await asyncio.gather(
                *(
                    flood.post(
                        "/agent/v1/enroll",
                        content=oversized,
                        headers={"Content-Type": "application/json"},
                    )
                    for _ in range(30)
                )
            )
            codes = Counter(r.status_code for r in burst)
            probe.check(
                f"9444, 30 тел длиннее 64 КиБ разом: 413 под пределами (коды {dict(codes)})",
                (set(codes) <= {413, 429}, codes[413] > 0, codes[429] > 0),
                (True, True, True),
            )
            await asyncio.sleep(12)
            burst = await asyncio.gather(*(flood.get("/__too_large") for _ in range(30)))
            codes = Counter(r.status_code for r in burst)
            not_found = {r.json()["error"]["code"] for r in burst if r.status_code == 404}
            probe.check(
                f"9444, 30 запросов на служебный путь 413 разом: 404 под пределами "
                f"(коды {dict(codes)})",
                (set(codes) <= {404, 429}, codes[404] > 0, codes[429] > 0, not_found),
                (True, True, True, {"not_found"}),
            )
        await asyncio.sleep(6)
        burst = await asyncio.gather(
            *(
                one.post(
                    "/agent/v1/heartbeat",
                    content=oversized,
                    headers={**hdr1, "Content-Type": "application/json"},
                )
                for _ in range(40)
            )
        )
        codes = Counter(r.status_code for r in burst)
        probe.check(
            f"9443, 40 тел длиннее 64 КиБ разом: 413 под пределами (коды {dict(codes)})",
            (set(codes) <= {413, 429}, codes[413] > 0, codes[429] > 0),
            (True, True, True),
        )
        # Управляющие символы в пути: nginx декодирует %0A в $uri, точные location такой путь не
        # выбирают, а маршрут приложения (регулярное выражение с $) — выбирает (роаст 001.25,
        # раунд 5: /metrics%0A — 200 с метриками, enroll%0A на 9443 — обработчик обмена). Теперь
        # первое правило server — 404 (на server Node API — после пределов). Ответ сверяется и по
        # телу: 404 обязан дать сам nginx, а не промежуточный слой приложения за ним (иначе проба
        # не отличила бы снятое правило прокси — роаст 001.25, раунд 6).
        burst = await asyncio.gather(*(public.get("/metrics%0A") for _ in range(30)))
        codes = Counter(r.status_code for r in burst)
        probe.check(
            f"публичный server, 30 × /metrics%0A: 404 от nginx, а не метрики (коды {dict(codes)})",
            (set(codes), refused_by(burst)),
            ({404}, {"nginx"}),
        )
        await asyncio.sleep(6)
        burst = await asyncio.gather(
            *(
                one.post("/agent/v1/enroll%0A", json={"bootstrap_token": "A" * 43}, headers=hdr1)
                for _ in range(40)
            )
        )
        codes = Counter(r.status_code for r in burst)
        probe.check(
            f"9443, 40 × POST /agent/v1/enroll%0A: 404 прокси под пределами, не обмен "
            f"(коды {dict(codes)})",
            (set(codes) <= {404, 429}, codes[404] > 0, codes[429] > 0, refused_by(burst)),
            (True, True, True, {PROXY_NOT_FOUND}),
        )
        await asyncio.sleep(6)
        burst = await asyncio.gather(
            *(
                one.post(
                    "/agent/v1/reports%0A",
                    content=oversized,
                    headers={**hdr1, "Content-Type": "application/json"},
                )
                for _ in range(40)
            )
        )
        codes = Counter(r.status_code for r in burst)
        probe.check(
            f"9443, 40 × отчёт 64 КиБ+ на /agent/v1/reports%0A: 404 прокси под пределами "
            f"(коды {dict(codes)})",
            (set(codes) <= {404, 429}, codes[404] > 0, codes[429] > 0, refused_by(burst)),
            (True, True, True, {PROXY_NOT_FOUND}),
        )
        await asyncio.sleep(12)
        async with httpx.AsyncClient(
            base_url=f"https://{host}:9444",
            verify=anchored(next(iter(anchors))),
            trust_env=False,
            timeout=30,
        ) as flood:
            burst = await asyncio.gather(
                *(flood.post("/agent/v1/enroll%0A", json={}) for _ in range(30))
            )
        codes = Counter(r.status_code for r in burst)
        probe.check(
            f"9444, 30 × POST /agent/v1/enroll%0A: 404 прокси под пределами (коды {dict(codes)})",
            (set(codes) <= {404, 429}, codes[404] > 0, codes[429] > 0, refused_by(burst)),
            (True, True, True, {PROXY_NOT_FOUND}),
        )
        # Н-25 на переведённых запросах: на 8443/8444 запрос, переведённый rewrite в служебный
        # location, несёт к журналу служебный $uri, и держит его только сырой ключ — в том числе
        # с косой %2F вокруг «s» (роаст 001.25, раунд 6: строка с токеном в журнале 9444).
        await asyncio.sleep(12)
        marks = [f"{PREFIX}h25-{uuid.uuid4().hex[:12]}" for _ in range(3)]
        async with httpx.AsyncClient(
            base_url=f"https://{host}:9444",
            verify=anchored(next(iter(anchors))),
            trust_env=False,
            timeout=30,
        ) as enrollment:
            rewritten = [
                await enrollment.get(f"/s%2F{marks[0]}%0A"),
                await enrollment.post(
                    f"/s%2F{marks[1]}",
                    content=without_length(),
                    headers={"Content-Type": "application/json"},
                ),
                await enrollment.post(
                    f"/s%2F{marks[2]}",
                    content=oversized,
                    headers={"Content-Type": "application/json"},
                ),
            ]
        await asyncio.sleep(2)
        nginx_log = stand_log("control-plane-nginx-1")
        codes_seen = [r.status_code for r in rewritten]
        probe.check(
            f"9444, /s%2F<метка> переведён в 404, 411 и 413: метки нет в журнале nginx "
            f"(коды {codes_seen})",
            (codes_seen, [mark in nginx_log for mark in marks]),
            ([404, 411, 413], [False, False, False]),
        )
        # Н-25 в журнале приложения: access-log uvicorn выключен — строка GET /s/<токен> в
        # журнал api не ложится (роаст 001.25, раунд 6: ложилась).
        mark = f"{PREFIX}app-{uuid.uuid4().hex[:12]}"
        subscription = await public.get(f"/s/{mark}")
        await asyncio.sleep(2)
        probe.check(
            f"публичный /s/<метка>: метки нет ни в журнале api, ни в журнале nginx "
            f"(код {subscription.status_code})",
            (
                subscription.status_code,
                mark in stand_log("control-plane-api-1"),
                mark in stand_log("control-plane-nginx-1"),
            ),
            (501, False, False),
        )
        # Н-25 на агентском порту — листом ноды: путь /s/<метка> уходит в location / (404 после
        # пределов), /s%2F<метка>%0A — переходом в служебный 404; журнал уровня server заменил бы
        # фильтр http (роаст 001.25, раунд 7: проверки на 9443 не было).
        await asyncio.sleep(12)
        agent_marks = [f"{PREFIX}h25a-{uuid.uuid4().hex[:12]}" for _ in range(2)]
        agent_answers = [
            await one.get(f"/s/{agent_marks[0]}", headers=hdr1),
            await one.get(f"/s%2F{agent_marks[1]}%0A", headers=hdr1),
        ]
        await asyncio.sleep(2)
        nginx_log = stand_log("control-plane-nginx-1")
        probe.check(
            f"9443, лист ноды: /s/<метка> и /s%2F<метка>%0A — метки нет в журнале nginx "
            f"(коды {[r.status_code for r in agent_answers]})",
            (
                [r.status_code for r in agent_answers],
                [mark in nginx_log for mark in agent_marks],
            ),
            ([404, 404], [False, False]),
        )
        # Журнал запросов не прячется путём: суффикс «#/s/» — фрагмент (nginx и httptools его
        # отбрасывают, маршрут срабатывает), и запрос обязан лечь в журнал, как любой (роаст
        # 001.25, раунд 8: сырой ключ Н-25 читался у всех запросов и прятал такой целиком). httpx
        # фрагмент не отправляет — цель запроса задаётся как есть.
        mark = f"{PREFIX}frag-{uuid.uuid4().hex[:12]}"
        fragment = await public.request(
            "GET", "/healthz", extensions={"target": f"/healthz#/s/{mark}".encode()}
        )
        await asyncio.sleep(2)
        probe.check(
            f"публичный server: GET /healthz#/s/<метка> — запрос в журнале nginx есть "
            f"(код {fragment.status_code})",
            (fragment.status_code, mark in stand_log("control-plane-nginx-1")),
            (200, True),
        )
        # Запрос, отвергнутый до разбора URI (пустой $uri): $request — сырые байты строки, и
        # строка журнала — форматом без строки запроса при любом статусе, а не только при 400
        # (роаст 001.25, раунд 9: 505, 414 и 408 писали /s/<токен>). 408 — после
        # client_header_timeout (5 с).
        unparsed_marks = [f"{PREFIX}unp-{uuid.uuid4().hex[:12]}" for _ in range(3)]
        answers = [
            raw_exchange(host, f"GET /s/{unparsed_marks[0]} HTTP/2.0\r\nHost: x\r\n\r\n".encode()),
            raw_exchange(
                host,
                f"GET /s/{unparsed_marks[1]}?{'a' * 9000} HTTP/1.1\r\nHost: x\r\n\r\n".encode(),
            ),
            raw_exchange(host, f"GET /s/{unparsed_marks[2]}".encode()),
        ]
        await asyncio.sleep(2)
        nginx_log = stand_log("control-plane-nginx-1")
        recent = stand_log("control-plane-nginx-1", since="60s")
        probe.check(
            f"публичный server: 505, 414 и 408 до разбора URI — метки нет в журнале nginx, "
            f"строки без разбора есть (ответы {answers})",
            (
                answers[:2],
                [mark in nginx_log for mark in unparsed_marks],
                [f'"<unparsed>" {code} ' in recent for code in ("505", "414", "408")],
            ),
            (["505", "414"], [False, False, False], [True, True, True]),
        )
        # Путь, который nginx нормализует в служебный, на публичном server — обычный запрос к
        # приложению: служебных путей у этого server нет, и сырой /s/ его из журнала не прячет
        # (роаст 001.25, раунд 9: карта служебных путей была общей для всех server).
        mark = f"{PREFIX}svc-{uuid.uuid4().hex[:12]}"
        normalized = await public.request(
            "GET", "/", extensions={"target": f"/s/{mark}/../../__not_found".encode()}
        )
        await asyncio.sleep(2)
        probe.check(
            f"публичный server: /s/<метка>/../../__not_found — запрос в журнале nginx есть "
            f"(код {normalized.status_code})",
            (normalized.status_code, mark in stand_log("control-plane-nginx-1")),
            (404, True),
        )
    failed = [result for result in probe.results if result[1] != result[2]]
    print(f"\nИТОГ: {len(probe.results) - len(failed)} из {len(probe.results)} проверок сошлись")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
