"""Задача 001.07, стражи схемы «подписки, баланс, токены, коды» по каталогам PostgreSQL.

``test_catalog_matches_data_model`` сверяет колонки, ограничения (PK/UNIQUE/FK/CHECK) и индексы
девяти таблиц с data-model.md §4.2.4 и §4.4 плюс отклонения, объявленные в задаче; частичный
уникальный индекс токенов (R-14), ключ партиционирования ``subscription_access_log`` и её
последовательность. ``test_app_rw_privileges`` (TC-UNIT-01): права — по §4.6, включая особый
запрет ``UPDATE``/``DELETE`` на ``balance_entries`` у ``app_rw``; права на две последовательности.
Без базы тесты падают, не пропускаются.
"""

from __future__ import annotations

import asyncpg
import pytest

from ._introspect import (
    EXPECTED_TABLE_GRANTS,
    SCHEMA,
    Column,
    assert_table_matches,
    table_grants,
)

EXPECTED_COLUMNS: dict[str, list[Column]] = {
    "subscription_periods": [
        ("id", "uuid", False, "uuidv7()"),
        ("user_id", "uuid", False, None),
        ("plan_id", "uuid", False, None),
        ("period_start", "timestamptz", False, None),
        ("period_end", "timestamptz", False, None),
        ("traffic_limit_bytes", "int8", True, None),
        ("used_billable_bytes", "int8", False, "0"),
        ("notified_80_at", "timestamptz", True, None),
        ("notified_95_at", "timestamptz", True, None),
        ("exhausted_at", "timestamptz", True, None),
        ("source", "period_source", False, None),
        ("source_id", "uuid", True, None),
        ("created_at", "timestamptz", False, "now()"),
    ],
    "subscriptions": [
        ("user_id", "uuid", False, None),
        ("state", "subscription_state", False, "'none'::subscription_state"),
        ("current_period_id", "uuid", True, None),
        ("device_limit", "int4", True, None),
        ("state_changed_at", "timestamptz", False, "now()"),
    ],
    "balance_entries": [
        ("id", "int8", False, None),
        ("period_id", "uuid", False, None),
        ("source", "balance_source", False, None),
        ("delta_billable_bytes", "int8", False, None),
        ("ref_key", "text", False, None),
        ("actor_id", "uuid", True, None),
        ("reason", "text", True, None),
        ("created_at", "timestamptz", False, "now()"),
    ],
    "subscription_tokens": [
        ("id", "uuid", False, "uuidv7()"),
        ("user_id", "uuid", False, None),
        ("token_hash", "text", False, None),
        ("issued_at", "timestamptz", False, "now()"),
        ("revoked_at", "timestamptz", True, None),
    ],
    "subscription_access_log": [
        ("id", "int8", False, "nextval('subscription_access_log_id_seq'::regclass)"),
        ("user_id", "uuid", False, None),
        ("ts", "timestamptz", False, "now()"),
        ("domain", "text", False, None),
        ("format", "text", False, None),
        ("user_agent", "text", True, None),
        ("source_ip", "inet", False, None),
        ("country", "bpchar", True, None),
    ],
    "codes": [
        ("id", "uuid", False, "uuidv7()"),
        ("kind", "code_kind", False, None),
        ("code_hash", "text", False, None),
        ("code_enc", "bytea", False, None),
        ("batch_id", "uuid", True, None),
        ("plan_id", "uuid", True, None),
        ("expires_at", "timestamptz", True, None),
        ("max_uses", "int4", False, "1"),
        ("max_uses_per_user", "int4", False, "1"),
        ("uses_count", "int4", False, "0"),
        ("traffic_bonus_bytes", "int8", False, "0"),
        ("duration_bonus_days", "int4", False, "0"),
        ("created_by", "uuid", False, None),
        ("created_at", "timestamptz", False, "now()"),
    ],
    "code_redemptions": [
        ("id", "uuid", False, "uuidv7()"),
        ("code_id", "uuid", False, None),
        ("user_id", "uuid", False, None),
        ("period_id", "uuid", False, None),
        ("redeemed_at", "timestamptz", False, "now()"),
    ],
    "orders": [
        ("id", "uuid", False, "uuidv7()"),
        ("user_id", "uuid", False, None),
        ("plan_id", "uuid", False, None),
        ("amount", "numeric", False, None),
        ("currency", "bpchar", False, None),
        ("status", "text", False, None),
        ("created_at", "timestamptz", False, "now()"),
    ],
    "payments": [
        ("id", "uuid", False, "uuidv7()"),
        ("order_id", "uuid", False, None),
        ("provider", "text", False, None),
        ("provider_ref", "text", False, None),
        ("amount", "numeric", False, None),
        ("currency", "bpchar", False, None),
        ("status", "text", False, None),
        ("created_at", "timestamptz", False, "now()"),
    ],
}

