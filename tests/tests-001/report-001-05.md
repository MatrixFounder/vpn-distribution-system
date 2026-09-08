# Отчёт о проверке — задача 001.05 «Схема: тарифы, группы, коэффициенты»

Дата: 2026-09-08 (раунд 3: раунд 2 — каскады, откат ровно до 050, схема `control_plane`; раунд 3 —
страж уровня схемы). Стенд: VM (`ssh vm`), PostgreSQL 18.6; `_yoyo_migration` →
`0001`, `040`, `050`. Тесты — с рабочей машины против базы стенда (`PG_DSN` под `app_rw`,
`MIGRATE_DSN` под `app_migrate` → VM:15432).

## Регрессия

- `make check` (nvm use 24, `PG_DSN`/`MIGRATE_DSN`/`REDIS_URL` → стенд) → код 0: ruff, ruff format,
  mypy strict (25 файлов), pytest **28 passed** (001.03 — 4, 001.04 — 11, 001.05 — 10, bootstrap-схема
  — 3), go, web.
- `cd control-plane && pytest tests/unit/db -k 'schema_catalog'` → 8 passed (сверка каталога + права
  на семь таблиц).
- Стенд: `migrate` при появлении файла → «применено — 1, всего в источнике — 3»; откат/повтор
  через `app.cli` в тестах.

## Сквозные тесты (`tests/e2e/test_schema_catalog.py`)

1. **TC-E2E-01** `test_migration_050_apply_rollback_reapply`: после `migrate` семь таблиц группы
   есть; `rollback_through("050_schema_catalog")` — ровно столько шагов `--rollback`, сколько миграций
   стоит над 050 вместе с ней (при 0001+040+050 — один), каждый шаг снимает ровно одну; таблиц
   группы нет, таблицы 040 на месте (050 от 040 не зависит); повторный `migrate` возвращает группу.
2. **TC-E2E-02** `test_constraints_reject_bad_rows` (под `app_rw`, в откатываемой транзакции):
   - `plans`: дубль `name` → `UniqueViolationError`; `duration_days = 0` → `CheckViolationError`;
     `status = 'paused'` → `InvalidTextRepresentationError`; `traffic_limit_bytes` без значения →
     NULL (Unlimited);
   - `plan_protocols`: дубль PK `(plan_id, profile)` → `UniqueViolationError`; профиль вне enum →
     ошибка; чужой `plan_id` → `ForeignKeyViolationError`;
   - `access_groups` дубль `name`; `plan_access_groups` с чужой группой → FK;
   - `billing_group_multipliers`: коэффициенты −100, 50, 150, 10100 → `CheckViolationError`;
     `valid_to = valid_from` и `valid_to < valid_from` → `CheckViolationError`; чужая группа → FK;
     интервалы [0,10) и [10,20) смежные — приняты (границы 0 и 10000 включительно); [5,15) →
     `ExclusionViolationError`; открытый [15,∞) при существующем [10,20) → `ExclusionViolationError`;
     открытый [20,∞) принят; ещё один открытый [100,∞) → `ExclusionViolationError`; другая группа с
     пересекающимся интервалом — принята;
   - `node_billing_assignments`: [0,10) и открытый [10,∞) приняты; [5,15) и [30,∞) →
     `ExclusionViolationError`; переопределение 250 и 10100 → `CheckViolationError`; чужая группа →
     FK; другая нода — принята (FK на `nodes` до 060 нет);
   - удаление тарифа уносит `plan_protocols` и `plan_access_groups` (CASCADE); группу с историей
     коэффициентов удалить нельзя (FK без каскада, история бессрочна §4.5).

## Модульные тесты (`tests/unit/db/test_schema_catalog.py`, 8 тестов)

- `test_catalog_matches_data_model`: колонки (тип, NULL, умолчание), ограничения (PK, UNIQUE, FK с
  каскадами, CHECK, EXCLUDE) и индексы семи таблиц — по §4.2.2/§4.4 с объявленными отклонениями;
  `price_amount numeric(12,2)`, `price_currency char(3)`.
- `test_app_rw_privileges[<таблица>]` ×7: `app_rw` ровно `{SELECT, INSERT, UPDATE, DELETE}`,
  `app_backup` `{SELECT}`.

## Посадки стражей (до ревью)

| Посадка | Результат |
| :--- | :--- |
| без `EXCLUDE` у `billing_group_multipliers` | «ограничения расходятся с §4.2» + `DID NOT RAISE ExclusionViolationError` |
| `CHECK` без кратности 100 | «ограничения расходятся» + `DID NOT RAISE CheckViolationError` (150 принято) |
| `numeric` без точности | «numeric(12,2), char(3) (§4.2.2)» |
| откат без `DROP TABLE plans` | тест отката красный (`relation "plans" already exists` при повторном apply); остаток снесён, база пересобрана |
| `REVOKE SELECT ON plans FROM app_backup` | `test_app_rw_privileges[plans]` красный |

