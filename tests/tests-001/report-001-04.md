# Отчёт о проверке — задача 001.04 «Схема: учётные записи и аутентификация»

Дата: 2026-09-08 (раунд 3 после ревью: C-1, C-2 и находки логики закрыты в раунде 2, в раунде 3 исправлен отчёт). Стенд: VM (`ssh vm`), PostgreSQL 18.6, база мигрирована
`0001_extensions_roles_enums` + `040_schema_identity`. Тесты — с рабочей машины против базы стенда
(`PG_DSN` под `app_rw`, `MIGRATE_DSN` под `app_migrate` → VM:15432).

## Регрессия

- `make check` (nvm use 24, `PG_DSN`/`MIGRATE_DSN`/`REDIS_URL` → стенд) → код 0: ruff, ruff format,
  mypy strict (20 файлов), pytest **15 passed** (001.03 — 4, 001.04 — 11), go, web.
- Регрессия задачи через CLI yoyo (`yoyo.ini`, `MIGRATE_DSN=postgresql+psycopg://…`):
  `yoyo apply --batch` → 0; `yoyo rollback --batch` (один шаг) → 0, `yoyo list` → `A 0001`, `U 040`;
  `yoyo apply --batch` → 0.
- Старт api: после `--rollback` в контейнере и `up -d --force-recreate api` лог api —
  `migrate: применено — 1, всего в источнике — 2`; `_yoyo_migration` → `0001`, `040`.

## Раунд 2 — правки по ревью и посадки

- **C-1** (гейт прав не видел последовательность, никто не писал в `auth_events` под `app_rw`):
  `test_sequence_privileges` (`has_sequence_privilege`: `app_rw` — USAGE, SELECT; `app_backup` —
  ничего) и `test_auth_events_insert_as_app_rw` — `app_owner` создаёт партицию на 2000-01-01
  (вне окна планировщика 001.08), `app_rw` вставляет две строки (`id` из последовательности,
  последовательные), PK `(ts, id)` и `CHECK result` отклоняют, `kind` вне enum отклоняется,
  партиция удаляется вместе со строками. Посадка `REVOKE USAGE, SELECT ON SEQUENCE … FROM app_rw`
  → оба теста красные (2 failed), после `GRANT` — зелёные.
- **C-2** (нет исполняемой сверки со спецификацией): `tests/unit/db/test_schema_identity.py::
  test_catalog_matches_data_model` сверяет по каталогам колонки (тип, NULL, умолчание),
  ограничения (`pg_get_constraintdef`: PK, UNIQUE, FK с ON DELETE, CHECK), индексы (`indexdef`,
  включая предикат и `DESC`), `pg_get_partkeydef = RANGE (ts)`, последовательность (bigint,
  OWNED BY `auth_events.id`) с ожиданием по §4.2.1 и объявленными отклонениями. Посадки: без
  индекса `(user_id, ts DESC)` → «auth_events: индексы расходятся»; `admin_users.email text` →
  «admin_users: колонки расходятся»; FK без CASCADE → «email_tokens: ограничения расходятся»;
  PK `(id, ts)` → «auth_events: ограничения расходятся». Файлы восстановлены (хэши совпадают с
  копией на VM), 15 passed.
- **Высокая** (`CHECK (language)` против R-51/AC-22): `CHECK` снят с `users` и `admin_users`,
  `language text` с умолчанием `'en'`; тест вставляет `language = 'de'` и ожидает успех.
- **Средняя** (необъявленные добавки): умолчания, индекс `email_tokens (user_id)`, `CHECK
  (result)` объявлены в файле задачи, в заголовке миграции и здесь.
- **Средняя** (мёртвая команда регрессии): модуль `tests/unit/db/test_schema_identity.py`
  (`pytest tests/unit/db -k 'schema_identity'` → 7 passed; `pytest -k 'schema_identity'` →
  11 passed: 7 unit + 4 e2e); имя `test_app_rw_privileges` сохранено.
- **Низкие**: `--rollback` и `--rollback-all` взаимоисключающие (argparse group, проверено:
  «not allowed with argument»); пробы партиции — 2000-01-01, а не сегодня; проверяется
  `pg_get_partkeydef`, а не только `partstrat`; число перечислений — из `EXPECTED_ENUMS` по схеме
  `public`; docstring `run_migrate` и `conftest.py` обновлены.

