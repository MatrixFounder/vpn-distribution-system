"""Сквозные проверки задачи 001.06 (миграция 060 «парк нод, inbound, состояние и команды»).

TC-E2E-01: применение, откат ровно до 060 и повторное применение; откат снимает только объекты 060
и внешний ключ истории назначений, таблицы 050/040 остаются. TC-E2E-02: ограничения §4.2.3/§4.4
под ролью app_rw — UNIQUE (code, cert_fingerprint, node_id+profile, node_id+port,
user_id+node_id), CHECK (порт, пороги, коэффициент), NOT NULL billing_group_id, FK и каскады,
node_metrics без партиции. Все вставки — в откатываемой транзакции.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import subprocess
import uuid

import asyncpg
import psycopg
import pytest
from app.cli import migrate_dsn
from psycopg import sql

from . import _cli
from ._catalog import PREFIX
from ._cli import (
    IDENTITY_MIGRATION,
    clear_test_identities,
    rollback_through,
    run_cli,
    start_cli,
)
from ._db import existing_tables, insert_node
from ._spec import CATALOG_TABLES, IDENTITY_TABLES, NODES_TABLES

PROBE_TS = dt.datetime(2000, 1, 1, 12, tzinfo=dt.UTC)  # вне окна планировщика партиций


async def test_migration_060_apply_rollback_reapply(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """TC-E2E-01: после apply таблицы группы есть; откат ровно до 060 снимает их и FK на nodes
    (050 и 040 остаются); повторное применение возвращает группу."""
    applied = run_cli(migrate_env, "migrate")
    assert applied.returncode == 0, applied.stderr
    assert NODES_TABLES <= await existing_tables(pg_dsn)

    rollback_through(migrate_env, "060_schema_nodes")
    tables = await existing_tables(pg_dsn)
    assert not (NODES_TABLES & tables), tables
    assert CATALOG_TABLES <= tables and IDENTITY_TABLES <= tables, "откат 060 не трогает 050 и 040"
    conn = await asyncpg.connect(pg_dsn)
    try:
        fk = await conn.fetchval(
            "select count(*) from pg_constraint "
            "where conname = 'node_billing_assignments_node_id_fkey'"
        )
        assert fk == 0, "откат снимает отложенный FK истории назначений"
    finally:
        await conn.close()

    reapplied = run_cli(migrate_env, "migrate")
    assert reapplied.returncode == 0, reapplied.stderr
    assert NODES_TABLES <= await existing_tables(pg_dsn)


async def test_constraints_reject_bad_rows(pg_dsn: str, migrate_env: dict[str, str]) -> None:
    """TC-E2E-02: UNIQUE, CHECK, NOT NULL, FK и каскады под app_rw."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    conn = await asyncpg.connect(pg_dsn)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            await check_node_constraints(conn)
        finally:
            await tx.rollback()
    finally:
        await conn.close()


