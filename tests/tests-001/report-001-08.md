# Отчёт о проверке — задача 001.08 «Схема: учёт трафика, гранты, адреса, функции обслуживания партиций»

Дата: 2026-09-09 (раунд 2 после ревью: timezone функций, граница срока хранения, откат без чужих
партиций, NULL, CHECK реестра, `--break-lock` — см. «Раунд 2»). Стенд: VM (`ssh vm`), PostgreSQL
18.6, схема `control_plane`;
`_yoyo_migration` → `0001`, `040`, `050`, `060`, `070`, `080`. Тесты — с рабочей машины против базы
стенда (`PG_DSN` под `app_rw`, `MIGRATE_DSN` под `app_migrate` → VM:15432).

## Регрессия

- `make check` (nvm use 24, `PG_DSN`/`MIGRATE_DSN`/`REDIS_URL` → стенд) → код 0: ruff, ruff format,
  mypy strict (31 файл), pytest **80 passed** (001.03 — 5, 001.04 — 11, 001.05 — 10, bootstrap — 3,
  001.06 — 18, 001.07 — 15, 001.08 — 18), go, web.
- `cd control-plane && pytest tests/unit/db -k 'schema_accounting'` → 14 passed.
- `app.cli migrate`: «применено — 1, всего в источнике — 6»; `--rollback` → «откачено — 1»;
  повторный `migrate` → «применено — 1». Стенд после `vm-sync` и `up -d --build api`: api при старте
  «применено — 0, всего в источнике — 6»; `pg_proc.proconfig` обеих функций —
  `{"search_path=control_plane, pg_temp", TimeZone=UTC}`.

## Сквозные тесты (`tests/e2e/test_schema_accounting.py`)

1. **TC-E2E-01** `test_migration_080_apply_rollback_reapply` (тело —
   `check_rollback_keeps_foreign_partitions`, в `finally` — повторный `migrate` и удаление
   партиций, чтобы падение посередине не каскадировало на соседние тесты): после `migrate`
   одиннадцать таблиц есть; `ensure_partitions(0)` создаёт пять партиций текущих суток, в
   `auth_events` вставляется строка;
   `rollback_through("080_schema_accounting")` — таблиц группы нет, функций `ensure_partitions`,
   `drop_expired_partitions`, `traffic_hourly_only_grows` нет, таблицы 070/060 на месте, три партиции
   чужих таблиц (`auth_events`, `subscription_access_log`, `node_metrics`) присоединены и строка
   `auth_events` на месте; повторный `migrate` возвращает группу; партиции теста удаляются.
2. **TC-E2E-02** `test_constraints_and_privileges` (под `app_rw`, откатываемая транзакция): дубль
   `(node_id, counter_epoch, report_seq)` → `UniqueViolationError` (R-21); `period_end = period_start`
   и отрицательные байты → `CheckViolationError`; чужая нода → FK; `traffic_lines` без партиции →
   `CheckViolationError`; `UPDATE`/`DELETE traffic_lines`, `DELETE traffic_hourly`, `UPDATE`/`INSERT
   partition_policies` → `InsufficientPrivilegeError`; дубли PK `traffic_daily`,
   `node_interface_hourly`, `user_online_ips`, `user_blocked_ips` → `UniqueViolationError`;
   `traffic_daily` с отрицательным значением, `traffic_gaps` с `gap_end = gap_start`,
   `quota_grants` с отрицательным грантом → `CheckViolationError`; `traffic_gaps` и `quota_grants` с
   чужими ссылками → FK; `reconciliation_runs.kind` вне enum → ошибка.
