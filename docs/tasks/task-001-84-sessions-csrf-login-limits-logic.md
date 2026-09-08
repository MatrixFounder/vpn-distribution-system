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

- [ ] AC-36 для входа
- [ ] Сессии инвалидируются в срок Н-27

## Примечания

`tdd-strict` для `sessions.py` и `ratelimit.py`.

Зависимости: 001.14. Приоритет: High. Оценка: 4 ч. Этап: 2 — пользователи и кабинет.
