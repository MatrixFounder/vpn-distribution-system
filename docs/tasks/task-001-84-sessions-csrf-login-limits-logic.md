# Задача 001.84: Сессии в Redis, CSRF, вход и ограничения частоты входа

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-02 Регистрация и активация подписки Redeem-кодом
- UC-16 Работа в личном кабинете

Требования RTM: R-08, R-45.

<!-- contract:goal -->

## Цель задачи

Реализовать `SessionStore` на Redis с TTL и набором сессий пользователя, `require_csrf`, вход с
argon2id, запись `auth_events`, лимиты входа по адресу и учётной записи с CAPTCHA вместо блокировки,
`logout-all`.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/tests/e2e/test_auth.py` — вход, лимиты, `logout-all`

### Изменения в существующих файлах

#### Файл: `control-plane/app/security/sessions.py`

- реализация на Redis; `revoke_all` через `user_sessions:{id}`

#### Файл: `control-plane/app/security/ratelimit.py`

- скользящее окно `INCR`/`EXPIRE`; ключи `rl:login:ip`, `rl:login:acct`, `rl:reg:ip`, `rl:reset:ip`

#### Файл: `control-plane/app/security/csrf.py`

- проверка `X-CSRF-Token`

#### Файл: `control-plane/app/domain/users.py`

- `authenticate`

### Интеграция компонентов

Fail-closed при недоступности Redis (001.12).

Уточнения при реализации (2026-09-09): задача закрыта **без нового кода** — всё её содержание
выполнено задачей 001.14 (логика API аутентификации, четыре раунда Sarcasmotron, коммит
`d162aab`) и подтверждено её тестами; ниже — соответствие пунктов контракта и объявленные
отклонения от формулировок этой задачи:

- `sessions.py` — `SessionStore` на Redis: хеш `sess:{sid}` с TTL бездействия (пользователь —
  30 суток, `USER_SESSION_TTL`; администратор — 12 ч по Н-27 задаёт 001.47), набор
  `user_sessions:{subject}` с TTL и чисткой мёртвых членов, `revoke_all(subject, except_sid)`;
  `create` возвращает сессию с маркером CSRF (tdd-strict `tests/unit/security/test_sessions.py`);
- `csrf.py` — `require_csrf`: заголовок `X-CSRF-Token` сверяется с записью сессии
  (`compare_digest`), безопасные методы и запросы без сессии проходят; подключён к
  `logout`/`logout-all` (001.14) и ко всем мутациям кабинета (001.15);
- `ratelimit.py` — окно **скользящее на `ZSET` одним Lua-скриптом**, а не `INCR`/`EXPIRE` из
  формулировки задачи: `INCR`/`EXPIRE` даёт окно с фиксированной границей, через которую перебор
  «протекает» удвоенным порогом на стыке окон; ключи — по построителям 001.12 и §5.12
  (построители `login_ip`, `login_account`, `register_ip`, `register_email_domain`,
  `reset_email`; ключи подтверждения ссылок `rl:verify:ip:*` и `rl:reset:ip:*` собираются
  общим `key("verify"|"reset", "ip", …)` в `api/auth.py`), а не `rl:login:acct`/`rl:reg:ip`
  из текста задачи; запрос восстановления ограничивается по адресу почты (§5.12: «Восстановление
  пароля — адрес электронной почты»), а `rl:reset:ip` из контракта живёт на `reset-confirm`
  (порог `TOKEN_IP` 20/600 с по адресу источника);
- R-45 закрыт этой задачей **только в части входа** (ключи по адресу и учётной записи, CAPTCHA);
  порог для несуществующих subscription-токенов — 001.42, лимит Node API по identity и полный
  перечень эндпоинтов §5.12 — 001.72 (построители `subscription_token`,
  `subscription_unknown_ip`, `node_identity` из 001.12 ждут их), джиттер и backoff агента —
  001.54;
- `users.py::authenticate` — argon2id, хеш-пустышка для неизвестного адреса, `auth_events`;
  порог по учётной записи хранит только отказы, над порогом с провайдером CAPTCHA проверяется до
  пароля, без провайдера не действует (§5.12 «блокировка учётной записи не применяется»; ОВ-A3;
  контракт — security.md §7.3);
- TC-E2E-01 — `tests/e2e/test_auth.py::test_uc02_a5_login_rate_limit_by_ip_does_not_block_account`
  (шестой запрос с адреса → 429 с `Retry-After`, учётная запись входит с другого адреса);
  TC-E2E-02 — `test_logout_requires_csrf_and_revokes_sessions` (две сессии → `logout-all` → обе
  недействительны); TC-UNIT-01 — `tests/unit/security/test_ratelimit.py::test_sixth_call_within_window_is_rejected`;
- `tests/e2e/test_auth.py` не создавался заново — он существует с 001.13 и расширен в 001.14
  (36 тестов).

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Лимит на вход
   - Входные данные: 6 неверных паролей с одного адреса
   - Ожидаемый результат: 6-й — 429; учётная запись не блокируется
2. **TC-E2E-02:** Выход везде
   - Входные данные: две сессии; `logout-all`
   - Ожидаемый результат: обе недействительны

### Модульные тесты

1. **TC-UNIT-01:** Скользящее окно
   - Проверяемая функция: `app/security/ratelimit.py::RateLimiter.check`
   - Входные данные: лимит 5 за 60 с, 6 вызовов
   - Ожидаемый результат: шестой — ложь

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_auth.py -k 'login or session' tests/unit/security`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [x] AC-36 для входа — 001.14, тест TC-E2E-01, посадки 2 и 8 отчёта 001.14
- [x] Сессии инвалидируются в срок Н-27 — механизм: `revoke_all` удаляет ключи немедленно
      (сброс пароля, «выход везде»); вызов при блокировке пользователя передан в 001.50 (операция
      блокировки в `/admin/users`), 12 ч бездействия администратора — 001.47

## Примечания

`tdd-strict` для `sessions.py` и `ratelimit.py`.

Зависимости: 001.14. Приоритет: High. Оценка: 4 ч. Этап: 2 — пользователи и кабинет.