3. `test_ensure_and_drop_partitions` (под `app_rw`): `ensure_partitions(1)` → 10 (по две суточные
   партиции на пять таблиц реестра, имена `<таблица>_pYYYYMMDD`), повтор → 0, `days_ahead = 400` →
   `RaiseError`; владелец партиций `app_owner`; ACL партиций: `traffic_lines_*` — `app_rw=ar`,
   `traffic_hourly_*` — `app_rw=arw`, остальные — `arwd`; на партиции `traffic_hourly` под `app_rw`:
   рост значения принят, уменьшение → `CheckViolationError` (триггер), `DELETE` →
   `InsufficientPrivilegeError`; граница срока хранения (`assert_retention_boundary`, сутки из базы
   `(now() at time zone 'UTC')::date`, `cutoff = today − 14`): партиции `traffic_lines` суток
   2000-01-01, `cutoff − 1` и `cutoff` созданы `app_owner` → `drop_expired_partitions()` = 2, сутки
   `cutoff` остаются, повтор = 0; `ensure_partitions(NULL)` → `RaiseError`; после теста партиций 0.
4. `test_maintenance_uses_utc_regardless_of_session_timezone`: под `SET timezone = 'Etc/GMT+12'`
   (UTC−12) и `'Etc/GMT-14'` (UTC+14) — в любой момент суток хотя бы в одной из зон локальная дата
   отличается от UTC — `ensure_partitions(0)` создаёт партиции суток UTC, граница срока хранения
   не сдвигается (та же `assert_retention_boundary`); партиции удаляются после каждой зоны.
5. `tests/e2e/test_migrations.py::test_migrate_break_lock` (сверх задачи, по замечанию ревью):
   строка в `yoyo_lock` от `app_migrate` → `migrate` код 1, «база заблокирована — Process 0 has
   locked … ; если процесса нет: --break-lock» (10 с таймаута yoyo); `migrate --break-lock` →
   код 0, «замок yoyo снят», «применено — 0»; `count(*) from yoyo_lock` = 0 — ассерт теста.

## Модульные тесты (`tests/unit/db/test_schema_accounting.py`, 14 тестов)

- `test_catalog_matches_data_model`: колонки, ограничения, индексы 11 таблиц по §4.2.5/§4.4 с
  объявленными отклонениями (в том числе `CHECK` реестра: `table_name ~ '^[a-z_]+$'`,
  `revoke_from_app_rw <@ {SELECT, INSERT, UPDATE, DELETE}`; `CHECK (estimated_bytes >= 0)` без
  `IS NULL OR`); `RANGE (period_start)` / `RANGE (hour_start)`; реестр = сроки §4.5 и запреты
  §4.6; триггер `traffic_hourly_only_grows` включён; `delta_pct numeric(6,3)`.
- `test_app_rw_privileges[<таблица>]` ×11: `traffic_lines` — `{SELECT, INSERT}`, `traffic_hourly` —
  `{SELECT, INSERT, UPDATE}`, `partition_policies` — `{SELECT}`, остальные — DML; `app_backup` —
  `{SELECT}`.
- `test_maintenance_functions[<функция>]` ×2: владелец `app_owner`, `SECURITY DEFINER`,
  `proconfig` ровно `["search_path=control_plane, pg_temp", "TimeZone=UTC"]`, возвращает
  `integer`, `EXECUTE` у `app_rw`, не у `app_backup` и не у PUBLIC.

## Посадки стражей (до ревью)

| Посадка | Результат |
| :--- | :--- |
| без `REVOKE UPDATE, DELETE ON traffic_lines` | `test_app_rw_privileges[traffic_lines]` красный + e2e |
| PK `traffic_hourly` без `multiplier_milli` | «traffic_hourly: ограничения расходятся» |
| триггер без проверки `billable_bytes` | e2e: `DID NOT RAISE CheckViolationError` (уменьшение принято) |
| `ensure_partitions` без REVOKE на партициях | e2e: «traffic_hourly_p…: без DELETE у app_rw» (`app_rw=arwd`) |
| функция без `SECURITY DEFINER` | unit: «SECURITY DEFINER (§4.6)»; e2e: `permission denied for schema` под `app_rw` |
| срок `traffic_lines` 90 вместо 14 | «реестр партиций — сроки §4.5 и запреты §4.6» |
| откат без `DROP FUNCTION ensure_partitions` | тест отката красный («функции … удалены откатом», 1 == 0); остаток снесён |

