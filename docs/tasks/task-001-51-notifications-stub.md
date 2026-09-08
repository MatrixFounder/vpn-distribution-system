# Задача 001.51: События, почта, webhook — интерфейсы и заглушки

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-05 Исчерпание лимита и отзыв доступа
- UC-07 Отказ и возврат ноды
- UC-02 Регистрация и активация подписки Redeem-кодом

Требования RTM: R-39, R-40, R-41.

<!-- contract:goal -->

## Цель задачи

Объявить `EventService`, `EmailSender`, `WebhookSender` и обработчики очереди `background` с
заглушками; двенадцать типов событий §4.17 зафиксированы перечислением.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/notifications/events.py` — `class EventType(Enum)` — 12 типов; `async def emit(conn, type, user_id=None, node_id=None, payload=None, dedup_key=None)` — заглушка
- `control-plane/app/notifications/email.py` — `class EmailSender`: `send(to, template, lang, ctx) -> DeliveryId` — заглушка; шаблоны в `app/notifications/templates/{ru,en}/`
- `control-plane/app/notifications/webhook.py` — `class WebhookSender`: `send(url, event) -> None` — заглушка; подпись HMAC-SHA256
- `control-plane/app/jobs/handlers/notifications.py` — `notify.email`, `notify.webhook` — заглушки
- `control-plane/tests/e2e/test_notifications.py` — событие → задачи в очереди

### Интеграция компонентов

`emit` вызывается из подписок (001.22), лимитов (001.35), статусов (001.30), inbound (001.27).

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Событие порождает задачи
   - Входные данные: `emit(traffic.80)`
   - Ожидаемый результат: две задачи `background`: почта и webhook
   - Примечание: заглушка

### Модульные тесты

- Не требуются: задача не содержит логики сверх заглушек или конфигурации.

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_notifications.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] 12 типов событий объявлены и совпадают с §4.17
- [ ] Шаблоны RU и EN заведены как файлы

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.11, 001.09. Приоритет: High. Оценка: 2 ч. Этап: 8 — уведомления и почта.
