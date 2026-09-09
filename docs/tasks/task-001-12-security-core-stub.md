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

Уточнения при реализации:

- `passwords.py`: `PasswordHasher(memory_cost=65536, time_cost=3, parallelism=4, type=ID)` —
  §7.2; `verify_password` возвращает ложь и при несовпадении, и при негодном хеше
  (`VerificationError`, `InvalidHashError`) — единственное «ветвление» — перевод исключений
  библиотеки в `bool`;
- `crypto.py`: `FieldCipher(key: bytes, key_version=1)` поверх `cryptography` `AESGCM`; формат
  контейнера `key_version (1 байт) || nonce (12 байт, случайный) || шифртекст+тег`, версия — в
  AAD; `decrypt` чужой версии, усечённого или подделанного контейнера → `InvalidTag`; ключ —
  ровно 32 байта, версия 1…255 (`ValueError`); `FieldCipher.from_settings(settings)` читает файл
  `encryption_key_path` как base64 от 32 байт (`openssl rand -base64 32`, как в
  `deploy/compose/secrets/README.md`) — иначе `SecretError`; ротация по `key_version` — позже;
- `sessions.py`: `Session` (frozen dataclass), `SessionKind = user | admin`, ключи `sess:{id}`,
  `user_sessions:{subject_id}`; методы `SessionStore` поднимают `NotImplementedError` (001.14);
- `csrf.py`: `require_csrf` — безопасные методы (GET/HEAD/OPTIONS) проходят, изменяющие →
  `NotImplementedError` (001.14): заглушка падает громко, а не пропускает молча;
- `ratelimit.py`: имя `RateLimitUnavailable` — из контракта (`noqa N818`); ключи §5.12 —
  функции `login_ip`, `login_account`, `register_ip`, `register_email_domain`, `reset_email`,
  `resend_email`, `subscription_token`, `subscription_unknown_ip`, `redeem_account`, `redeem_ip`,
  `token_reissue_account`, `api_account`, `node_identity`, плюс `enrollment(ip, token_hash)` по
  security.md §7.1 (§5.12 ключа для запроса без identity не задаёт); формат
  `rl:<операция>:<измерение>:<значение>`; `RateLimiter.ensure_available()` — `PING`, отказ →
  `RateLimitUnavailable`; `check()` сначала `ensure_available()`, затем `NotImplementedError`
  (счётчик — 001.14);
- `deps.py`: `current_user`, `current_admin`, `require_permission(name)` → `NotImplementedError`
  (001.14 / 001.47 / 001.46); сверх контракта `redis_required` — зависимость fail-closed (§9.1):
  подключена к роутерам `/api/v1/auth` и `/s` (операции с лимитом частоты §5.12), поэтому уже
  заглушки отвечают 503 при недоступном Redis; `/api/v1/me` и `/admin` не ограничены до
  появления сессий (001.14/001.16); `/agent/v1` fail-closed не подключает намеренно —
  security.md §7.3 задаёт состав fail-closed как `/s/{token}`, вход, восстановление, активацию
  кодов; лимит Node API по identity (§5.12) добавит задача 001.2x вместе с самим Node API;
- `errors.py`: обработчик `RateLimitUnavailable` → 503 `service_unavailable` с `Retry-After: 5`,
  подробности отказа — в журнал, не клиенту; `app/redis.py`: таймауты подключения 1 с и
  команды 2 с — fail-closed отвечает быстро, а не висит (страж: сокет, принимающий соединение и
  молчащий, — `RateLimitUnavailable` за < 5 с; ревью раунда 1 показало, что закрытый порт эту
  половину поведения не проверяет);
- TC-E2E-01: вход — `POST /api/v1/auth/login` (§5.1; `GET` в контракте — описка); «Redis
  остановлен» в тестах моделируется адресом на закрытый порт (Redis стенда общий), на стенде
  проверено `docker compose pause redis` через nginx;
- сопутствующее: `get_pool()`/`get_redis()`/`close_*` учитывают цикл событий — объект чужого
  (закрытого) цикла отбрасывается и создаётся заново (под uvicorn и в исполнителях цикл один; в
  тестах цикл на каждый тест, иначе «Event loop is closed»); `tests/conftest.py` — autouse-фикстура
  `app_environment` даёт полное окружение `Settings` (роль `api`, временный файл ключа), потому что
  ленивые пул и Redis загружают настройки целиком.

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
