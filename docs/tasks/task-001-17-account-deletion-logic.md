# Задача 001.17: Удаление аккаунта: стирание PII, отзыв credentials, аннулирование токена

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-14 Удаление аккаунта

Требования RTM: R-13.

<!-- contract:goal -->

## Цель задачи

Реализовать модель удаления из `docs/architectures/security.md` §7.2: стирание PII в `users` и
удаление строк таблиц с PII без изменения прежних записей журналов.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/tests/unit/domain/test_profile.py` — TC-UNIT-01 (`PII_TABLES` против перечня §7.2)

### Изменения в существующих файлах

#### Файл: `control-plane/tests/e2e/test_me.py`

- дополнить (файл заведён 001.15): сценарий UC-14 с проверкой таблиц

#### Файл: `control-plane/app/domain/profile.py`

- `delete_account(user_id, ctx)`, одна транзакция: `users.status = 'deleted'`; `email` = `deleted+{id}@invalid` (уникальная заглушка: колонка `NOT NULL UNIQUE`); `password_hash` — непроверяемая заглушка (`NOT NULL`); `timezone = 'UTC'`; `deleted_at = now()`; `subscription_tokens.revoked_at`
- в той же транзакции — удаление строк пользователя по перечню §7.2: `email_tokens`, `auth_events`, `subscription_access_log`, `user_online_ips`, `held_online_ips`, `user_blocked_ips`; `email_deliveries` — по `events.user_id`, служебные письма (`event_id IS NULL`, 001.78) — по `email_token_id` токенов пользователя и по `recipient` = прежнему `users.email`, до удаления `email_tokens` и замены адреса заглушкой
- отзыв credentials — в той же транзакции `CompositionService.remove_user(conn, user_id)` (001.29): постановка задачи `composition.publish_user`; её исполнитель переводит строки `node_user_state` всех нод в `removed` с пустым `blocked_ips`, `updated_seq` по правилу M-2 (data-model §4.4); прямой записи `node_user_state` здесь нет
- в той же транзакции — `audit.record` на её подключении (событие «Удаление пользователя» таблицы 001.48): `ctx` — `AuditContext` сессии пользователя (001.46), `actor_type = user`, `entity_id` — `user_id`; `ip` и `user_agent` не заполняются, адреса почты в `old_value` и `new_value` нет (security.md §7.2)
- после коммита — `SessionStore.revoke_all`
- `PII_TABLES` — перечень §7.2, включая `user_online_ips`, `held_online_ips` и `user_blocked_ips`

#### Файл: `control-plane/app/api/me.py`

- Изменение принятого кода (отступление от 001.15): сигнатура заглушки `ProfileService.delete_account(user_id)` дополняется параметром `ctx: AuditContext` (001.46); маршрут `DELETE /me` передаёт контекст сессии пользователя

### Интеграция компонентов

Отзыв credentials на нодах — через `CompositionService.remove_user` (001.29); ноды без связи
получают изменение при первом обмене. Для `users.status = 'deleted'` `publish_user` публикует
только `removed` (001.29), поэтому начисление, продление и истечение удалённому доступа не
возвращают. Операции панели над удалённым пользователем отвечают 409 (001.50). Служебные письма
(`event_id IS NULL`, колонка `email_token_id`) появляются с миграцией 001.78 (зависимость 001.17
→ 001.78). Таблицу `held_online_ips` заводит 001.77 (зависимость через 001.16 → 001.77b →
001.77). Запись журнала — `audit.record` и `AuditContext` (001.46, зависимость 001.17 → 001.46);
строку `audit_log` удаления проверяет эта задача (001.48).

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Удаление аккаунта
   - Входные данные: `DELETE /me` с подтверждением
   - Ожидаемый результат: вход по адресу невозможен; `GET /s/{token}` → 404; `traffic_hourly` содержит `user_id`, `users` без PII и со `status = 'deleted'`; строк `user_online_ips`, `held_online_ips`, `user_blocked_ips` и `email_deliveries` пользователя нет (AC-24)
2. **TC-E2E-02:** Начисление удалённому не возвращает доступ
   - Входные данные: удалённый пользователь; `SubscriptionService.add_traffic` для него
   - Ожидаемый результат: строки `node_user_state` пользователя остаются `removed`
3. **TC-E2E-03:** Удаление в журнале аудита
   - Входные данные: `DELETE /me` с подтверждением
   - Ожидаемый результат: одна новая строка `audit_log` с `actor_type = user` и `entity_id` = `user_id`; `ip` и `user_agent` пусты, адреса почты в `old_value` и `new_value` нет; прежние строки `audit_log` не изменены

### Модульные тесты

1. **TC-UNIT-01:** Список таблиц PII
   - Проверяемая функция: `app/domain/profile.py::PII_TABLES`
   - Входные данные: —
   - Ожидаемый результат: совпадает с перечнем §7.2 архитектуры

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_me.py -k delete` и
  `pytest tests/unit/domain/test_profile.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] AC-24 выполнен
- [ ] Прежние строки `audit_log` и `traffic_*` операцией удаления не изменены; удаление записано в
  `audit_log` с `actor_type = user` в транзакции удаления (TC-E2E-03)
- [ ] Сессии пользователя инвалидированы
- [ ] Начисление, продление и истечение после удаления доступа не возвращают

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.16, 001.29, 001.78, 001.46. Приоритет: Medium. Оценка: 2 ч. Этап: 6 —
subscription-эндпоинт и логика кабинета.
