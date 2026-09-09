# Отчёт о проверке — задача 001.13 «API аутентификации пользователей: маршруты и сквозные тесты на заглушках»

Дата: 2026-09-09 (раунд 2 после ревью: стражи fail-closed на всех маршрутах, полного сравнения
ответов A1, атрибутов cookie при снятии, лишних полей и результата `authenticate` — см. «Раунд
2»). Стенд: VM (`ssh vm`), Compose `control-plane` (все роли `Up`). Тесты —
с рабочей машины через `ASGITransport`; Redis стенда — для fail-closed роутера `/auth`.

## Регрессия

- `make check` (nvm use 24, `PG_DSN`/`MIGRATE_DSN`/`REDIS_URL` → стенд, живые контейнеры) → код 0:
  ruff, ruff format, mypy strict (69 файлов), pytest **158 passed** (прежние 140 − 1 заглушка
  `login` из 001.10 + 001.13: 19), go, web. Первый прогон прервался сетевым таймаутом до VM
  (`could not receive data from server: Operation timed out` в `test_migrate_break_lock`) —
  повторный прогон зелёный, контейнеры не пострадали.
- `cd control-plane && pytest tests/e2e/test_auth.py` → 19 passed.

## Стенд

`vm-sync`, `up -d --build api` → `healthy`. Через nginx:

```text
POST /api/v1/auth/register {"email","password","aup_version"} → 201 {"status":"unconfirmed"}
POST /api/v1/auth/login                                        → 200, set-cookie: sid=<43 симв.>; HttpOnly; Max-Age=2592000; Path=/; SameSite=lax; Secure
POST /api/v1/auth/reset-request  u@example.com / nobody@nowhere.tld → 202 {"status":"accepted"}, 21 байт в обоих случаях
POST /api/v1/auth/login {"email":"bad","password":"x"}         → 422 validation_error (email pattern, password ≥ 8)
POST /api/v1/auth/register {…, "is_admin": true}               → 422 (лишнее поле, раунд 2)
```

## Сквозные тесты (`control-plane/tests/e2e/test_auth.py`, 19 тестов)

1. `test_seven_routes_in_openapi_with_schemas`: семь `POST` под `/api/v1/auth/` в схеме; у пяти —
   `requestBody` со ссылкой на `RegisterIn`/`VerifyIn`/`LoginIn`/`ResetRequestIn`/`ResetConfirmIn`
   в `components.schemas`; `logout`/`logout-all` без тела, ответ 204; `StatusOut.status` — enum
   из пяти фиксированных значений.
2. **TC-E2E-01** `test_uc02_register_verify`: `register` → 201 `{"status": "unconfirmed"}` без
   cookie; `verify` → 200 `confirmed`.
3. **TC-E2E-02** `test_uc02_login_sets_session_cookie`: `login` → 200 `ok`; cookie `sid` разобрана
   `SimpleCookie`: `HttpOnly`, `Secure`, `SameSite=lax`, `Path=/`, `Max-Age ≥ 3600`, значение ≥ 32
   символов и разное для двух входов.
4. `test_logout_clears_cookie_with_same_attributes` ×2 (`logout`, `logout-all`): 204, cookie
   снимается (`Max-Age=0`/`Expires`) с теми же `Path`, `HttpOnly`, `Secure`, `SameSite`, что при
   выдаче.
5. **TC-E2E-03** `test_uc15_reset_request_is_indistinguishable`: `reset-request` для
   зарегистрированного и неизвестного адреса → 202, одинаковые байты тела и одинаковый набор
   заголовков (все, кроме `Date`), без cookie; `reset-confirm` → 200 `password_changed`; вход с
   новым паролем → 200 + cookie.
6. `test_validation_errors_use_unified_format` ×11: неверный адрес, короткий пароль, язык `de`,
   без `aup_version`, лишнее поле `is_admin`, вход без пароля, лишнее поле `junk`, короткий токен
   подтверждения, NUL в адресе, `\r\nBcc:` в адресе, короткий пароль при восстановлении → 422
   `validation_error` с ожидаемым `loc`, без cookie.
7. `test_login_rejects_when_authenticate_returns_none`: подмена `UserService.authenticate` на
   `None` → 401 `invalid_credentials` без cookie (контракт для 001.14).
8. `test_user_service_stub_contract`: сигнатуры `UserService`, фиксированный `STUB_USER_ID`.
9. `test_security_fail_closed.py` (001.12, расширен): без Redis все семь маршрутов `/auth` с
   валидными телами → 503, `Retry-After: 5`, без cookie.

Скорректированы тесты прежних задач: `test_skeleton.py::test_stubs_return_501` без `login`;
`test_security_fail_closed.py` — с Redis вход без тела даёт 422 (раньше 501), без Redis — 503
раньше валидации.

## Посадки стражей (до ревью, по одной; файлы восстановлены после каждой, `diff` пуст)

