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

Уточнения при реализации:

- схемы: `Email` — строка «имя@домен» 3…254 символа без пробелов и управляющих символов (NUL и
  переводы строки не должны доезжать до базы и конверта письма), без нормализации (§16.8 —
  001.14; `email-validator` не добавляется); `Password` — 8…256 символов (минимальную длину §9
  постановки не задаёт — решение 001.13); `Token` — 16…512 символов; тела запросов —
  `extra="forbid"`: неизвестные поля дают 422, а не проглатываются; `RegisterIn(email, password,
  aup_version 1…32, lang en|ru = en, captcha_token?)`, `VerifyIn(token)`, `LoginIn(email,
  password, captcha_token?)`, `ResetRequestIn(email)`, `ResetConfirmIn(token, password)`;
  `captcha_token` принимается и до 001.14 не проверяется; ответы — `StatusOut(status)` с
  `Literal` фиксированных значений (закреплены в OpenAPI); ошибки валидации — 422 единого
  формата (001.10);
- фиксированные ответы заглушек: `register` 201 `unconfirmed`, `verify` 200 `confirmed`, `login`
  200 `ok` + cookie, `logout`/`logout-all` 204 со снятием cookie, `reset-request` 202 `accepted`
  для любого адреса (UC-15 A1: статус, байты тела и все заголовки кроме `Date` одинаковы;
  001.14 обязана сохранить и одинаковое время ответа — один путь кода для обоих случаев),
  `reset-confirm` 200 `password_changed`;
- cookie сессии — `app/security/sessions.py`: `SESSION_COOKIE = "sid"`, `set_session_cookie()` /
  `clear_session_cookie()` с атрибутами §7.1 (`HttpOnly; Secure; SameSite=Lax; Path=/;
  Max-Age`), `USER_SESSION_TTL` = 30 суток до уточнения в 001.14; значение cookie в заглушке —
  `secrets.token_urlsafe(32)` (≥ 128 бит, не фиксировано), сессии за ним до 001.14 нет;
- `UserService` (`app/domain/users.py`) — заглушки с фиксированными значениями
  (`STUB_USER_ID`), методы `register`, `verify_email`, `authenticate`, `request_reset`,
  `confirm_reset` с сигнатурами контракта; зависимость `get_user_service()` в `api/auth.py`
  (001.14 подключит пул и почту); заглушка `authenticate` принимает любые учётные данные — на
  стенде вход «успешен» для любого адреса и пароля (ревью раунда 1), 001.14 обязана это снять;
  маршрут уже сейчас переводит `authenticate → None` в 401 `invalid_credentials` без cookie
  (страж подменой);
- `require_csrf`, `SessionStore` и `RateLimiter.check` в маршруты **не** подключены: их заглушки
  001.12 поднимают `NotImplementedError` (500) и сломали бы фиксированные ответы; подключаются в
  001.14 вместе с логикой. Fail-closed по Redis действует уже сейчас — `redis_required` на
  включении роутера `/auth` (`api/router.py`), проверяется до валидации тела;
- заглушка `login` из 001.10 (501) заменена маршрутом с фиксированным ответом; тест 001.10
  `test_stubs_return_501` и тест fail-closed 001.12 скорректированы (вход без тела с Redis → 422);
- `tests/e2e/test_auth.py` — UC-02 шаги 1–6 и UC-15 на заглушках, ровно семь маршрутов в OpenAPI
  со схемами, четырнадцать случаев валидации (включая лишнее поле в каждой из пяти моделей и
  управляющие символы), `authenticate → None` = 401, контракт `UserService`.

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
