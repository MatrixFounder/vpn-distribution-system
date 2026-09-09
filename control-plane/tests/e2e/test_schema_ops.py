"""Сквозные проверки задачи 001.09 (миграция 090 «события, доставки, очередь задач, аудит,
настройки»).

TC-E2E-01: применение, откат ровно до 090 и повторное применение; откат удаляет только объекты 090
(включая функции). TC-E2E-02: ограничения §4.2.6/§4.4 и запреты §4.6 под app_rw — UNIQUE
dedup_key, FK и каскад доставок, CHECK попыток, частичная уникальность ключа идемпотентности
среди активных задач (R-46), CHECK пары блокировки, CHECK result, audit_log без UPDATE/DELETE,
settings.state_generation; purge_audit_log под app_rw удаляет ровно записи старше 12 месяцев
(граница ±1 с по now() базы). Append-only audit_log в три слоя под владельцем: привилегии
(UPDATE/DELETE/TRUNCATE отозваны и у app_owner), триггер (при возвращённых себе правах и при
подделке текста запроса — RaiseException), роль очистки (UPDATE запрещён; DELETE под явным
SET ROLE — объявленный остаточный путь).
"""

from __future__ import annotations

import datetime as dt
import uuid

import asyncpg
import psycopg
import pytest

from ._cli import rollback_through, run_cli
from ._db import existing_tables, owner_connection
from ._spec import ACCOUNTING_TABLES, OPS_TABLES

AUDIT_ROW = (
    "insert into audit_log (actor_type, action, entity_type, entity_id, result) "
    "values ('system', 'probe', 'node', $1, $2)"
)


async def test_migration_090_apply_rollback_reapply(
    pg_dsn: str, migrate_env: dict[str, str]
) -> None:
    """TC-E2E-01: после apply шесть таблиц и функции есть; откат ровно до 090 снимает только их
    (080 остаётся); повторное применение возвращает группу."""
    applied = run_cli(migrate_env, "migrate")
    assert applied.returncode == 0, applied.stderr
    assert OPS_TABLES <= await existing_tables(pg_dsn)

    rollback_through(migrate_env, "090_schema_ops")
    tables = await existing_tables(pg_dsn)
    assert not (OPS_TABLES & tables), tables
    assert ACCOUNTING_TABLES <= tables, "откат 090 не трогает 080"
    conn = await asyncpg.connect(pg_dsn)
    try:
        functions = await conn.fetchval(
            "select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
            "where n.nspname = 'control_plane' "
            "and p.proname in ('purge_audit_log', 'audit_log_immutable')"
        )
        assert functions == 0, "функции 090 удалены откатом"
    finally:
        await conn.close()

    reapplied = run_cli(migrate_env, "migrate")
    assert reapplied.returncode == 0, reapplied.stderr
    assert OPS_TABLES <= await existing_tables(pg_dsn)