EXPECTED_CONSTRAINTS: dict[str, set[tuple[str, str]]] = {
    "subscription_periods": {
        ("c", "CHECK ((period_end > period_start))"),
        ("f", "FOREIGN KEY (plan_id) REFERENCES plans(id)"),
        ("f", "FOREIGN KEY (user_id) REFERENCES users(id)"),
        ("p", "PRIMARY KEY (id)"),
    },
    "subscriptions": {
        ("f", "FOREIGN KEY (current_period_id) REFERENCES subscription_periods(id)"),
        ("f", "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"),
        ("p", "PRIMARY KEY (user_id)"),
    },
    "balance_entries": {
        ("c", "CHECK (((source <> 'adjustment'::balance_source) OR (reason IS NOT NULL)))"),
        ("f", "FOREIGN KEY (period_id) REFERENCES subscription_periods(id)"),
        ("p", "PRIMARY KEY (id)"),
    },
    "subscription_tokens": {
        ("f", "FOREIGN KEY (user_id) REFERENCES users(id)"),
        ("p", "PRIMARY KEY (id)"),
        ("u", "UNIQUE (token_hash)"),
    },
    "subscription_access_log": {
        ("p", "PRIMARY KEY (ts, id)"),
    },
    "codes": {
        ("c", "CHECK ((duration_bonus_days >= 0))"),
        ("c", "CHECK ((max_uses > 0))"),
        ("c", "CHECK ((max_uses_per_user > 0))"),
        ("c", "CHECK ((traffic_bonus_bytes >= 0))"),
        ("c", "CHECK ((uses_count >= 0))"),
        ("f", "FOREIGN KEY (created_by) REFERENCES admin_users(id)"),
        ("f", "FOREIGN KEY (plan_id) REFERENCES plans(id)"),
        ("p", "PRIMARY KEY (id)"),
        ("u", "UNIQUE (code_hash)"),
    },
    "code_redemptions": {
        ("f", "FOREIGN KEY (code_id) REFERENCES codes(id)"),
        ("f", "FOREIGN KEY (period_id) REFERENCES subscription_periods(id)"),
        ("f", "FOREIGN KEY (user_id) REFERENCES users(id)"),
        ("p", "PRIMARY KEY (id)"),
    },
    "orders": {
        ("c", "CHECK ((amount >= (0)::numeric))"),
        ("f", "FOREIGN KEY (plan_id) REFERENCES plans(id)"),
        ("f", "FOREIGN KEY (user_id) REFERENCES users(id)"),
        ("p", "PRIMARY KEY (id)"),
    },
    "payments": {
        ("c", "CHECK ((amount >= (0)::numeric))"),
        ("f", "FOREIGN KEY (order_id) REFERENCES orders(id)"),
        ("p", "PRIMARY KEY (id)"),
    },
}

EXPECTED_INDEXES: dict[str, set[str]] = {
    "subscription_periods": {
        "CREATE INDEX subscription_periods_period_end_idx ON control_plane.subscription_periods "
        "USING btree (period_end)",
        "CREATE INDEX subscription_periods_user_id_period_end_idx ON "
        "control_plane.subscription_periods USING btree (user_id, period_end DESC)",
        "CREATE UNIQUE INDEX subscription_periods_pkey ON control_plane.subscription_periods "
        "USING btree (id)",
    },
    "subscriptions": {
        "CREATE UNIQUE INDEX subscriptions_pkey ON control_plane.subscriptions USING btree "
        "(user_id)",
    },
    "balance_entries": {
        "CREATE INDEX balance_entries_period_id_created_at_idx ON control_plane.balance_entries "
        "USING btree (period_id, created_at)",
        "CREATE UNIQUE INDEX balance_entries_pkey ON control_plane.balance_entries USING btree "
        "(id)",
    },
    "subscription_tokens": {
        "CREATE UNIQUE INDEX subscription_tokens_active_user_idx ON "
        "control_plane.subscription_tokens USING btree (user_id) WHERE (revoked_at IS NULL)",
        "CREATE UNIQUE INDEX subscription_tokens_pkey ON control_plane.subscription_tokens USING "
        "btree (id)",
        "CREATE UNIQUE INDEX subscription_tokens_token_hash_key ON "
        "control_plane.subscription_tokens USING btree (token_hash)",
    },
    "subscription_access_log": {
        "CREATE INDEX subscription_access_log_user_id_ts_idx ON ONLY "
        "control_plane.subscription_access_log USING btree (user_id, ts DESC)",
        "CREATE UNIQUE INDEX subscription_access_log_pkey ON ONLY "
        "control_plane.subscription_access_log USING btree (ts, id)",
    },
    "codes": {
        "CREATE INDEX codes_batch_id_idx ON control_plane.codes USING btree (batch_id)",
        "CREATE UNIQUE INDEX codes_code_hash_key ON control_plane.codes USING btree (code_hash)",
        "CREATE UNIQUE INDEX codes_pkey ON control_plane.codes USING btree (id)",
    },
    "code_redemptions": {
        "CREATE INDEX code_redemptions_code_id_user_id_idx ON control_plane.code_redemptions "
        "USING btree (code_id, user_id)",
        "CREATE INDEX code_redemptions_user_id_idx ON control_plane.code_redemptions USING btree "
        "(user_id)",
        "CREATE UNIQUE INDEX code_redemptions_pkey ON control_plane.code_redemptions USING btree "
        "(id)",
    },
    "orders": {
        "CREATE INDEX orders_user_id_idx ON control_plane.orders USING btree (user_id)",
        "CREATE UNIQUE INDEX orders_pkey ON control_plane.orders USING btree (id)",
    },
    "payments": {
        "CREATE INDEX payments_order_id_idx ON control_plane.payments USING btree (order_id)",
        "CREATE UNIQUE INDEX payments_pkey ON control_plane.payments USING btree (id)",
    },
}

