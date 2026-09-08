# Задача 001.13: API аутентификации пользователей: маршруты и сквозные тесты на заглушках

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-02 Регистрация и активация подписки Redeem-кодом
- UC-15 Восстановление пароля

Требования RTM: R-08, R-09.

<!-- contract:goal -->

## Цель задачи

Объявить маршруты `/api/v1/auth/*` со схемами запросов и ответов и сквозными тестами, проходящими на
фиксированных ответах.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/api/auth.py` — `POST /auth/register`, `POST /auth/verify`, `POST /auth/login`, `POST /auth/logout`, `POST /auth/logout-all`, `POST /auth/reset-request`, `POST /auth/reset-confirm`; схемы `RegisterIn`, `LoginIn`, `ResetConfirmIn`
- `control-plane/app/domain/users.py` — `class UserService`: `register(email, password, aup_version, lang) -> UserId`; `verify_email(token)`; `authenticate(email, password) -> UserId | None`; `request_reset(email)`; `confirm_reset(token, password)` — заглушки
- `control-plane/tests/e2e/test_auth.py` — сценарий UC-02 шаги 1–6 и UC-15 на заглушках

### Интеграция компонентов

Маршруты используют `SessionStore`, `RateLimiter`, `require_csrf` из 001.12 и `UserService`.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Регистрация возвращает фиксированный ответ
   - Входные данные: `POST /auth/register` с валидным телом
   - Ожидаемый результат: 201 и `{"status": "unconfirmed"}`
   - Примечание: заглушка
2. **TC-E2E-02:** Вход возвращает cookie
   - Входные данные: `POST /auth/login`
   - Ожидаемый результат: 200, cookie `sid` с `HttpOnly; Secure; SameSite=Lax`
   - Примечание: заглушка
3. **TC-E2E-03:** Восстановление принимает запрос
   - Входные данные: `POST /auth/reset-request`
   - Ожидаемый результат: 202 независимо от существования адреса

### Модульные тесты

- Не требуются: задача не содержит логики сверх заглушек или конфигурации.

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_auth.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Семь маршрутов присутствуют в OpenAPI со схемами
- [ ] Сквозные тесты проходят на заглушках
- [ ] Ответ на несуществующий адрес неотличим от ответа на существующий (UC-15 A1)

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.12. Приоритет: High. Оценка: 3 ч. Этап: 2 — пользователи и кабинет.
