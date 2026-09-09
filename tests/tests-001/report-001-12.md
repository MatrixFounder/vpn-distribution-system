# Отчёт о проверке — задача 001.12 «Ядро безопасности: интерфейсы паролей, сессий, CSRF, лимитов, шифрования»

Дата: 2026-09-09 (раунд 2 после ревью: страж таймаутов fail-closed, уборка слопа, объявление
`/agent/v1` — см. «Раунд 2»). Стенд: VM (`ssh vm`), Compose `control-plane` (все роли `Up`). Тесты —
с рабочей машины: модульные без стенда, сквозные через `ASGITransport` (Redis стенда для
положительного случая, закрытый порт — для отказа).

## Регрессия

- `make check` (nvm use 24, `PG_DSN`/`MIGRATE_DSN`/`REDIS_URL` → стенд, живые контейнеры) → код 0:
  ruff, ruff format, mypy strict (66 файлов), pytest **140 passed** (прежние 124 + 001.12: unit 14,
  e2e 2), go, web.
- `cd control-plane && pytest tests/unit/security` → 14 passed (без базы и Redis; ≈2 с — тест
  молчащего сокета ждёт таймаут команды 2 с).
- Ручная проба: `hash_password` → `$argon2id$v=19$m=65536,t=3,p=4$…`, `verify_password` true /
  false / false для мусорного хеша; `FieldCipher`: контейнер 61 байт для 32 байт текста,
  обратимость, подделка тега → `InvalidTag`; приложение с Redis стенда: `POST /api/v1/auth/login`
  → 501; с адресом на закрытый порт → 503 `service_unavailable`, `Retry-After: 5`, за 0,00 с;
  `/s/tok` → 503; `/api/v1/me` → 501 (без лимита).

## Стенд

`vm-sync`, `up -d --build` четырёх ролей → все `Up`, api `healthy`. Через nginx с Redis стенда:
`POST /api/v1/auth/login` → 501 `not_implemented`, `/s/abc` → 501. **Redis приостановлен**
(`docker compose pause redis`, собственный контейнер стенда, обратимо):

```text
POST https://VM/api/v1/auth/login → 503 {"error":{"code":"service_unavailable","message":"сервис временно недоступен","details":{}}}
                                    Retry-After: 5, ответ за 2,0 с (таймаут команды PING 2 с)
GET  https://VM/s/abc               → 503 за 2,0 с
GET  https://VM/api/v1/me           → 501 (лимита нет — от Redis не зависит)
```

`unpause` → `POST /api/v1/auth/login` снова 501; в журнале api строка `503 … Redis недоступен: …`
(подробности только в журнале).

## Сквозные тесты (`control-plane/tests/e2e/test_security_fail_closed.py`, 2 теста) — TC-E2E-01

1. `test_rate_limited_endpoints_fail_closed_without_redis`: `REDIS_URL` на закрытый порт →
   `POST /api/v1/auth/login` → 503, `Retry-After: 5`, тело ровно `{"error": {"code":
   "service_unavailable", "message": "сервис временно недоступен", "details": {}}}`, слова
   «Redis» в ответе нет; `GET /s/sometoken` → 503; `GET /api/v1/me` → 501; `/healthz` → 200.
2. `test_rate_limited_endpoints_answer_when_redis_is_up`: с Redis стенда `login` → 501
   `not_implemented`, `/s/sometoken` → 501.

## Модульные тесты (`control-plane/tests/unit/security/`, 14 тестов)

- **TC-UNIT-01** `test_passwords.py`: верный пароль → истина, изменённый и пустой → ложь; хеш
  начинается с `$argon2id$v=19$m=65536,t=3,p=4$` (64 MiB, t = 3, p = 4 — §7.2), юникод; два хеша
  одного пароля различаются (соль); мусорный и пустой хеш → ложь без исключения.
- **TC-UNIT-02** `test_crypto.py`: 32 байта → `decrypt(encrypt(x)) == x`, контейнер `1 + 12 + 32 +
  16` байт, версия 1, два контейнера различаются (nonce), пустой текст обратим; подделка байта в
  nonce, в шифртексте и в теге, усечение, пустой контейнер, чужой ключ, чужая версия ключа →
  `InvalidTag`; версия 2 со своим ключом обратима; ключ 16 байт и версия 0/256 → `ValueError`;
  `from_settings`: base64-ключ из файла работает, не base64 и 16 байт → `SecretError`.
- `test_stubs.py`: `SessionStore.create/get/revoke/revoke_all` → `NotImplementedError`, ключи
  `sess:` и `user_sessions:`, сигнатура `create(kind, subject_id, ip, ua, ttl)`; `require_csrf`:
  GET/HEAD/OPTIONS проходят, POST → `NotImplementedError`, заголовок `X-CSRF-Token`; ключи §5.12
  (`rl:login:ip:…`, домен и почта в нижнем регистре, `rl:subscription-unknown:ip:…`,
  `rl:enroll:ip-token:…`); `RateLimiter` на закрытый порт: `ensure_available` и `check` →
  `RateLimitUnavailable`; `test_fail_closed_is_bounded_when_redis_accepts_but_never_answers`:
  сокет на локальном порту принимает соединение и молчит → клиент из `get_redis()` даёт
  `RateLimitUnavailable` за < 5 с (таймаут команды 2 с), а не висит;
  `current_user`/`current_admin`/`require_permission("users.read")` → `NotImplementedError` с
  именем разрешения.

