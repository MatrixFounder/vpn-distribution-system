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

Таблицы создаются ролью `app_migrate`; права `app_rw` выдаются в той же миграции по перечню §4.6.

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
   - Проверяемая функция: `tests/unit/db/test_grants.py::test_app_rw_privileges`
   - Входные данные: каталог `information_schema.role_table_grants`
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