## Сквозные тесты (`tests/e2e/test_schema_identity.py`)

1. **TC-E2E-01** `test_migration_040_apply_rollback_one_step`: после `migrate` таблицы `users`,
   `admin_users`, `admin_recovery_codes`, `email_tokens`, `auth_events` существуют; `auth_events`
   партиционирована по диапазону (`pg_get_partkeydef = 'RANGE (ts)'`), `id` —
   `nextval('auth_events_id_seq')`, `users.email` — `citext`, `uuidv7()` даёт версию 7.
   `migrate --rollback` → код 0, «откачено — 1»: таблиц группы нет, последовательность удалена
   (OWNED BY), 22 перечисления 0001 на месте. Повторный `migrate` → таблицы снова есть.
2. **TC-E2E-02** `test_constraints_reject_bad_rows` (под `app_rw`, во внешней транзакции с
   откатом — стенд без тестовых строк): `UNIQUE` на `users.email` с другим регистром (citext) →
   `UniqueViolationError`; `language = 'de'` принимается (R-51, AC-22: `CHECK` на язык нет);
   `status = 'frozen'` →
   `InvalidTextRepresentationError` (enum); `admin_recovery_codes` и `email_tokens` с чужим id →
   `ForeignKeyViolationError`; повтор `token_hash` → `UniqueViolationError`; `admin_users` без
   `role` → `NotNullViolationError`; удаление администратора уносит его коды (`ON DELETE CASCADE`).
3. `test_auth_events_no_partition_rejects_rows`: под `app_rw` строка на 2000-01-01 без партиции →
   `CheckViolationError` («no partition of relation found for row»); попытка — в откатываемой
   транзакции, при красном гейте строка на стенде не остаётся.
4. `test_auth_events_insert_as_app_rw`: см. раунд 2, C-1. После теста партиций у `auth_events` 0,
   строк в `users` 0.

## Модульные тесты (`tests/unit/db/test_schema_identity.py`, 7 тестов)

- `test_catalog_matches_data_model` — сверка со спецификацией §4.2.1 (см. раунд 2, C-2).
- `test_app_rw_privileges[<таблица>]` ×5: `has_table_privilege` для `app_rw` → ровно
  `{SELECT, INSERT, UPDATE, DELETE}` (без TRUNCATE/REFERENCES/TRIGGER), для `app_backup` →
  `{SELECT}`. `information_schema.role_table_grants` из сессии `app_rw` чужих прав не показывает —
  поэтому `has_table_privilege` (отражено в файле задачи). `app_migrate` наследует права владельца
  через членство в `app_owner`; владение объектами стережёт `test_migrations`.
- `test_sequence_privileges` — `auth_events_id_seq`: `app_rw` USAGE+SELECT, `app_backup` ничего.

## Критерии приёмки

- [x] Все таблицы группы существуют с типами и ограничениями §4.2 — исполняемая сверка
      `test_catalog_matches_data_model` (колонки, PK/UNIQUE/FK+CASCADE/CHECK, частичный индекс
      неиспользованных кодов, индекс `(user_id, ts DESC)`, ключ партиционирования)
- [x] `users.email` — `citext UNIQUE`
- [x] `auth_events` партиционирована по суткам (RANGE по `ts`; партиции — функция 001.08),
      `id` через `nextval`
- [x] Откат удаляет только объекты этой миграции (перечисления и расширения 0001 остаются)

## Отклонения от описания задачи

- `app.cli migrate --rollback` (откат на один шаг) добавлен для TC-E2E-01.
- Сверх §4.2.1: умолчания колонок, индекс `email_tokens (user_id)`, `CHECK (result)`; `language`
  без `CHECK` (R-51, AC-22). Всё объявлено в файле задачи и заголовке миграции.
- Модуль TC-UNIT-01 — `tests/unit/db/test_schema_identity.py` (команда регрессии задачи выбирает
  его по `-k`), функция `test_app_rw_privileges` сохранена.
- Проверка прав — `has_table_privilege` вместо `information_schema.role_table_grants` (см. выше).
- Права `app_rw`/`app_backup` не выдаются явно в миграции: их дают умолчания привилегий
  `app_owner` из 0001 (§4.6); проверено тестом на каждой таблице.
- Общие помощники тестов вынесены в `tests/e2e/_cli.py`, фикстура `migrate_env` — в `conftest.py`.