Файлы восстановлены, 78 passed, партиций на стенде 0.

## Раунд 2 — правки по ревью и их проверка

Ревью раунда 1 (`sarcasmotron-001-08b`, REJECTED): функции без закреплённого `timezone`, граница
срока хранения без стража, откат уничтожал партиции чужих таблиц, `ensure_partitions(NULL)` → 0,
реестр без `CHECK`, гонка `to_regclass`, дата в e2e с машины, стиль.

- **timezone** (`080_schema_accounting.sql`): обе функции — `SET search_path = control_plane,
  pg_temp` + `SET timezone = 'UTC'`; сутки и граница срока не зависят от сессии вызывающего (R-52).
  Стражи: unit — точный `proconfig`; e2e — `test_maintenance_uses_utc_regardless_of_session_timezone`.
- **граница срока хранения**: комментарий функции фиксирует правило `d + 1 <= current_date −
  retention_days` (остаются ровно `retention_days` последних полных суток); страж
  `assert_retention_boundary` (сутки `cutoff − 1` уходят, `cutoff` остаются) в двух e2e-тестах.
- **откат** (`080_schema_accounting.rollback.sql`): DO-блок удалён; откат снимает только объекты
  080, партиции `auth_events`/`subscription_access_log`/`node_metrics` остаются присоединёнными с
  данными (страж: строка `auth_events` после отката на месте). Критерий «откат удаляет только
  объекты этой миграции» теперь выполняется буквально; контракт задачи исправлен.
- **NULL**: `IF days_ahead IS NULL OR … THEN RAISE`; страж `ensure_partitions(NULL::int)` →
  `RaiseError`.
- **реестр**: `CHECK (table_name ~ '^[a-z_]+$')`, `CHECK (revoke_from_app_rw <@ ARRAY['SELECT',
  'INSERT', 'UPDATE', 'DELETE'])` — в золотой спецификации unit-теста.
- **гонка**: `CREATE TABLE IF NOT EXISTS … PARTITION OF` после проверки `to_regclass`.
- **дата в e2e**: `utc_today(conn)` = `(now() at time zone 'UTC')::date` из базы, не `date.today()`
  машины и не `current_date` сессии.
- **сверх задачи — `migrate --break-lock`** (`app/cli.py`): замечание ревью об out-of-scope
  дефекте. `LockTimeout` → одна строка «база заблокирована — …; если процесса нет: --break-lock»,
  код 1; флаг вызывает `backend.break_lock()` перед взятием замка. `--break-lock` снимает только
  замок: клиент, умерший между транзакцией отката и снятием отметки, оставляет отметку
  применённой миграции без объектов, и повторный `migrate` — no-op (так стенд чинился вручную в
  раунде 1 через `unmark_migrations`). Это свойство `app.cli migrate`, не 080 — заведено
  **WI-3** (`docs/backlog/wi-3-migrate-interrupted-rollback-leaves-mark-without-objects.md`:
  `--unmark <id>` и/или проверка ключевого объекта при старте; рекомендовано вместе с 001.10).
- Стиль: `CHECK (estimated_bytes >= 0)` без `IS NULL OR`; `DROP TABLE traffic_hourly` снимает
  триггер, затем `DROP FUNCTION`; DO-блока с именами таблиц больше нет. `GRANT EXECUTE` явный
  оставлен: в 0001 `EXECUTE` отозван у PUBLIC глобально, право `app_rw` даётся именно им.

Посадки раунда 2 (по одной, файлы восстановлены после каждой; `pytest test_schema_accounting`
e2e + unit):