## Посадки стражей (до ревью, по одной; файлы восстановлены после каждой, `diff` пуст)

| Посадка | Результат |
| :--- | :--- |
| argon2 `m = 32 MiB` вместо 64 | unit: хеш `m=32768` ≠ `m=65536` |
| `argon2i` вместо `argon2id` | unit: `$argon2i$` |
| версия ключа вне AAD при шифровании | unit: `InvalidTag` при расшифровке |
| `decrypt` не сверяет версию ключа | unit: `DID NOT RAISE InvalidTag` (чужая версия) |
| фиксированный nonce | unit: «nonce случайный — контейнеры различаются» |
| `from_settings` без проверки длины | unit: `ValueError` вместо `SecretError` (33 байта прошли в конструктор) |
| 503 без `Retry-After` | e2e: `KeyError: 'retry-after'` |
| `/auth` без `redis_required` | e2e: `501 == 503` |
| 503 с текстом исключения | e2e: тело содержит адрес Redis |
| `require_csrf` пропускает POST | unit: `DID NOT RAISE NotImplementedError` |

После посадок: 15 passed (unit security + e2e), `make check` → 0, 139 passed.

## Раунд 2 — правки по ревью и их проверка

Ревью раунда 1 (`sarcasmotron-001-12`, REJECTED): C-1 — таймауты клиента Redis (1 с / 2 с) не
имели стража: тест отказа моделировал закрытый порт (мгновенный `ECONNREFUSED`), и посадка
«без таймаутов» осталась зелёной; слоп — мёртвый `# noqa: A002`, развилка с одинаковыми
ветками в `close_redis`, `(binascii.Error, ValueError)`, магическое 16; необъявленное
исключение `/agent/v1` из fail-closed.

- **C-1:** новый unit-тест `test_fail_closed_is_bounded_when_redis_accepts_but_never_answers` —
  `asyncio.start_server` на случайном порту принимает соединение и молчит; `RateLimiter` поверх
  клиента `get_redis()` → `RateLimitUnavailable` за < 5 с. Посадка ревьюера (обе строки
  таймаутов удалены) → «таймаут команды не ограничен» (ждали 5 с и прервали). Первый вариант
  теста держал соединение `sleep(30)` и тормозил уборку на 30 с — заменён на событие.
- **Слоп:** параметр `check` переименован в `counter_key` (тень модульной функции `key`),
  `noqa` удалён; `close_redis` — один `suppress`, `_loop` сбрасывается и в `close_redis`, и в
  `close_pool`; `except ValueError` (binascii.Error — подкласс); `TAG_BYTES = 16` в модуле и
  тесте.
- **`/agent/v1`:** объявлено в задаче — состав fail-closed по security.md §7.3, лимит Node API
  по identity — с самим Node API (001.2x).

После правок: `make check` при живых контейнерах → 0, 140 passed; стенд пересобран, все роли
`Up`.

### Вердикт раунда 2

`sarcasmotron-001-12`: **APPROVED**. Ревьюер пересадил регрессию таймаутов трижды: обе строки →
красный за 5,3 с, только `socket_timeout` → красный, только `socket_connect_timeout` → зелёный
(молчащий сокет соединение принимает — структурная граница поведенческого стража; рекомендация
— статическая проверка конфигурации клиента). Все пять пунктов слопа закрыты по коду; `make
check` → 0, 140 passed; unit security 14 passed за 2,2 с. После вердикта, без нового раунда:
в тест молчащего сокета добавлена проверка `connection_kwargs` клиента (`socket_connect_timeout`
1.0, `socket_timeout` 2.0) — посадка «без `socket_connect_timeout`» теперь красная (`KeyError`);
локальные импорты теста подняты в заголовок; заголовок раздела модульных тестов исправлен на 14.

## Критерии приёмки

- [x] Интерфейсы всех пяти модулей объявлены с сигнатурами из этой задачи — `passwords`,
      `sessions`, `csrf`, `ratelimit`, `crypto` (+ `deps`); сигнатуры проверены тестами
- [x] `hash_password` и `FieldCipher` — вызовы библиотек без собственной логики ветвления —
      argon2-cffi и cryptography `AESGCM`; своя часть — только формат контейнера и перевод
      исключений
- [x] Сессии, CSRF, лимиты — заглушки с фиксированным поведением, помеченные
      `NotImplementedError` в логике — проверено тестами; CSRF пропускает только безопасные
      методы, `RateLimiter.check` сначала проверяет доступность Redis

## Отклонения от описания задачи

- `redis_required` (сверх контракта) — зависимость fail-closed на роутерах `/auth` и `/s`; 503 с
  `Retry-After: 5`; таймауты клиента Redis 1 с / 2 с; вход — `POST` (описка `GET` в контракте);
  ключ `enrollment` по security.md §7.1; имя `RateLimitUnavailable` — из контракта (`noqa N818`);
  `get_pool`/`get_redis`/`close_*` учитывают цикл событий (тесты; в рантайме цикл один);
  autouse-фикстура `app_environment` в `tests/conftest.py`. Всё объявлено в «Уточнениях при
  реализации» задачи; карты `app/.AGENTS.md`, `control-plane/.AGENTS.md`.
- Redis стенда для теста отказа не останавливается (общий ресурс): отказ моделируется закрытым
  портом; поведение с реально остановленным Redis показано вручную (`pause`/`unpause`).
