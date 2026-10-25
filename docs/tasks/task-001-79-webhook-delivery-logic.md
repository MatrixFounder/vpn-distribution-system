# Задача 001.79: Доставка webhook: подпись HMAC, повторы, идентификатор события, защита от SSRF

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-07 Отказ и возврат ноды
- UC-05 Исчерпание лимита и отзыв доступа

Требования RTM: R-39, R-40.

<!-- contract:goal -->

## Цель задачи

Реализовать `WebhookSender`: HMAC-SHA256 тела, заголовок `X-Event-Id`, повторы с экспоненциальной
задержкой, `webhook_deliveries`, запрет частных диапазонов, службы метаданных и перенаправлений
после разрешения имени, сид ключа `settings.webhooks` и ряд доставок в `/metrics`.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/tests/e2e/test_notifications.py` — webhook в локальный сервер; SSRF отклонён
- `control-plane/migrations/NNN_settings_webhooks.sql` и откат `.rollback.sql` (NNN — следующий
  свободный номер) — миграция-сид ключа `settings.webhooks` (владелец — эта задача, data-model
  §4.2.6; на неё ссылаются 001.51 и 001.52): пустой список получателей `[]`, элемент —
  `{url, secret}` (секрет подписи HMAC хранится в `settings`, `docs/architectures/security.md`
  §7.1); `ON CONFLICT DO NOTHING`, откат удаляет ровно этот ключ; получателей добавляет миграция

### Изменения в существующих файлах

#### Файл: `control-plane/app/notifications/webhook.py`

- реализация; `is_allowed_target(url) -> bool`
- ряд `/metrics` (префикс `control_plane_`, имя берёт 001.68):
  `control_plane_webhook_deliveries{status}` — число строк `webhook_deliveries` за последний час по
  `status` (`pending`, `sent`, `failed`), производный ряд из базы, как `control_plane_reports`

### Интеграция компонентов

Получатели — из `settings.webhooks`; задача доставки — на каждого получателя
(`notify:webhook:{event_id}:{получатель}`, 001.51).

Webhook приложения доставляет пользовательские события и «Нода приостановлена провайдером»
(001.52). Остальные операторские события доставляет Alertmanager (001.68); идентификатор события в
его webhook — `fingerprint` алерта и `groupKey` группы.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Webhook с идентификатором
   - Входные данные: событие
   - Ожидаемый результат: запрос с `X-Event-Id` и подписью (AC-17)
2. **TC-E2E-02:** SSRF
   - Входные данные: адрес `169.254.169.254`
   - Ожидаемый результат: доставка отклонена без запроса
3. **TC-E2E-03:** Сид получателей и ряд доставок
   - Входные данные: база после миграций; затем получатель добавлен фикстурой, одна доставка
     успешна, одна исчерпала повторы; `GET /metrics`
   - Ожидаемый результат: `settings.webhooks` — `[]`;
     `control_plane_webhook_deliveries{status="sent"} 1`, `…{status="failed"} 1`

### Модульные тесты

1. **TC-UNIT-01:** Проверка адреса
   - Проверяемая функция: `notifications/webhook.py::is_allowed_target`
   - Входные данные: `10.0.0.1`, публичный адрес
   - Ожидаемый результат: ложь, истина

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_notifications.py -k webhook tests/unit/notifications && pytest tests/e2e/test_migrations.py` (`-k` действует на все файлы вызова, поэтому миграции — отдельным вызовом)
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] AC-17 в части webhook
- [ ] Повторная доставка различима получателем по `X-Event-Id`
- [ ] Ключ `settings.webhooks` создаёт миграция-сид этой задачи (TC-E2E-03)
- [ ] Ряд `control_plane_webhook_deliveries{status}` экспортируется (TC-E2E-03)

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.52. Приоритет: High. Оценка: 3 ч. Этап: 8 — уведомления и почта.