| Посадка | Результат |
| :--- | :--- |
| `ensure_partitions` без `SET timezone` | `test_maintenance_uses_utc…`: `('Etc/GMT+12', {…_p20260908})` ≠ `{…_p20260909}` |
| `IF part_day <= current_date − retention_days` (сдвиг на сутки) | `assert 3 == 2` — сутки `cutoff` удалены |
| откат снова с DO-блоком по чужим партициям | «откат оставляет партиции чужих таблиц присоединёнными»: `set()` |
| без `IS NULL` в проверке `days_ahead` | `DID NOT RAISE RaiseError` |
| `--break-lock` без `backend.break_lock()` | `test_migrate_break_lock`: второй запуск код 1 «база заблокирована» |

После посадок: 80 passed, `yoyo_lock` пуст, партиций на стенде 0, `make check` → 0.

### Вердикт раунда 2

`sarcasmotron-001-08b`: **APPROVED**. Проверено ревьюером на живой базе: `proconfig` с
`TimeZone=UTC`, три `CHECK` реестра, откат без DO-блока сохраняет партиции и строки, `NULL` →
`RaiseError`; три посадки ревьюера красные (`SET timezone` снят только с `drop_expired_partitions`
→ `assert 1 == 2` под UTC−12; `part_day + 2 <=` → `assert 1 == 2` в двух e2e; `TRUNCATE auth_events`
в откате → «откат не трогает данные чужих таблиц», `assert 0 == 1`); `make check` → 0, 80 passed.
Замечания низкой значимости закрыты после вердикта без изменения поведения: (1) число,
возвращаемое `ensure_partitions`, при гонке двух вызовов засчитывает проигравшему партицию
победителя — оговорено в комментарии функции и контракте задачи; (2) `drop_all_partitions` и
повторный `migrate` в `finally` теста отката; (3) `test_migrate_break_lock` утверждает пустой
`yoyo_lock`; (4) формулировка про «`--break-lock` возвращает базу к „всё применено“» исправлена
(см. выше) и заведён WI-3. Регрессия после правок: `pytest test_schema_accounting` e2e + unit и
`test_migrations` → 23 passed; `make check` → 0, 80 passed.

## Критерии приёмки

- [x] Все таблицы группы существуют с типами и ограничениями §4.2 — исполняемая сверка
- [x] PK `traffic_reports (node_id, counter_epoch, report_seq)` — сверка и дубль под `app_rw`
- [x] PK `traffic_hourly` включает `billing_group_id` и `multiplier_milli` — сверка и посадка
- [x] Функции `SECURITY DEFINER` `ensure_partitions(days_ahead int)` и `drop_expired_partitions()`
      принадлежат `app_owner` — `pg_proc`, посадка без `SECURITY DEFINER`
- [x] `app_rw` имеет `UPDATE` на `traffic_hourly` и не имеет на `traffic_lines` — `has_table_privilege`
      и `InsufficientPrivilegeError` под `app_rw`; то же на партициях
- [x] Откат удаляет только объекты этой миграции — 070/060 на месте; функции и триггер сняты;
      партиции и строки чужих таблиц не тронуты

## Отклонения от описания задачи

- Реестр `partition_policies`, триггер монотонности, `CHECK`, умолчания, индексы, политика FK —
  объявлены поимённо в задаче и заголовке миграции.
- Сутки функций обслуживания — UTC независимо от сессии (`SET timezone`); модель §4.5 timezone не
  оговаривает, выбор зафиксирован в задаче (R-52).
- Партиции чужих таблиц, созданные `ensure_partitions`, откат 080 не трогает (раунд 1 удалял).
- Сверх задачи: `app.cli migrate --break-lock` и обработка `LockTimeout` (`app/cli.py`,
  `tests/e2e/test_migrations.py`).
- Модуль TC-UNIT-01 — `tests/unit/db/test_schema_accounting.py`; права — `has_table_privilege`.
