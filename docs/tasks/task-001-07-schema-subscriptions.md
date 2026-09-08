# Задача 001.07: Схема: подписки, баланс, токены, коды

Тип задачи: `[CONFIGURATION]`.

## Связь со сценариями
- UC-02 Регистрация и активация подписки Redeem-кодом
- UC-06 Продление и смена тарифа
- UC-16 Работа в личном кабинете

Требования RTM: R-01, R-14, R-31, R-32, R-33.

<!-- contract:goal -->

## Цель задачи

Создать таблицы, ограничения и индексы по `docs/architectures/data-model.md` для группы «схема:
подписки, баланс, токены, коды» так, чтобы миграция применялась и откатывалась без ошибок.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/migrations/070_schema_subscriptions.sql` — таблицы: `subscriptions`, `subscription_periods`, `balance_entries`, `subscription_tokens`, `subscription_access_log`, `codes`, `code_redemptions`, `orders`, `payments`
- `control-plane/migrations/070_schema_subscriptions.rollback.sql` — откат

### Интеграция компонентов

Таблицы создаются ролью `app_migrate`; права `app_rw` выдаются в той же миграции по перечню §4.6
(умолчания привилегий `app_owner` из 0001; особый запрет — `REVOKE UPDATE, DELETE ON balance_entries
FROM app_rw`).

Уточнения при реализации. Сверх §4.2.4:

- умолчания: `subscription_periods.used_billable_bytes 0`, `created_at now()`;
  `subscriptions.state 'none'`, `state_changed_at now()`; `balance_entries.created_at now()`;
  `subscription_tokens.issued_at now()`; `subscription_access_log.ts now()`; `codes.max_uses 1`,
  `max_uses_per_user 1`, `uses_count 0`, `traffic_bonus_bytes 0`, `duration_bonus_days 0`,
  `created_at now()`; `code_redemptions.redeemed_at now()`; `orders.created_at now()`,
  `payments.created_at now()`;
- `CHECK (period_end > period_start)`; `CHECK (source <> 'adjustment' OR reason IS NOT NULL)`
  (правило §4.2.4 «reason обязательно для adjustment»); `CHECK` на счётчики кодов (`max_uses > 0`,
  `max_uses_per_user > 0`, `uses_count >= 0`, бонусы `>= 0`); `CHECK (amount >= 0)` в `orders`
  и `payments`;
- индекс `subscription_periods (period_end)` без предиката: `WHERE period_end > now()` из модели
  PostgreSQL не допускает (`now()` не IMMUTABLE) — модель исправлена;
- индексы по FK сверх §4.4: `code_redemptions (user_id)`, `orders (user_id)`, `payments (order_id)`;
- `balance_entries.id` — `GENERATED ALWAYS AS IDENTITY`;
- `subscription_access_log.user_agent` допускает NULL (заголовок может отсутствовать),
  `country` — NULL по модели; `source_ip` NOT NULL;
- `ON DELETE CASCADE` только у `subscriptions.user_id` (строка состояния без пользователя
  бессмысленна); остальные связи — NO ACTION (умолчание): периоды, токены, активации и заказы
  переживают удаление аккаунта как история (§16.4);
- `orders`/`payments` — заглушки О-2, типы выбраны здесь: `amount numeric(12,2)`, `currency char(3)`,
  `status text`, `provider`/`provider_ref text`.

Модуль TC-UNIT-01 — `tests/unit/db/test_schema_subscriptions.py` (команда `-k 'schema_subscriptions'`),
права — `has_table_privilege`/`has_sequence_privilege`.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Миграция применяется и откатывается
   - Входные данные: база после предыдущих миграций
   - Ожидаемый результат: `yoyo apply` и `yoyo rollback` завершаются с кодом 0
2. **TC-E2E-02:** Ограничения действуют
   - Входные данные: вставка строк, нарушающих `UNIQUE`, `CHECK`, `EXCLUDE` из §4.4
   - Ожидаемый результат: каждая вставка отклонена с ошибкой ограничения

### Модульные тесты

1. **TC-UNIT-01:** Проверка прав роли `app_rw`
   - Проверяемая функция: `tests/unit/db/test_schema_subscriptions.py::test_app_rw_privileges`
   - Входные данные: `has_table_privilege` для `app_rw`, `app_backup` (каталог `information_schema` из сессии `app_rw` чужих прав не показывает)
   - Ожидаемый результат: права совпадают с перечнем §4.6

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/unit/db -k 'schema_subscriptions'`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Все таблицы группы существуют с типами и ограничениями §4.2
- [ ] Частичный `UNIQUE (user_id) WHERE revoked_at IS NULL` на `subscription_tokens`
- [ ] `balance_entries` без `UPDATE`/`DELETE` у `app_rw`
- [ ] Откат удаляет только объекты этой миграции

## Примечания

Миграция — конфигурационная задача без пары stub/logic. Партиционированные таблицы получают функцию
создания партиций в 001.08.

Зависимости: 001.05. Приоритет: Critical. Оценка: 3 ч. Этап: 1 — схема данных и каркас Control
Plane.