async def test_constraints_and_privileges(pg_dsn: str, migrate_env: dict[str, str]) -> None:
    """TC-E2E-02: ограничения и запреты §4.6 под app_rw (в откатываемой транзакции)."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    conn = await asyncpg.connect(pg_dsn)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            await check_ops_constraints(conn)
        finally:
            await tx.rollback()
    finally:
        await conn.close()


async def check_ops_constraints(conn: asyncpg.Connection) -> None:
    """Тело TC-E2E-02 внутри открытой транзакции."""

    async def rejected(exc: type[Exception], query: str, *args: object) -> None:
        with pytest.raises(exc):
            async with conn.transaction():
                await conn.execute(query, *args)

    event = (
        "insert into events (type, user_id, dedup_key) values ('traffic_80', $1, $2) returning id"
    )
    user_id = uuid.uuid4()
    event_id = await conn.fetchval(event, user_id, f"traffic_80:{user_id}")
    await rejected(asyncpg.UniqueViolationError, event, user_id, f"traffic_80:{user_id}")
    await rejected(
        asyncpg.InvalidTextRepresentationError,
        "insert into events (type, dedup_key) values ($1::event_type, 'x')",
        "not_an_event",
    )

    delivery = (
        "insert into email_deliveries (event_id, recipient, language, attempts) "
        "values ($1, 'User@Example.com', 'ru', $2)"
    )
    await conn.execute(delivery, event_id, 0)
    await rejected(asyncpg.ForeignKeyViolationError, delivery, uuid.uuid4(), 0)
    await rejected(asyncpg.CheckViolationError, delivery, event_id, -1)
    await conn.execute(
        "insert into webhook_deliveries (event_id, url) values ($1, 'https://hooks.example.com/x')",
        event_id,
    )
    assert (
        await conn.fetchval(
            "select count(*) from email_deliveries where recipient = 'user@example.com'"
        )
        == 1
    ), "recipient — citext"
    await conn.execute("delete from events where id = $1", event_id)
    assert (
        await conn.fetchval("select count(*) from email_deliveries where event_id = $1", event_id)
        == 0
    ), "каскад: доставки уходят вместе с событием"
    assert (
        await conn.fetchval("select count(*) from webhook_deliveries where event_id = $1", event_id)
        == 0
    )

    job = (
        "insert into jobs (queue, type, idempotency_key, max_attempts, status) "
        "values ('critical', 'probe', $1, $2, $3::job_status)"
    )
    await conn.execute(job, "k1", 3, "done")
    await conn.execute(job, "k1", 3, "pending")  # done не занимает ключ
    await rejected(asyncpg.UniqueViolationError, job, "k1", 3, "pending")
    await rejected(asyncpg.UniqueViolationError, job, "k1", 3, "running")
    await rejected(asyncpg.CheckViolationError, job, "k2", 0, "pending")
    await rejected(
        asyncpg.CheckViolationError,
        "insert into jobs (queue, type, idempotency_key, max_attempts, locked_at) "
        "values ('background', 'probe', 'k3', 1, now())",
    )
    await conn.execute(
        "update jobs set status = 'running', locked_at = now(), locked_by = 'w1' "
        "where idempotency_key = 'k1' and status = 'pending'"
    )
    await rejected(asyncpg.UniqueViolationError, job, "k1", 3, "pending")  # running тоже держит

    await conn.execute(AUDIT_ROW, "n1", "success")
    await rejected(asyncpg.CheckViolationError, AUDIT_ROW, "n1", "oops")
    await rejected(
        asyncpg.InsufficientPrivilegeError, "update audit_log set action = 'x' where false"
    )
    await rejected(asyncpg.InsufficientPrivilegeError, "delete from audit_log where false")

    assert await conn.fetchval("select value from settings where key = 'state_generation'") == "0"
    await conn.execute(
        "update settings set value = '1', updated_at = now() where key = 'state_generation'"
    )
    assert await conn.fetchval("select value from settings where key = 'state_generation'") == "1"

    # Удаление по сроку (12 месяцев, Н-23) под app_rw. Граница считается базой той же
    # арифметикой, что в функции, и в той же транзакции (now() постоянен): секунда за границей
    # уходит, секунда до границы и запись 300 суток остаются; повтор ничего не удаляет.
    dated = (
        "insert into audit_log (ts, actor_type, action, entity_type, entity_id, result) "
        "values (now() - interval '12 months' + $1::interval, 'system', 'probe', 'node', $2, "
        "'success')"
    )
    await conn.execute(dated, dt.timedelta(days=-100), "old")
    await conn.execute(dated, dt.timedelta(seconds=-1), "expired")
    await conn.execute(dated, dt.timedelta(seconds=1), "kept")
    await conn.execute(dated, dt.timedelta(days=65), "recent")
    assert await conn.fetchval("select purge_audit_log()") == 2
    remaining = await conn.fetch("select entity_id from audit_log order by 1")
    assert [r["entity_id"] for r in remaining] == ["kept", "n1", "recent"]
    assert await conn.fetchval("select purge_audit_log()") == 0


MUTATIONS = (
    "update audit_log set action = 'x' where entity_id = 'owner'",
    "delete from audit_log where entity_id = 'owner'",
    "truncate audit_log",
)
# Подделка текста запроса из plpgsql: раньше триггер проверял PG_CONTEXT, где виден текст
# оператора, и такой литерал его обходил (ревью 001.09); проверка роли на текст не смотрит.
SPOOF = (
    "create function pg_temp.spoof() returns void language plpgsql as $f$ begin "
    "delete from control_plane.audit_log where entity_id = 'owner' and "
    "E'\\nPL/pgSQL function purge_audit_log() line 1 at SQL statement' <> ''; end $f$"
)


def test_audit_log_immutable_for_owner(migrate_env: dict[str, str]) -> None:
    """Append-only под владельцем (§4.6 «для всех ролей»), три слоя в одной откатываемой
    транзакции: (1) привилегии — UPDATE/DELETE/TRUNCATE у app_owner отозваны; (2) триггер — даже
    вернув себе права GRANT-ом, владелец получает RaiseException, подделка текста запроса тоже;
    (3) роль очистки — UPDATE ей не выдан, а при выданном триггер пропускает только DELETE;
    DELETE под явным SET ROLE app_audit_purge проходит — объявленный остаточный путь."""
    assert run_cli(migrate_env, "migrate").returncode == 0
    with owner_connection(migrate_env) as owner:
        owner.autocommit = False
        with owner.transaction():
            owner.execute(
                "insert into audit_log (actor_type, action, entity_type, entity_id, result) "
                "values ('system', 'probe', 'node', 'owner', 'success')"
            )
            for statement in MUTATIONS:  # слой 1
                with pytest.raises(psycopg.errors.InsufficientPrivilege), owner.transaction():
                    owner.execute(statement)
            owner.execute("grant update, delete, truncate on audit_log to app_owner")
            for statement in MUTATIONS:  # слой 2
                with (
                    pytest.raises(psycopg.errors.RaiseException, match="только для добавления"),
                    owner.transaction(),
                ):
                    owner.execute(statement)
            with (
                pytest.raises(psycopg.errors.RaiseException, match="только для добавления"),
                owner.transaction(),
            ):
                owner.execute(SPOOF)
                owner.execute("select pg_temp.spoof()")
            owner.execute("revoke update, delete, truncate on audit_log from app_owner")
            assert owner.execute(
                "select count(*) from audit_log where entity_id = 'owner'"
            ).fetchone() == (1,)

            # Слой 3: роль очистки видит только ts, поэтому условия — по ts.
            owner.execute("grant update on audit_log to app_audit_purge")
            owner.execute("set local role app_audit_purge")
            with (
                pytest.raises(psycopg.errors.RaiseException, match="UPDATE запрещён"),
                owner.transaction(),
            ):
                owner.execute("update audit_log set action = 'x' where ts is not null")
            owner.execute("set local role app_owner")
            owner.execute("revoke update on audit_log from app_audit_purge")
            owner.execute("set local role app_audit_purge")
            with pytest.raises(psycopg.errors.InsufficientPrivilege), owner.transaction():
                owner.execute("update audit_log set action = 'x' where ts is not null")
            with pytest.raises(psycopg.errors.InsufficientPrivilege), owner.transaction():
                owner.execute("select entity_id from audit_log")  # содержимое журнала закрыто
            owner.execute("delete from audit_log where ts is not null")  # остаточный путь
            owner.execute("set local role app_owner")
            assert owner.execute(
                "select count(*) from audit_log where entity_id = 'owner'"
            ).fetchone() == (0,)
            raise psycopg.Rollback
        assert owner.execute("select count(*) from audit_log").fetchone() == (0,)