Файлы восстановлены, стенд пересобран (`--rollback-all` + `migrate` → «применено — 3»), 25 passed.

## Схема `control_plane` (решение пользователя в ходе задачи)

Все объекты Control Plane переведены из `public` в схему `control_plane`: bootstrap `roles.sql`
создаёт её (владелец `app_owner`), даёт `USAGE` ролям приложения и `USAGE, CREATE` роли
`app_migrate`, задаёт `search_path = control_plane` трём ролям; 0001 устанавливает расширения
`SCHEMA control_plane` и умолчания привилегий `IN SCHEMA control_plane`; каждая миграция и откат
начинаются с `SET LOCAL ROLE app_owner; SET LOCAL search_path TO control_plane` (статический страж
проверяет оба оператора). Стенд пересобран с чистого тома: `pg_namespace` → `control_plane|app_owner`,
`public|pg_database_owner`; 17 отношений (таблицы, последовательность, таблицы yoyo) — все в
`control_plane`, в `public` — 0; `btree_gist|control_plane`, `citext|control_plane`;
`pg_db_role_setting` → `search_path=control_plane` у `app_rw`, `app_migrate`, `app_backup`; api при
старте применил три миграции. Тесты сверяют каталоги по схеме `control_plane`; дефолт
`nextval('auth_events_id_seq')` рендерится без схемы, потому что она в `search_path` роли.

## Раунд 3 — страж уровня схемы

Ревью раунда 2 показало, что после переезда в `control_plane` право `USAGE`/`CREATE` на схему стало
несущим, а тесты его не видели (снятый `USAGE` у `app_backup` и выданный `CREATE` `app_rw` оставляли
25 тестов зелёными). Добавлен `tests/unit/db/test_bootstrap_schema.py`: владелец схемы `app_owner`;
`has_schema_privilege` — `app_rw {USAGE}`, `app_backup {USAGE}`, `app_migrate {USAGE, CREATE}`
(`app_rw`/`app_backup` без `CREATE` и на `public`); `pg_db_role_setting` — `search_path=control_plane`
у трёх ролей и `current_schema()` под `app_rw`; в `public` 0 отношений, перечислений, функций и
расширений. `roles.sql` дополнен `ALTER SCHEMA control_plane OWNER TO app_owner` (на случай схемы,
созданной ранее). Посадки: `REVOKE USAGE … FROM app_backup` → красный; `GRANT CREATE … TO app_rw`
→ красный; `RESET search_path` у `app_backup` → красный; `create table public._stray` → красный;
после восстановления 28 passed. Комментарии про `public` в `test_migrations.py` и `.AGENTS.md`
переписаны.

## Критерии приёмки

- [x] Все таблицы группы существуют с типами и ограничениями §4.2 — исполняемая сверка
      `test_catalog_matches_data_model`
- [x] `EXCLUDE USING gist` на интервалах двух таблиц истории действует — пересечения отклоняются,
      смежные и чужие интервалы принимаются (обе таблицы)
- [x] `CHECK` на `multiplier_milli`: 0…10000, кратно 100 — границы 0 и 10000 приняты, −100, 50, 150,
      10100 отклонены; то же для `multiplier_override_milli`
- [x] Откат удаляет только объекты этой миграции — таблицы 040 и типы 0001 остаются

## Отклонения от описания задачи

- `node_billing_assignments.node_id` без FK: `nodes` создаёт 001.06, FK добавляет миграция 060
  (записано в контракт 001.06).
- Сверх §4.2.2: умолчания, `CHECK` порядка границ интервалов, индексы обратного поиска по FK —
  объявлены в задаче и заголовке миграции.
- Модуль TC-UNIT-01 — `tests/unit/db/test_schema_catalog.py`; проверка прав —
  `has_table_privilege`; общий код сверки вынесен в `tests/unit/db/_introspect.py`, тест 001.04
  переведён на него.
- Откат в TC-E2E-01 — ровно столько шагов, сколько нужно снять 050 и всё новее
  (`rollback_through` в `tests/e2e/_cli.py`; каждый шаг обязан снять ровно одну миграцию), так как
  `--rollback` снимает новейшую миграцию; тест 001.04 переведён на тот же помощник, оба теста
  переименованы в `…_apply_rollback_reapply`.
- `ON DELETE CASCADE` на `plan_protocols` и `plan_access_groups` (модель каскадов не задаёт) —
  объявлено в задаче и заголовке миграции; в 001.04 задним числом объявлены каскады
  `admin_recovery_codes` и `email_tokens`.