| Посадка | Результат |
| :--- | :--- |
| cookie без `Secure` | e2e: атрибута нет в `set-cookie` |
| `SameSite=None` | e2e: `samesite=lax` не найден |
| фиксированное значение cookie `stub-session` | e2e: «значение cookie — непрозрачное, ≥ 128 бит», `12 >= 32` |
| `reset-request` различает адреса | e2e: `accepted` ≠ `unknown` |
| пароль без минимальной длины | e2e: `201 == 422` |
| `/auth` без `redis_required` | fail-closed: `422 == 503` |
| `register` 200 вместо 201 | e2e: `200 == 201` |

После посадок: 11 passed; `make check` → 0, 150 passed.

## Раунд 2 — правки по ревью и их проверка

Ревью раунда 1 (`sarcasmotron-001-13`, REJECTED): три посадки ревьюера прошли зелёными — C-1
fail-closed охранялся только на `login`; C-2 UC-15 A1 не сравнивал заголовки; C-3 снятие cookie
с другим `Path` не ловилось (проверка подстрокой); плюс выброшенный результат `authenticate`,
протухшая строка в `control-plane/.AGENTS.md`, необъявленное проглатывание лишних полей, NUL в
адресе, `StatusOut.status: str`, заглушка входа «успешна» на стенде, `listen 80` без редиректа
(вне задачи — 001.66).

- **C-1:** `test_security_fail_closed.py` проверяет все семь маршрутов `/auth` с валидными телами
  → 503, `Retry-After`, без cookie. Посадка ревьюера (`redis_required` только на `login`) →
  `register` `201 == 503`.
- **C-2:** A1 сравнивает байты тела и полный набор заголовков кроме `Date`. Посадка ревьюера
  (`X-Mail-Sent` для «зарегистрированного» адреса) → «ни один заголовок не различает».
- **C-3:** атрибуты cookie разбираются `SimpleCookie`, при снятии сверяются `Path`, `HttpOnly`,
  `Secure`, `SameSite` с выданными. Посадка ревьюера (`Path=/api/v1/auth` при снятии) → красная.
- **`authenticate`:** маршрут переводит `None` в 401 `invalid_credentials` без cookie; страж
  подменой метода. Посадка «результат выброшен» → `200 == 401`.
- **Лишние поля:** `extra="forbid"` на телах `/auth` (объявлено); посадка `extra="ignore"` →
  `201 == 422`. **Управляющие символы:** шаблон адреса исключает `\x00-\x1f\x7f`; тесты NUL и
  `\r\nBcc:`. **`StatusOut.status`** — `Literal`, enum в OpenAPI (тест). **Заглушка входа** —
  объявлена в задаче как долг 001.14. Карта `control-plane/.AGENTS.md` исправлена.
- Инцидент посадок: после восстановления файла в ту же секунду Python использовал байткод
  посаженной версии (одинаковая секунда mtime) — два теста падали до очистки `__pycache__`;
  это свойство инструмента, отмечено на будущее.

После правок: `make check` → 0, 158 passed; api пересобран, через nginx лишнее поле → 422.

### Вердикт раунда 2

`sarcasmotron-001-13`: **APPROVED**. Пять из шести посадок ревьюера красные (fail-closed на
`register`, заголовок-утечка, `Path` при снятии ×2, выброшенный `authenticate`); шестая —
`VerifyIn` без `StrictModel` — зелёная (страж лишних полей покрывал 2 модели из 5); карты
`app/api/.AGENTS.md` и текст задачи отставали от раунда 2. Закрыто сразу после вердикта без
нового раунда: случаи с лишним полем для `VerifyIn`, `ResetRequestIn`, `ResetConfirmIn` (всего
14 случаев валидации), проверка «ровно семь маршрутов раздела», карта `auth.py` и текст задачи
приведены к раунду 2; посадка «`VerifyIn` без `StrictModel`» → красная. Передача в 001.14:
сохранить одинаковое время ответа A1 (один путь кода), снять «успех для любых учётных данных».

## Критерии приёмки

- [x] Семь маршрутов присутствуют в OpenAPI со схемами — тест 1
- [x] Сквозные тесты проходят на заглушках — 19 passed, стенд через nginx
- [x] Ответ на несуществующий адрес неотличим от ответа на существующий (UC-15 A1) — статус,
      байты тела и все заголовки кроме `Date` совпадают (тест 5, стенд)

## Отклонения от описания задачи

- `require_csrf`, `SessionStore`, `RateLimiter.check` в маршруты не подключены (заглушки 001.12
  поднимают `NotImplementedError`); fail-closed действует через `redis_required`; границы полей
  (пароль ≥ 8, адрес по шаблону без управляющих символов и без `email-validator`,
  `extra="forbid"`, `StatusOut` как `Literal`); `authenticate → None` = 401; cookie-помощники и
  `USER_SESSION_TTL` в `security/sessions.py`; заглушка `login` 001.10 заменена. Всё объявлено в
  «Уточнениях при реализации»; карты `app/api/.AGENTS.md`, `app/.AGENTS.md`,
  `control-plane/.AGENTS.md`.
- Передача в 001.66: публичный `server` nginx слушает 80 без редиректа на 443 и без HSTS —
  cookie `Secure` по открытому HTTP браузер отбрасывает (замечание ревью).
