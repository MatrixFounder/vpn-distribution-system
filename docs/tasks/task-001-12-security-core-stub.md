# Задача 001.12: Ядро безопасности: интерфейсы паролей, сессий, CSRF, лимитов, шифрования

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-02 Регистрация и активация подписки Redeem-кодом
- UC-15 Восстановление пароля
- UC-16 Работа в личном кабинете

Требования RTM: R-36, R-44, R-45.

<!-- contract:goal -->

## Цель задачи

Определить интерфейсы модулей безопасности с заглушками так, чтобы обработчики API могли их вызывать
до реализации логики.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/security/passwords.py` — `def hash_password(p: str) -> str`; `def verify_password(p: str, h: str) -> bool` — параметры по `docs/architectures/security.md` §7.2; вызов библиотеки без собственной логики
- `control-plane/app/security/sessions.py` — `class SessionStore`: `create(kind, subject_id, ip, ua, ttl) -> str`; `get(sid) -> Session | None`; `revoke(sid)`; `revoke_all(subject_id, except_sid=None)`; ключи `sess:{id}`, набор `user_sessions:{id}`
- `control-plane/app/security/csrf.py` — зависимость `require_csrf` для изменяющих методов
- `control-plane/app/security/ratelimit.py` — `class RateLimiter`: `check(key: str, limit: int, window_s: int) -> bool`; ключи по §5.12; при недоступности Redis — исключение `RateLimitUnavailable`
- `control-plane/app/security/crypto.py` — `class FieldCipher`: `encrypt(b: bytes) -> bytes`, `decrypt(b: bytes) -> bytes`, `key_version: int` — AES-256-GCM по §7.2; вызов библиотеки без собственной логики
- `control-plane/app/security/deps.py` — зависимости FastAPI: `current_user`, `current_admin`, `require_permission(name)` — заглушки
- `control-plane/tests/unit/security/test_passwords.py` — хеширование
- `control-plane/tests/unit/security/test_crypto.py` — шифрование

### Интеграция компонентов

`SessionStore` и `RateLimiter` получают клиент Redis из `app/redis.py`. `FieldCipher` читает ключ из
`Settings.encryption_key_path`.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Отказ при недоступности Redis закрыт
   - Входные данные: Redis остановлен; `GET /api/v1/auth/login`
   - Ожидаемый результат: 503 с `Retry-After`
   - Примечание: поведение fail-closed по §9.1 архитектуры

### Модульные тесты

1. **TC-UNIT-01:** Пароль проверяется
   - Проверяемая функция: `app/security/passwords.py::verify_password`
   - Входные данные: пароль и его хеш
   - Ожидаемый результат: истина; другой пароль — ложь
2. **TC-UNIT-02:** Шифрование обратимо
   - Проверяемая функция: `app/security/crypto.py::FieldCipher`
   - Входные данные: 32 байта
   - Ожидаемый результат: `decrypt(encrypt(x)) == x`; изменённый шифртекст даёт ошибку аутентичности

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/unit/security`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Интерфейсы всех пяти модулей объявлены с сигнатурами из этой задачи
- [ ] `hash_password` и `FieldCipher` — вызовы библиотек без собственной логики ветвления
- [ ] Сессии, CSRF, лимиты — заглушки с фиксированным поведением, помеченные `NotImplementedError` в логике

## Примечания

Логика сессий и лимитов — задача 001.14; второй фактор — 001.47.

Зависимости: 001.10. Приоритет: Critical. Оценка: 3 ч. Этап: 1 — схема данных и каркас Control
Plane.