async def test_rollback_131_keeps_annulled_tokens_refused(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Откат 131 снимает отметку аннулирования, а срок жизни аннулирование не трогает: без
    поправки токен, аннулированный отзывом identity, после отката (и повторного применения с
    пустой отметкой) снова был бы годен до конца часа (роаст 001.25, раунд 4). Откат сводит срок
    аннулированного токена к моменту аннулирования — отказ держится сроком, как до миграции;
    живой токен откат не трогает. Повторное применение нумерует прежние строки порядком выдачи
    (``id``), а не физическим порядком: UPDATE отката переносит аннулированный токен за живой, и
    нумерация по строкам сделала бы его «последним» (роаст 001.25, раунд 6); выданный после — с
    номером больше всех прежних. Физический порядок от раскладки страниц не зависит только тогда,
    когда вставка сама идёт не порядком ``id``: два десятка токенов с возрастающими uuidv7
    вставляются перемешанными, каждый третий аннулирован, и после повторного применения порядок
    номеров обязан совпасть с порядком ``id`` у всех (роаст раунда 7)."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    conn = await asyncpg.connect(pg_dsn)
    names = {"billing": "rollback-131-probe", "admin": "rollback-131@example.com"}
    try:
        billing_id = await conn.fetchval(
            "insert into billing_groups (name) values ($1) returning id", names["billing"]
        )
        node_id = await insert_node(conn, "RB-131-01", billing_id)
        admin_id = await conn.fetchval(
            "insert into admin_users (email, password_hash, role) values ($1, 'x', 'admin') "
            "returning id",
            names["admin"],
        )
        issue = (
            "insert into bootstrap_tokens (node_id, token_hash, expires_at, created_by, "
            "annulled_at) values ($1, $2, now() + interval '50 minutes', $3, $4) "
            "returning expires_at"
        )
        annulled_at = dt.datetime.now(dt.UTC).replace(microsecond=0) - dt.timedelta(minutes=5)
        await conn.fetchval(issue, node_id, "rb-annulled", admin_id, annulled_at)
        live_expires = await conn.fetchval(issue, node_id, "rb-live", admin_id, None)
        issued = [(uuid.uuid7(), f"rb-order-{i:02d}", i % 3 == 0) for i in range(24)]
        # Порядок вставки — перестановка шагом 7 (взаимно просто с 24): не порядок id.
        for token_id, token_hash, annulled in (issued[(i * 7) % 24] for i in range(24)):
            await conn.execute(
                "insert into bootstrap_tokens (id, node_id, token_hash, expires_at, created_by, "
                "annulled_at) values ($1, $2, $3, now() + interval '50 minutes', $4, $5)",
                token_id,
                node_id,
                token_hash,
                admin_id,
                annulled_at if annulled else None,
            )
        rollback_through(migrate_env, "131_bootstrap_token_annulled_at")
        rows = await conn.fetch(
            "select token_hash, expires_at from bootstrap_tokens where node_id = $1 "
            "and token_hash in ('rb-annulled', 'rb-live')",
            node_id,
        )
        expires = {row["token_hash"]: row["expires_at"] for row in rows}
        assert expires == {"rb-annulled": annulled_at, "rb-live": live_expires}, expires
        assert run_cli(migrate_env, "migrate").returncode == 0
        seqs = {
            row["token_hash"]: row["issue_seq"]
            for row in await conn.fetch(
                "select token_hash, issue_seq from bootstrap_tokens where node_id = $1", node_id
            )
        }
        assert seqs["rb-live"] > seqs["rb-annulled"], seqs
        ordered = await conn.fetch(
            "select issue_seq from bootstrap_tokens where token_hash like 'rb-order-%' order by id"
        )
        numbers = [row["issue_seq"] for row in ordered]
        assert len(numbers) == 24 and numbers == sorted(numbers), numbers
        fresh = await conn.fetchval(
            "insert into bootstrap_tokens (node_id, token_hash, expires_at, created_by) "
            "values ($1, 'rb-fresh', now() + interval '1 hour', $2) returning issue_seq",
            node_id,
            admin_id,
        )
        top = await conn.fetchval(
            "select max(issue_seq) from bootstrap_tokens where token_hash <> 'rb-fresh'"
        )
        assert fresh > top, (fresh, top)
    finally:
        reapplied = run_cli(migrate_env, "migrate")
        await conn.execute(
            "delete from bootstrap_tokens where token_hash = any($1::text[]) "
            "or token_hash like 'rb-order-%'",
            ["rb-annulled", "rb-live", "rb-fresh"],
        )
        await conn.execute("delete from nodes where code = 'RB-131-01'")
        await conn.execute("delete from billing_groups where name = $1", names["billing"])
        await conn.execute("delete from admin_users where email = $1", names["admin"])
        await conn.close()
    assert reapplied.returncode == 0, reapplied.stderr


IDENTITY_ROW = (
    "insert into node_identities (node_id, cert_fingerprint, cert_serial, token_hash, generation, "
    "expires_at, enrolled_from) values ($1, $2, $3, 'tok', 1, now() + interval '90 days', "
    "'198.51.100.9')"
)


async def test_rollback_130_refuses_while_identities_exist(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Откат 130 стёр бы серийные номера листов (по ним отказ на прокси, 001.66) и адреса обмена,
    а повторное применение 130 не прошло бы NOT NULL и оставило бы базу ниже 130 при работающем
    api (роаст 001.25, раунд 8): при непустой ``node_identities`` откат отказывает, а колонки и
    строка остаются на месте. Код ноды — тестовый (``t25-``): строку, брошенную прерванным
    прогоном, откаты следующего прогона снимут сами (роаст раунда 9)."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    conn = await asyncpg.connect(pg_dsn)
    suffix = uuid.uuid4().hex[:6]
    billing, code = f"rollback-130-probe-{suffix}", f"t25-rb130-{suffix}"
    try:
        billing_id = await conn.fetchval(
            "insert into billing_groups (name) values ($1) returning id", billing
        )
        node_id = await insert_node(conn, code, billing_id)
        await conn.execute(IDENTITY_ROW, node_id, "rb-130-fp", "RB130")
        first = run_cli(migrate_env, "migrate", "--rollback")
        assert first.returncode == 0 and "откачено миграций — 1" in first.stdout, first.stderr
        refused = run_cli(migrate_env, "migrate", "--rollback")
        assert refused.returncode != 0, refused.stdout
        assert "непустой node_identities" in refused.stdout + refused.stderr, refused.stderr
        columns = await conn.fetch(
            "select column_name from information_schema.columns where table_schema = "
            "'control_plane' and table_name = 'node_identities' "
            "and column_name in ('cert_serial', 'enrolled_from')"
        )
        assert {row["column_name"] for row in columns} == {"cert_serial", "enrolled_from"}
        serial = await conn.fetchval(
            "select cert_serial from node_identities where node_id = $1", node_id
        )
        assert serial == "RB130"
    finally:
        await conn.execute("delete from node_identities where cert_fingerprint = 'rb-130-fp'")
        await conn.execute("delete from nodes where code = $1", code)
        await conn.execute("delete from billing_groups where name = $1", billing)
        await conn.close()
        reapplied = run_cli(migrate_env, "migrate")
    assert reapplied.returncode == 0, reapplied.stderr


async def test_rollback_130_checks_under_the_table_lock(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """Отказ отката 130 — под блокировкой ``node_identities``: identity, вставленная обменом, но не
    зафиксированная к моменту проверки, иначе проверку миновала бы, ``DROP COLUMN`` дождался бы
    фиксации и стёр бы серийный номер уже выданного листа (роаст 001.25, раунд 9). Откат ждёт
    незавершённую вставку на блокировке таблицы, видит строку после фиксации и отказывает."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    conn = await asyncpg.connect(pg_dsn)
    writer = await asyncpg.connect(pg_dsn)
    suffix = uuid.uuid4().hex[:6]
    billing, code = f"rollback-130-race-{suffix}", f"t25-rb130-race-{suffix}"
    rollback: subprocess.Popen[str] | None = None
    killed = False
    try:
        billing_id = await conn.fetchval(
            "insert into billing_groups (name) values ($1) returning id", billing
        )
        node_id = await insert_node(conn, code, billing_id)
        first = run_cli(migrate_env, "migrate", "--rollback")  # 131: таблицу identity не трогает
        assert first.returncode == 0 and "откачено миграций — 1" in first.stdout, first.stderr
        insert = writer.transaction()
        await insert.start()
        await writer.execute(IDENTITY_ROW, node_id, "rb-130-race", "RB130R")
        rollback = start_cli(migrate_env, "migrate", "--rollback")
        waiting = 0
        for _ in range(200):
            waiting = await conn.fetchval(
                "select count(*) from pg_locks l join pg_class c on c.oid = l.relation "
                "where c.relname = 'node_identities' and not l.granted"
            )
            if waiting or rollback.poll() is not None:
                break
            await asyncio.sleep(0.05)
        assert waiting, "откат 130 не встал на блокировке node_identities"
        await insert.commit()
        out, err = rollback.communicate(timeout=60)
        assert rollback.returncode != 0, out
        assert "непустой node_identities" in out + err, err
        serial = await conn.fetchval(
            "select cert_serial from node_identities where cert_fingerprint = 'rb-130-race'"
        )
        assert serial == "RB130R", "серийный номер выданного листа на месте"
    finally:
        if rollback is not None and rollback.poll() is None:
            if writer.is_in_transaction():
                await writer.execute("rollback")
            try:
                rollback.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                rollback.kill()
                rollback.communicate()
                killed = True
        await writer.close()
        await conn.execute("delete from node_identities where cert_fingerprint = 'rb-130-race'")
        await conn.execute("delete from nodes where code = $1", code)
        await conn.execute("delete from billing_groups where name = $1", billing)
        await conn.close()
        if killed:
            run_cli(migrate_env, "migrate", "--break-lock")
        reapplied = run_cli(migrate_env, "migrate")
    assert reapplied.returncode == 0, reapplied.stderr


async def test_rollback_through_130_clears_test_identities_first(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """``rollback_through`` через 130 сначала снимает identity тестовых нод: строка, брошенная
    прерванным прогоном (здесь её вставляет сам тест), иначе валила бы откаты всех тестов
    миграций (роаст 001.25, раунд 8); место вызова уборки прежде держал только пустой стенд
    (роаст раунда 9)."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    conn = await asyncpg.connect(pg_dsn)
    suffix = uuid.uuid4().hex[:6]
    billing, code = f"rollback-130-left-{suffix}", f"t25-rb130-left-{suffix}"
    try:
        billing_id = await conn.fetchval(
            "insert into billing_groups (name) values ($1) returning id", billing
        )
        node_id = await insert_node(conn, code, billing_id)
        await conn.execute(IDENTITY_ROW, node_id, "rb-130-left", "RB130L")
        assert rollback_through(migrate_env, IDENTITY_MIGRATION) == 2
        assert await conn.fetchval("select count(*) from node_identities") == 0
    finally:
        await conn.execute("delete from node_identities where cert_fingerprint = 'rb-130-left'")
        await conn.execute("delete from nodes where code = $1", code)
        await conn.execute("delete from billing_groups where name = $1", billing)
        await conn.close()
        reapplied = run_cli(migrate_env, "migrate")
    assert reapplied.returncode == 0, reapplied.stderr


async def test_the_cleanup_before_rollback_130_takes_only_test_identities(
    pg_dsn: str, migrate_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Помощник отката (``clear_test_identities``) снимает identity тестовых нод (сквозные тесты —
    ``t19-``, пробы стенда — ``t25-``), а identity другой ноды оставляет и отказывает с их числом
    (роаст 001.25, раунд 8). Чужой строку делает подмена префиксов помощника, а не нетестовый код:
    строка, брошенная прерванным прогоном, остаётся тестовой, и её снимут откаты следующего
    прогона (роаст раунда 9)."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    clear_test_identities(migrate_env)  # чистый старт: брошенные тестовые строки сняты
    conn = await asyncpg.connect(pg_dsn)
    suffix = uuid.uuid4().hex[:6]
    billing = f"rollback-130-clear-{suffix}"
    codes = {"test": f"{PREFIX}rb130-{suffix}", "other": f"t25-rb130-{suffix}"}
    try:
        billing_id = await conn.fetchval(
            "insert into billing_groups (name) values ($1) returning id", billing
        )
        nodes = {kind: await insert_node(conn, code, billing_id) for kind, code in codes.items()}
        await conn.execute(IDENTITY_ROW, nodes["test"], "rb-130-test", "RB130T")
        await conn.execute(IDENTITY_ROW, nodes["other"], "rb-130-other", "RB130O")
        # Для этого вызова тестовые — только ноды сквозных тестов: строка пробы стенда — чужая.
        monkeypatch.setattr(_cli, "TEST_NODE_CODES", (f"{PREFIX}%",))
        with pytest.raises(AssertionError, match="нетестовых строк"):
            clear_test_identities(migrate_env)
        left = await conn.fetch("select cert_fingerprint from node_identities")
        assert [row["cert_fingerprint"] for row in left] == ["rb-130-other"], left
        monkeypatch.undo()
        clear_test_identities(migrate_env)
        assert await conn.fetchval("select count(*) from node_identities") == 0
    finally:
        await conn.execute(
            "delete from node_identities where cert_fingerprint = any($1::text[])",
            ["rb-130-test", "rb-130-other"],
        )
        await conn.execute("delete from nodes where code = any($1::text[])", list(codes.values()))
        await conn.execute("delete from billing_groups where name = $1", billing)
        await conn.close()


async def check_node_constraints(conn: asyncpg.Connection) -> None:
    """Тело TC-E2E-02 внутри открытой транзакции."""

    async def rejected(exc: type[Exception], query: str, *args: object) -> None:
        with pytest.raises(exc):
            async with conn.transaction():
                await conn.execute(query, *args)

    billing_id = await conn.fetchval(
        "insert into billing_groups (name) values ($1) returning id", "tier-probe"
    )
    node_id = await insert_node(conn, "DE-Berlin-01", billing_id)
    await rejected(
        asyncpg.UniqueViolationError,
        "insert into nodes (code, name, country, city, "
        "provider, public_ipv4, billing_group_id, bandwidth_mbps, max_conn_per_ip) "
        "values ('DE-Berlin-01', 'dup', 'DE', 'Berlin', 'p', '203.0.113.9', $1, 100, 4)",
        billing_id,
    )
    await rejected(
        asyncpg.NotNullViolationError,
        "insert into nodes (code, name, country, city, "
        "provider, public_ipv4, bandwidth_mbps, max_conn_per_ip) values ('NL-01', 'n', "
        "'NL', 'Ams', 'p', '203.0.113.10', 100, 4)",
    )  # billing_group_id NOT NULL (R-18)
    await rejected(
        asyncpg.ForeignKeyViolationError,
        "insert into nodes (code, name, country, "
        "city, provider, public_ipv4, billing_group_id, bandwidth_mbps, max_conn_per_ip) "
        "values ('NL-02', 'n', 'NL', 'Ams', 'p', '203.0.113.11', $1, 100, 4)",
        uuid.uuid4(),
    )
    threshold_updates = {  # имена колонок фиксированы тестом, значения нарушают CHECK
        "bandwidth_mbps": "update nodes set bandwidth_mbps = $1 where id = $2",
        "max_conn_per_ip": "update nodes set max_conn_per_ip = $1 where id = $2",
        "multiplier_milli": "update nodes set multiplier_milli = $1 where id = $2",
    }
    for column, value in (("bandwidth_mbps", 0), ("max_conn_per_ip", 0), ("multiplier_milli", 150)):
        await rejected(asyncpg.CheckViolationError, threshold_updates[column], value, node_id)
    await conn.execute("update nodes set multiplier_milli = 500 where id = $1", node_id)
    await rejected(
        asyncpg.InvalidTextRepresentationError,
        "update nodes set status = $1::node_status where id = $2",
        "sleeping",
        node_id,
    )

    inbound = (
        "insert into inbounds (node_id, profile, port, tag, reality_public_key, reality_target, "
        "reality_min_client_ver, client_fingerprint) values ($1, $2, $3, $4, 'pk', "
        "'www.example.com', "
        "'1.8.0', 'chrome') returning id"
    )
    inbound_id = await conn.fetchval(inbound, node_id, "vless_raw_vision", 443, "vision")
    await rejected(asyncpg.UniqueViolationError, inbound, node_id, "vless_raw_vision", 8443, "dup")
    await rejected(asyncpg.UniqueViolationError, inbound, node_id, "vless_xhttp", 443, "dup-port")
    await rejected(asyncpg.CheckViolationError, inbound, node_id, "vless_xhttp", 70000, "port")
    await rejected(asyncpg.CheckViolationError, inbound, node_id, "trojan_reality", 0, "port0")
    await conn.execute(
        "insert into inbound_secrets (inbound_id, private_key_enc) values ($1, $2)",
        inbound_id,
        b"enc",
    )
    await rejected(
        asyncpg.ForeignKeyViolationError,
        "insert into inbound_secrets (inbound_id, private_key_enc) values ($1, $2)",
        uuid.uuid4(),
        b"enc",
    )
    # Секрет живёт только вместе с inbound (ON DELETE CASCADE); нода с inbound не удаляется.
    await rejected(asyncpg.ForeignKeyViolationError, "delete from nodes where id = $1", node_id)
    await conn.execute("delete from inbounds where id = $1", inbound_id)
    assert (
        await conn.fetchval(
            "select count(*) from inbound_secrets where inbound_id = $1", inbound_id
        )
        == 0
    )

    identity = (
        "insert into node_identities (node_id, cert_fingerprint, cert_serial, token_hash, "
        "generation, expires_at, enrolled_from) "
        "values ($1, $2, $3, 'tok', $4, now() + interval '90 days', $5)"
    )
    source = "198.51.100.7"
    await conn.execute(identity, node_id, "fp-1", "01", 1, source)
    await rejected(asyncpg.UniqueViolationError, identity, node_id, "fp-1", "02", 2, source)
    stranger = uuid.uuid4()
    await rejected(asyncpg.ForeignKeyViolationError, identity, stranger, "fp-2", "03", 1, source)
    # Миграция 130 (001.25): identity без адреса источника enrollment не записывается —
    # администратору на шаге 6 UC-01 нечего было бы сверять; поколение ноды уникально; серийный
    # номер листа обязателен и уникален — по нему карта отказа 001.66 находит лист.
    await rejected(asyncpg.NotNullViolationError, identity, node_id, "fp-3", "04", 2, None)
    await rejected(asyncpg.UniqueViolationError, identity, node_id, "fp-4", "05", 1, source)
    await rejected(asyncpg.NotNullViolationError, identity, node_id, "fp-5", None, 2, source)
    await rejected(asyncpg.UniqueViolationError, identity, node_id, "fp-6", "01", 2, source)
    # Миграция 131 (001.25): токен либо погашен, либо аннулирован — не то и другое сразу.
    admin_id = await conn.fetchval(
        "insert into admin_users (email, password_hash, role) values ($1, 'x', 'admin') "
        "returning id",
        "schema-probe@example.com",
    )
    issue = (
        "insert into bootstrap_tokens (node_id, token_hash, expires_at, created_by, used_at, "
        "annulled_at) values ($1, $2, now() + interval '1 hour', $3, $4, $5)"
    )
    moment = dt.datetime.now(dt.UTC)
    await conn.execute(issue, node_id, "th-1", admin_id, None, moment)
    await conn.execute(issue, node_id, "th-2", admin_id, moment, None)
    await rejected(asyncpg.CheckViolationError, issue, node_id, "th-3", admin_id, moment, moment)
    # Номер выдачи (131, раунд 4 роаста) даёт последовательность базы: у позже выданного он
    # больше, и обычная вставка его не задаёт (GENERATED ALWAYS; явное значение — только
    # `OVERRIDING SYSTEM VALUE`, которого в коде нет) — «последний токен» не зависит от часов.
    order = await conn.fetch(
        "select token_hash from bootstrap_tokens where node_id = $1 order by issue_seq", node_id
    )
    assert [row["token_hash"] for row in order] == ["th-1", "th-2"]
    await rejected(
        asyncpg.GeneratedAlwaysError,
        "insert into bootstrap_tokens (node_id, token_hash, expires_at, created_by, issue_seq) "
        "values ($1, 'th-4', now() + interval '1 hour', $2, 1)",
        node_id,
        admin_id,
    )

    user_id = await conn.fetchval(
        "insert into users (email, password_hash, aup_version, aup_accepted_at) "
        "values ('node-probe@example.com', 'x', '2026-09', now()) returning id"
    )
    credential = (
        "insert into node_user_credentials (user_id, node_id, xray_email, vless_uuid_enc, "
        "trojan_password_enc) values ($1, $2, $3, $4, $4)"
    )
    await conn.execute(credential, user_id, node_id, f"u{user_id}", b"enc")
    await rejected(asyncpg.UniqueViolationError, credential, user_id, node_id, "dup", b"enc")
    await rejected(asyncpg.ForeignKeyViolationError, credential, uuid.uuid4(), node_id, "x", b"e")
    state = (
        "insert into node_user_state (node_id, user_id, state, updated_seq) values ($1, $2, $3, 1)"
    )
    await conn.execute(state, node_id, user_id, "active")
    await rejected(asyncpg.UniqueViolationError, state, node_id, user_id, "expired")  # PK
    await rejected(
        asyncpg.InvalidTextRepresentationError,
        "insert into node_user_state (node_id, user_id, state, updated_seq) values ($1, $2, "
        "$3::user_node_state, 2)",
        node_id,
        uuid.uuid4(),
        "paused",
    )

    group_id = await conn.fetchval(
        "insert into access_groups (name) values ('probe-group') returning id"
    )
    await conn.execute(
        "insert into node_access_groups (node_id, access_group_id) values ($1, $2)",
        node_id,
        group_id,
    )
    await conn.execute("delete from access_groups where id = $1", group_id)
    assert (
        await conn.fetchval("select count(*) from node_access_groups where node_id = $1", node_id)
        == 0
    )

    command = (
        "insert into commands (node_id, type, expires_at) "
        "values ($1, $2, now() + interval '1 hour')"
    )
    await conn.execute(command, node_id, "restart_xray")
    await rejected(
        asyncpg.InvalidTextRepresentationError,
        command.replace("$2", "$2::command_type"),
        node_id,
        "reboot",
    )
    await rejected(asyncpg.ForeignKeyViolationError, command, uuid.uuid4(), "restart_xray")

    await conn.execute(
        "insert into node_country_availability (node_id, country, available) values ($1, 'RU', "
        "true)",
        node_id,
    )
    await rejected(
        asyncpg.UniqueViolationError,
        "insert into node_country_availability (node_id, country, available) values ($1, 'RU', "
        "false)",
        node_id,
    )
    await conn.execute(
        "insert into node_config_versions (node_id, config_version, config_json, checksum) "
        "values ($1, 1, '{}', 'sha')",
        node_id,
    )
    await rejected(
        asyncpg.UniqueViolationError,
        "insert into node_config_versions (node_id, config_version, config_json, checksum) "
        "values ($1, 1, '{}', 'sha2')",
        node_id,
    )
    history_id = await conn.fetchval(
        "insert into node_ip_history (node_id, public_ipv4, valid_from) "
        "values ($1, '203.0.113.20', now()) returning id",
        node_id,
    )
    assert isinstance(history_id, int) and history_id > 0, "identity-столбец под app_rw"
    await rejected(  # valid_to раньше valid_from — CHECK, как у историй 050
        asyncpg.CheckViolationError,
        "insert into node_ip_history (node_id, public_ipv4, valid_from, valid_to) "
        "values ($1, '203.0.113.21', now(), now() - interval '1 day')",
        node_id,
    )
    await rejected(  # партиций node_metrics до планировщика 001.08 нет
        asyncpg.CheckViolationError,
        "insert into node_metrics (node_id, ts, cpu_pct, mem_pct, disk_pct, net_rx_bytes, "
        "net_tx_bytes, online_ips, connections) values ($1, $2, 1, 1, 1, 0, 0, 0, 0)",
        node_id,
        PROBE_TS,
    )


async def test_node_metrics_insert_as_app_rw(pg_dsn: str, migrate_env: dict[str, str]) -> None:
    """С партицией на дату app_rw вставляет метрики; PK (node_id, ts) отклоняет дубль. Партицию
    создаёт и удаляет app_owner (через MIGRATE_DSN); дата — 2000-01-01, вне окна планировщика."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    dsn = migrate_dsn(migrate_env["MIGRATE_DSN"], migrate_env.get("MIGRATE_PASSWORD_FILE"))
    with psycopg.connect(
        dsn.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True
    ) as owner:
        owner.execute("SET ROLE app_owner")
        owner.execute("SET search_path TO control_plane")
        owner.execute("DROP TABLE IF EXISTS node_metrics_probe")
        day = PROBE_TS.replace(hour=0)
        owner.execute(
            sql.SQL(
                "CREATE TABLE node_metrics_probe PARTITION OF node_metrics "
                "FOR VALUES FROM ({start}) TO ({stop})"
            ).format(
                start=sql.Literal(day.isoformat()),
                stop=sql.Literal((day + dt.timedelta(days=1)).isoformat()),
            )
        )
        try:
            conn = await asyncpg.connect(pg_dsn)
            try:
                tx = conn.transaction()
                await tx.start()
                try:
                    billing_id = await conn.fetchval(
                        "insert into billing_groups (name) values ('metrics-probe') returning id"
                    )
                    node_id = await insert_node(conn, "SG-01", billing_id)
                    insert = (
                        "insert into node_metrics (node_id, ts, cpu_pct, mem_pct, disk_pct, "
                        "net_rx_bytes, net_tx_bytes, online_ips, connections) "
                        "values ($1, $2, 12.5, 40.0, 55.5, 1024, 2048, 3, 7)"
                    )
                    await conn.execute(insert, node_id, PROBE_TS)
                    with pytest.raises(asyncpg.UniqueViolationError):  # PK (node_id, ts)
                        async with conn.transaction():
                            await conn.execute(insert, node_id, PROBE_TS)
                    stored = await conn.fetchval(
                        "select count(*) from node_metrics where node_id = $1", node_id
                    )
                    assert stored == 1
                finally:
                    await tx.rollback()
            finally:
                await conn.close()
        finally:
            owner.execute("DROP TABLE node_metrics_probe")
