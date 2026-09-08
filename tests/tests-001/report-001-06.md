# Отчёт о проверке — задача 001.06 «Схема: парк нод, inbound, состояние и команды»

Дата: 2026-09-08 (раунд 2 после ревью: объявления отклонений, CHECK на node_ip_history). Стенд: VM (`ssh vm`), PostgreSQL 18.6, схема `control_plane`;
`_yoyo_migration` → `0001`, `040`, `050`, `060`. Тесты — с рабочей машины против базы стенда
(`PG_DSN` под `app_rw`, `MIGRATE_DSN` под `app_migrate` → VM:15432).

## Регрессия

- `make check` (nvm use 24, `PG_DSN`/`MIGRATE_DSN`/`REDIS_URL` → стенд) → код 0: ruff, ruff format,
  mypy strict (27 файлов), pytest **46 passed** (001.03 — 4, 001.04 — 11, 001.05 — 10, bootstrap — 3,
  001.06 — 18), go, web.
- `cd control-plane && pytest tests/unit/db -k 'schema_nodes'` → 15 passed (сверка каталога, права на
  13 таблиц, права на последовательность).
- `app.cli migrate`: «применено — 1, всего в источнике — 4»; `--rollback` → «откачено — 1»;
  повторный `migrate` → «применено — 1».

## Сквозные тесты (`tests/e2e/test_schema_nodes.py`)

1. **TC-E2E-01** `test_migration_060_apply_rollback_reapply`: после `migrate` тринадцать таблиц есть;
   `rollback_through("060_schema_nodes")` (один шаг при 0001+040+050+060) — таблиц группы нет,
   ограничение `node_billing_assignments_node_id_fkey` снято, таблицы 050 и 040 на месте; повторный
   `migrate` возвращает группу.
2. **TC-E2E-02** `test_constraints_reject_bad_rows` (под `app_rw`, откатываемая транзакция):
   дубль `nodes.code` → `UniqueViolationError`; нода без `billing_group_id` → `NotNullViolationError`
   (R-18); чужая группа → FK; `bandwidth_mbps = 0`, `max_conn_per_ip = 0`, `multiplier_milli = 150` →
   `CheckViolationError`; `status` вне enum → ошибка; `inbounds`: дубль `(node_id, profile)` и
   `(node_id, port)` → `UniqueViolationError`, порт 70000 и 0 → `CheckViolationError`;
   `inbound_secrets` с чужим inbound → FK; удаление ноды с inbound → FK (RESTRICT); удаление inbound
   уносит секрет (CASCADE); `node_identities`: дубль `cert_fingerprint` → `UniqueViolationError`,
   чужая нода → FK; `node_user_credentials`: дубль `(user_id, node_id)` → `UniqueViolationError`,
   чужой пользователь → FK; `node_user_state`: дубль PK, `state` вне enum; удаление группы доступа
   уносит `node_access_groups` (CASCADE); `commands`: тип вне enum, чужая нода → FK;
   `node_country_availability` и `node_config_versions`: дубли PK; `node_ip_history.id` —
   identity под `app_rw`; `node_metrics` без партиции → `CheckViolationError`.
3. `test_node_metrics_insert_as_app_rw`: `app_owner` создаёт партицию на 2000-01-01, `app_rw`
   вставляет метрики, дубль `(node_id, ts)` → `UniqueViolationError`; партиция удалена вместе со
   строками (после теста партиций 0, нод 0).

## Модульные тесты (`tests/unit/db/test_schema_nodes.py`, 15 тестов)

- `test_catalog_matches_data_model`: колонки, ограничения, индексы 13 таблиц по §4.2.3/§4.4 с
  объявленными отклонениями; `pg_get_partkeydef(node_metrics) = RANGE (ts)`; FK
  `node_billing_assignments_node_id_fkey` = `FOREIGN KEY (node_id) REFERENCES nodes(id)`;
  `monthly_cost numeric(12,2)`, `traffic_overage_cost numeric(12,4)`, `currency char(3)`,
  `country char(2)`.
- `test_app_rw_privileges[<таблица>]` ×13: `app_rw` ровно DML, `app_backup` `{SELECT}`.
- `test_sequence_privileges`: `node_ip_history_id_seq` — `app_rw` USAGE+SELECT, `app_backup` ничего.
- Ожидание в `test_schema_catalog.py` дополнено FK на `nodes`; тест 001.05 `check_assignment_history`
  переведён на реальные ноды (`insert_node`), чужой `node_id` → `ForeignKeyViolationError`.

## Посадки стражей (до ревью)

| Посадка | Результат |
| :--- | :--- |
| без `UNIQUE (node_id, port)` | «inbounds: ограничения расходятся» + `DID NOT RAISE UniqueViolationError` |
| `billing_group_id` без `NOT NULL` | «nodes: колонки расходятся» + `DID NOT RAISE NotNullViolationError` |
| `node_metrics` без `PARTITION BY` | «node_metrics: индексы расходятся» + `DID NOT RAISE CheckViolationError` |
| без отложенного FK (и без его снятия в откате) | «060 добавляет отложенный FK» + «node_billing_assignments: ограничения расходятся» + `DID NOT RAISE ForeignKeyViolationError` |
| откат без `DROP CONSTRAINT` | откат падает (`cannot drop table nodes … depends on it`), тест отката красный |

Файлы восстановлены, 46 passed.

## Раунд 2 — правки по ревью

- Контракт задачи: умолчания перечислены поимённо (включая девять `now()`, `enabled true`,
  `resync_required false`, `client_spider_x ''`, лимиты 0); `node_ip_history.valid_to` объявлен
  nullable (действующий адрес — открытый интервал) и получил `CHECK (valid_to IS NULL OR valid_to >
  valid_from)` как у историй 050 — SQL, спецификация стража и e2e (`valid_to < valid_from` →
  `CheckViolationError`) дополнены; индекс `node_access_groups (access_group_id)` больше не значится
  «сверх модели» (он в §4.2.3/§4.4); связи с `nodes` описаны как NO ACTION (умолчание), а не RESTRICT.
- `tests/e2e/_db.py::insert_node`: адреса из 203.0.113.0/24 по счётчику (детерминированно),
  типы `uuid.UUID`.
- Стенд: `--rollback` + `migrate` с новым файлом 060 → «применено — 1, всего в источнике — 4»;
  `make check` → 0, pytest 46 passed.

## Критерии приёмки

- [x] Все таблицы группы существуют с типами и ограничениями §4.2 — исполняемая сверка
- [x] `nodes.billing_group_id NOT NULL` — сверка колонок и `NotNullViolationError` под `app_rw`
- [x] `UNIQUE (node_id, profile)`, `UNIQUE (node_id, port)` на `inbounds` — сверка и посадки
- [x] `node_metrics` партиционирована по суткам — `RANGE (ts)`; партиции — функция 001.08
- [x] Откат удаляет только объекты этой миграции — таблицы 050/040 и типы 0001 остаются, FK снят

## Отклонения от описания задачи

- Сверх §4.2.3: умолчания, `CHECK` порогов, индексы по FK, identity для `node_ip_history.id`,
  каскады только у `node_access_groups` и `inbound_secrets` — объявлены в задаче и заголовке миграции.
- Модуль TC-UNIT-01 — `tests/unit/db/test_schema_nodes.py`; права — `has_table_privilege`.
