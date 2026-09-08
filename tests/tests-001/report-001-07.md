# Отчёт о проверке — задача 001.07 «Схема: подписки, баланс, токены, коды»

Дата: 2026-09-08 (раунд 2 после ревью: реальный ассерт каскада `subscriptions`). Стенд: VM (`ssh vm`), PostgreSQL 18.6, схема `control_plane`;
`_yoyo_migration` → `0001`, `040`, `050`, `060`, `070`. Тесты — с рабочей машины против базы стенда
(`PG_DSN` под `app_rw`, `MIGRATE_DSN` под `app_migrate` → VM:15432).

## Регрессия

- `make check` (nvm use 24, `PG_DSN`/`MIGRATE_DSN`/`REDIS_URL` → стенд) → код 0: ruff, ruff format,
  mypy strict (29 файлов), pytest **61 passed** (001.03 — 4, 001.04 — 11, 001.05 — 10, bootstrap — 3,
  001.06 — 18, 001.07 — 15), go, web.
- `cd control-plane && pytest tests/unit/db -k 'schema_subscriptions'` → 12 passed.
- `app.cli migrate`: «применено — 1, всего в источнике — 5»; `--rollback` → «откачено — 1»;
  повторный `migrate` → «применено — 1».

## Сквозные тесты (`tests/e2e/test_schema_subscriptions.py`)

1. **TC-E2E-01** `test_migration_070_apply_rollback_reapply`: после `migrate` девять таблиц есть;
   `rollback_through("070_schema_subscriptions")` (один шаг при 0001…070) — таблиц группы и обеих
   последовательностей нет, таблицы 050/040 на месте; повторный `migrate` возвращает группу.
2. **TC-E2E-02** `test_constraints_reject_bad_rows` (под `app_rw`, откатываемая транзакция):
   `period_end = period_start` → `CheckViolationError`; чужой пользователь → FK; `source` вне enum;
   вторая строка `subscriptions` для пользователя → `UniqueViolationError` (PK); `current_period_id`
   на несуществующий период → FK; `balance_entries`: `adjustment` без `reason` →
   `CheckViolationError`, с `reason` — принято; чужой период → FK; `UPDATE` и `DELETE` под `app_rw`
   → `InsufficientPrivilegeError` (§4.6); `id` — identity под `app_rw`; токены: второй активный →
   `UniqueViolationError` (частичный UNIQUE, R-14), дубль `token_hash` → `UniqueViolationError`,
   отозванный — принят, после отзыва новый активный — принят; коды: дубль `code_hash`, счётчики и
   бонусы вне диапазона → `CheckViolationError` (пять вариантов), чужой тариф и чужой администратор →
   FK, `kind` вне enum; активации: чужой код → FK, без периода → `NotNullViolationError`; заказы:
   `amount < 0` → `CheckViolationError`; платёж с чужим заказом → FK; журнал обращений без
   партиции → `CheckViolationError`; удаление пользователя с периодами → FK (NO ACTION);
   второй пользователь только со строкой `subscriptions` удаляется, и строка состояния уходит
   каскадом (`count(*) = 0`).
3. `test_access_log_insert_as_app_rw`: `app_owner` создаёт партицию на 2000-01-01, `app_rw`
   пишет два обращения (`id` из последовательности, последовательные, `user_agent` NULL допустим),
   дубль `(ts, id)` → `UniqueViolationError`; партиция удалена вместе со строками.

## Модульные тесты (`tests/unit/db/test_schema_subscriptions.py`, 12 тестов)

- `test_catalog_matches_data_model`: колонки, ограничения, индексы девяти таблиц по §4.2.4/§4.4 с
  объявленными отклонениями; частичный `UNIQUE (user_id) WHERE revoked_at IS NULL`;
  `pg_get_partkeydef(subscription_access_log) = RANGE (ts)`; последовательность bigint OWNED BY;
  `numeric(12,2)`, `char(3)`, `char(2)`.
- `test_app_rw_privileges[<таблица>]` ×9: `app_rw` DML, для `balance_entries` — ровно
  `{SELECT, INSERT}`; `app_backup` `{SELECT}`.
- `test_sequence_privileges[<seq>]` ×2: `balance_entries_id_seq`, `subscription_access_log_id_seq`.

## Посадки стражей (до ревью)

| Посадка | Результат |
| :--- | :--- |
| без частичного UNIQUE токенов | «subscription_tokens: индексы расходятся» + `DID NOT RAISE UniqueViolationError` |
| без `REVOKE UPDATE, DELETE ON balance_entries` | `test_app_rw_privileges[balance_entries]` красный + e2e |
| `CHECK (true)` вместо правила adjustment | «balance_entries: ограничения расходятся» + `DID NOT RAISE CheckViolationError` |
| журнал без `PARTITION BY` | «subscription_access_log: индексы расходятся» + `DID NOT RAISE CheckViolationError` |
| откат без `DROP TABLE codes` | тест отката красный (`codes` в наборе после отката); остаток снесён, база пересобрана |

Файлы восстановлены, 61 passed.

## Раунд 2 — правки по ревью

- Тест каскада `subscriptions` был ложным утверждением (удалялась строка вручную): добавлен
  реальный ассерт — пользователь без периодов удаляется, `subscriptions` для него пусто. Посадка
  «без `ON DELETE CASCADE`» → e2e красный (`ForeignKeyViolationError … subscriptions_user_id_fkey`),
  после восстановления 61 passed.
- Докстринг `test_sequence_privileges` уточнён: для identity-столбца `balance_entries` `USAGE` на
  вставку не требуется, ассерт фиксирует ACL по умолчаниям §4.6; для `nextval`-столбца журнала —
  требуется.
- Формулировка критерия отката приведена к тому, что проверяет тест.

## Критерии приёмки

- [x] Все таблицы группы существуют с типами и ограничениями §4.2 — исполняемая сверка
- [x] Частичный `UNIQUE (user_id) WHERE revoked_at IS NULL` на `subscription_tokens` — сверка индексов
      и посадка второго активного токена
- [x] `balance_entries` без `UPDATE`/`DELETE` у `app_rw` — `has_table_privilege` и
      `InsufficientPrivilegeError` под `app_rw`
- [x] Откат удаляет только объекты этой миграции — тест проверяет таблицы 050/040 и обе
      последовательности; таблицы 060 и типы 0001 после отката на месте (проверено вручную ревью)

## Отклонения от описания задачи

- Индекс `subscription_periods (period_end)` без предиката: `WHERE period_end > now()` из модели
  PostgreSQL не допускает (`now()` не IMMUTABLE); `data-model.md` §4.2.4 и §4.4 исправлены.
- Остальные добавки (умолчания, CHECK, индексы по FK, identity, nullable `user_agent`, каскад
  `subscriptions`, типы заглушек `orders`/`payments`) объявлены поимённо в задаче и заголовке
  миграции.
- Модуль TC-UNIT-01 — `tests/unit/db/test_schema_subscriptions.py`; права — `has_table_privilege`.
