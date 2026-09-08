# Задача 001.21: Служба подписок: интерфейс состояния, периодов и переходов — заглушки

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-02 Регистрация и активация подписки Redeem-кодом
- UC-05 Исчерпание лимита и отзыв доступа
- UC-06 Продление и смена тарифа

Требования RTM: R-24, R-31.

<!-- contract:goal -->

## Цель задачи

Объявить `SubscriptionService` с операциями жизненного цикла и состояниями по `docs/idea.md` §4.11
так, чтобы API и очередь могли вызывать их до реализации.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/domain/subscriptions.py` — `class SubscriptionService`: `activate(user_id, plan_id, source) -> PeriodId`; `renew(user_id, source)`; `change_plan(user_id, plan_id, actor)`; `add_traffic(user_id, bytes, actor, reason)`; `expire(user_id)`; `set_state(user_id, state, reason)`; `remaining(user_id) -> int | None`; перечисление `SubscriptionState`
- `control-plane/app/jobs/handlers/subscriptions.py` — обработчики `subscription.expire`, `subscription.notify_expiring` — заглушки
- `control-plane/tests/e2e/test_subscriptions.py` — сценарии UC-06 на заглушках

### Интеграция компонентов

Каждый переход состояния публикует изменение в поток состава через
`CompositionService.publish_user(user_id)` (001.29, до него — заглушка).

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Активация возвращает период
   - Входные данные: `activate`
   - Ожидаемый результат: фиксированный `PeriodId`; состояние `active`
   - Примечание: заглушка

### Модульные тесты

- Не требуются: задача не содержит логики сверх заглушек или конфигурации.

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_subscriptions.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Сигнатуры объявлены
- [ ] Состояния `none | active | suspended_quota | suspended_admin | expired` определены
- [ ] Тесты проходят на заглушках

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.07, 001.11. Приоритет: Critical. Оценка: 2 ч. Этап: 3 — тарифы, подписки, коды.
