# Задача 001.83: Служба подписок: истечение по расписанию и уведомление «истекает»

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-06 Продление и смена тарифа

Требования RTM: R-24, R-31.

<!-- contract:goal -->

## Цель задачи

Реализовать истечение по расписанию (раз в минуту) через `SubscriptionService.expire` (001.22) с
переводом в `expired` и публикацией отзыва в поток состава без снятия блокировки администратора, а
также уведомление «Подписка истекает» за срок `settings.subscription_expiring_notice_days` (по
умолчанию 3 дня).

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/migrations/NNN_settings_subscription_expiring.sql` и откат `.rollback.sql` (NNN — следующий свободный номер) — миграция-сид ключа `settings.subscription_expiring_notice_days`, целое число дней, по умолчанию 3 (`ON CONFLICT DO NOTHING`; откат удаляет ровно этот ключ) (решение заказчика 2026-10-25, В-29)

### Изменения в существующих файлах

#### Файл: `control-plane/tests/e2e/test_subscriptions.py`

- дополнить (файл заведён 001.21): истечение — `expired` не позднее минуты; событие «Подписка истекла»; блокировка администратора при истечении сохраняется (TC-E2E-03); однократность уведомления «истекает»

#### Файл: `control-plane/app/jobs/handlers/subscriptions.py`

- заглушки 001.21 заменяются: `expire` выбирает текущие периоды с `period_end <= now()` у подписок в состоянии `active` или `suspended_quota`, вызывает `SubscriptionService.expire(user_id)` (001.22) и порождает событие `subscription_expired`; `notify_expiring` читает `subscription_expiring_notice_days`, выбирает `expiring_within(days)` и порождает `subscription_expiring`

#### Файл: `control-plane/app/domain/subscriptions.py`

- `expiring_within(days)` — новый метод: подписки в состоянии `active` или `suspended_quota`, у которых `period_end` текущего периода лежит в `(now(), now() + days]`
- Изменение принятого кода (отступление от 001.22): `expire(user_id)` переводит в `expired` только `active` и `suspended_quota` (условие обновления `state IN ('active', 'suspended_quota')` вместо `state <> 'expired'`); `suspended_admin` сохраняется до разблокировки администратором, после неё ближайший тик переводит подписку с прошедшим `period_end` в `expired` (решение заказчика 2026-10-25, В-24); идемпотентность и однократная публикация состава — как прежде

#### Файл: `control-plane/app/jobs/scheduler.py`

- две записи `SCHEDULE`: `subscription.expire` раз в минуту; `subscription.notify_expiring` раз в час

#### Файл: `control-plane/tests/unit/jobs/test_handlers.py`

- страж обновить: `subscription.expire` и `subscription.notify_expiring` больше не заглушки и стоят в `SCHEDULE`

### Интеграция компонентов

Событие — `emit` (001.51, зависимость 001.83 → 001.51): строка `events` с `dedup_key` =
`{type}:{user_id}:{period_id}`, повтор в периоде события не создаёт; доставка — 001.52.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Истечение по расписанию
   - Входные данные: `period_end` в прошлом; тик планировщика
   - Ожидаемый результат: состояние `expired`; событие `subscription_expired` в `events`
2. **TC-E2E-02:** Уведомление однократно
   - Входные данные: подписка с `period_end` в окне срока; два тика `subscription.notify_expiring`
   - Ожидаемый результат: в `events` одна строка `subscription_expiring` за период
3. **TC-E2E-03:** Истечение не снимает блокировку администратора
   - Входные данные: подписка `suspended_admin` с `period_end` в прошлом; тик планировщика; затем `set_state(active)` и следующий тик
   - Ожидаемый результат: после первого тика — `suspended_admin`, события `subscription_expired` нет; после второго — `expired` и одно событие `subscription_expired`

### Модульные тесты

1. **TC-UNIT-01:** Выбор истекающих
   - Проверяемая функция: `app/domain/subscriptions.py::expiring_within`
   - Входные данные: срок 3 дня (значение по умолчанию); подписки с окончанием через 2 дня, через 4 дня, `suspended_admin` с окончанием через 2 дня
   - Ожидаемый результат: выбрана только первая

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_subscriptions.py -k expir tests/e2e/test_migrations.py && pytest tests/unit/jobs/test_handlers.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Истечение переводит в `expired` не позднее минуты после `period_end`
- [ ] Уведомление однократно за период
- [ ] Истечение сохраняет `suspended_admin` (TC-E2E-03)
- [ ] Срок уведомления — `settings.subscription_expiring_notice_days`, по умолчанию 3 дня

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.22, 001.51. Приоритет: High. Оценка: 2 ч. Этап: 3 — тарифы, подписки, коды.