SCHEMA_SUBSCRIPTIONS_TABLES = list(EXPECTED_COLUMNS)
# balance_entries — журнал изменений баланса: приложению только чтение и вставка (§4.6, R-38).
EXPECTED_GRANTS_BY_TABLE: dict[str, dict[str, set[str]]] = {
    "balance_entries": {"app_rw": {"SELECT", "INSERT"}, "app_backup": {"SELECT"}},
}
SEQUENCES = ["balance_entries_id_seq", "subscription_access_log_id_seq"]
EXPECTED_SEQUENCE_GRANTS: dict[str, set[str]] = {"app_rw": {"USAGE", "SELECT"}, "app_backup": set()}


async def test_catalog_matches_data_model(pg_dsn: str) -> None:
    """Девять таблиц группы, частичный UNIQUE токенов, партиционирование журнала обращений."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        for table in SCHEMA_SUBSCRIPTIONS_TABLES:
            await assert_table_matches(
                conn,
                table,
                EXPECTED_COLUMNS[table],
                EXPECTED_CONSTRAINTS[table],
                EXPECTED_INDEXES[table],
            )
        partition_key = await conn.fetchval(
            "select pg_get_partkeydef($1::regclass)", f"{SCHEMA}.subscription_access_log"
        )
        assert partition_key == "RANGE (ts)", "subscription_access_log — партиции по суткам (§4.5)"
        sequence = await conn.fetchrow(
            "select s.seqtypid::regtype::text as type, d.refobjid::regclass::text as tbl, "
            "a.attname from pg_sequence s join pg_depend d on d.objid = s.seqrelid "
            "and d.deptype = 'a' join pg_attribute a on a.attrelid = d.refobjid "
            "and a.attnum = d.refobjsubid where s.seqrelid = $1::regclass",
            f"{SCHEMA}.subscription_access_log_id_seq",
        )
        assert sequence is not None and tuple(sequence) == (
            "bigint",
            "subscription_access_log",
            "id",
        )
        typed = await conn.fetch(
            "select table_name, column_name, numeric_precision, numeric_scale, "
            "character_maximum_length from information_schema.columns where table_schema = $1 "
            "and ((table_name in ('orders', 'payments') and column_name in ('amount', 'currency')) "
            "or (table_name = 'subscription_access_log' and column_name = 'country'))",
            SCHEMA,
        )
        assert {tuple(r) for r in typed} == {
            ("orders", "amount", 12, 2, None),
            ("orders", "currency", None, None, 3),
            ("payments", "amount", 12, 2, None),
            ("payments", "currency", None, None, 3),
            ("subscription_access_log", "country", None, None, 2),
        }
    finally:
        await conn.close()


@pytest.mark.parametrize("table", SCHEMA_SUBSCRIPTIONS_TABLES)
async def test_app_rw_privileges(pg_dsn: str, table: str) -> None:
    """TC-UNIT-01: app_rw — DML (для balance_entries только SELECT, INSERT), app_backup — SELECT."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        expected = EXPECTED_GRANTS_BY_TABLE.get(table, EXPECTED_TABLE_GRANTS)
        assert await table_grants(conn, table) == expected
    finally:
        await conn.close()


@pytest.mark.parametrize("sequence", SEQUENCES)
async def test_sequence_privileges(pg_dsn: str, sequence: str) -> None:
    """Последовательности группы по умолчаниям §4.6: app_rw — USAGE и SELECT, app_backup — ничего.
    Для identity-столбца balance_entries USAGE на вставку не требуется — ассерт фиксирует ACL,
    а не право вставки; nextval-столбец subscription_access_log без USAGE вставки не примет."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        actual = {
            role: {
                priv
                for priv in ("USAGE", "SELECT", "UPDATE")
                if await conn.fetchval(
                    "select has_sequence_privilege($1, $2, $3)", role, f"{SCHEMA}.{sequence}", priv
                )
            }
            for role in EXPECTED_SEQUENCE_GRANTS
        }
        assert actual == EXPECTED_SEQUENCE_GRANTS, sequence
    finally:
        await conn.close()
