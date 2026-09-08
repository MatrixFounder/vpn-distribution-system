# Задача 001.17: Удаление аккаунта: стирание PII, отзыв credentials, аннулирование токена

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-14 Удаление аккаунта

Требования RTM: R-13.

<!-- contract:goal -->

## Цель задачи

Реализовать модель удаления из `docs/architectures/security.md` §7.2: стирание PII в `users` и
удаление строк таблиц с PII без записи в журналы.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/tests/e2e/test_me.py` — сценарий UC-14 с проверкой таблиц

### Изменения в существующих файлах

#### Файл: `control-plane/app/domain/profile.py`

- `delete_account(user_id)`: одна транзакция — `subscription_tokens.revoked_at`, состояние `removed` в `node_user_state` всех нод, стирание `email`, `password_hash`, `timezone`, `deleted_at = now()`, удаление `email_tokens`, `auth_events`, `subscription_access_log`, `email_deliveries` пользователя; `SessionStore.revoke_all`

### Интеграция компонентов

Отзыв credentials на нодах — через `CompositionService.remove_user` (001.29); ноды без связи
получают изменение при первом обмене.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Удаление аккаунта
   - Входные данные: `DELETE /me` с подтверждением
   - Ожидаемый результат: вход по адресу невозможен; `GET /s/{token}` → 404; `traffic_hourly` содержит `user_id`, `users` без PII (AC-24)

### Модульные тесты

1. **TC-UNIT-01:** Список таблиц PII
   - Проверяемая функция: `app/domain/profile.py::PII_TABLES`
   - Входные данные: —
   - Ожидаемый результат: совпадает с перечнем §7.2 архитектуры

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_me.py -k delete`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] AC-24 выполнен
- [ ] `audit_log` и `traffic_*` не изменены операцией удаления
- [ ] Сессии пользователя инвалидированы

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.16, 001.29. Приоритет: Medium. Оценка: 2 ч. Этап: 6 — subscription-эндпоинт и
логика кабинета.
