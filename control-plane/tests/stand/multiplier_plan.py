"""План запросов разрешения коэффициента на заполненной истории (001.23) — подтверждение, что
``multiplier.resolve`` обслуживают ``EXCLUDE``-индексы обеих таблиц истории, а не перебор.

На пустых таблицах стенда (статистика ``reltuples = 0``) планировщик берёт что угодно, и план
ничего не доказывает. Поэтому скрипт в **одной транзакции** под владельцем схемы заводит 200
групп по 20 интервалов коэффициента, 2000 нод по 5 назначений, собирает статистику
(``ANALYZE`` — тоже в транзакции), печатает ``EXPLAIN (ANALYZE, BUFFERS)`` обоих запросов
``resolve`` и откатывает строки. Откат возвращает строки и ``pg_statistic``, но не
``relpages``/``reltuples`` в ``pg_class``: их ``ANALYZE`` пишет на месте, вне транзакции. Поэтому
после отката скрипт ещё раз выполняет ``ANALYZE`` трёх таблиц (на настоящих данных стенда) и
печатает число оставшихся строк с префиксом ``x23-`` (ожидается 0) и ``reltuples`` трёх таблиц.

Запуск из ``control-plane`` через туннель стенда (``deploy/scripts/stand-tunnel.sh start``)::

    MIGRATE_DSN=postgresql://app_migrate:app@127.0.0.1:15432/control_plane \\
        .venv/bin/python -m tests.stand.multiplier_plan
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os

import asyncpg
from app.domain.multiplier import _COVERING, _FIRST

FILL = """
insert into billing_groups (name) select 'x23-g' || g from generate_series(1, 200) g;
insert into billing_group_multipliers (billing_group_id, multiplier_milli, valid_from, valid_to)
  select b.id, 100 * (1 + (i % 50)), timestamptz '2026-01-01' + (i - 1) * interval '5 days',
         case when i = 20 then null else timestamptz '2026-01-01' + i * interval '5 days' end
  from billing_groups b, generate_series(1, 20) i where b.name like 'x23-g%';
insert into nodes (code, name, country, city, provider, public_ipv4, billing_group_id,
                   bandwidth_mbps, max_conn_per_ip)
  select 'x23-n' || n, 'x23-n' || n, 'JP', 'Tokyo', 'probe',
         ('198.18.' || (n / 250) || '.' || (n % 250))::inet,
         (select id from billing_groups where name = 'x23-g' || (1 + n % 200)), 1000, 8
  from generate_series(1, 2000) n;
insert into node_billing_assignments
  (node_id, billing_group_id, multiplier_override_milli, valid_from, valid_to)
  select x.id, (select id from billing_groups where name = 'x23-g' || (1 + (k * 7 + x.n) % 200)),
         case when k = 3 then 3000 else null end,
         timestamptz '2026-01-01' + (k - 1) * interval '20 days',
         case when k = 5 then null else timestamptz '2026-01-01' + k * interval '20 days' end
  from (select id, substr(code, 6)::int n from nodes where code like 'x23-n%') x,
       generate_series(1, 5) k;
"""


async def main() -> None:
    conn = await asyncpg.connect(os.environ["MIGRATE_DSN"].replace("+psycopg", "", 1))
    await conn.execute("set role app_owner")
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(FILL)
        await conn.execute(
            "analyze nodes; analyze node_billing_assignments; analyze billing_group_multipliers"
        )
        counts = await conn.fetchrow(
            "select (select count(*) from node_billing_assignments) as assignments, "
            "(select count(*) from billing_group_multipliers) as multipliers, "
            "(select count(*) from nodes) as nodes"
        )
        print("строк:", dict(counts))
        node_id = await conn.fetchval("select id from nodes where code = 'x23-n1000'")
        moment = dt.datetime(2026, 2, 15, 12, tzinfo=dt.UTC)
        for name, sql, args in (
            ("_COVERING", _COVERING, (node_id, moment)),
            ("_FIRST", _FIRST, (node_id,)),
        ):
            plan = await conn.fetch(
                "explain (analyze, buffers, costs off, summary on) " + sql, *args
            )
            print(f"== {name}")
            print("\n".join(row[0] for row in plan))
    finally:
        await tx.rollback()
    await conn.execute(
        "analyze nodes; analyze node_billing_assignments; analyze billing_group_multipliers"
    )
    left = await conn.fetchval("select count(*) from nodes where code like 'x23-%'")
    print("после отката строк x23-:", left)
    stats = await conn.fetch(
        "select relname, reltuples from pg_class where relname in "
        "('nodes', 'node_billing_assignments', 'billing_group_multipliers') order by relname"
    )
    print("reltuples после ANALYZE:", {row["relname"]: row["reltuples"] for row in stats})
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
