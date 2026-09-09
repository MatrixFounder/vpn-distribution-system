# Отчёт о проверке — задача 001.15 «API кабинета `/api/v1/me`: маршруты и сквозные тесты на заглушках»

Дата: 2026-09-09 (раунд 3 после двух ревью — см. «Раунд 2» и «Раунд 3»; разделы ниже
описывают текущее состояние кода). Стенд: VM (`ssh vm`), Compose `control-plane` — все роли `Up`, `api`
healthy. Тесты — с рабочей машины через `ASGITransport` против живых базы и Redis стенда
(сессия пользователя — настоящая, через регистрацию, подтверждение и вход 001.14).

## Регрессия

- `make check` (nvm use 24, `PG_DSN`/`MIGRATE_DSN`/`REDIS_URL` → стенд) → код 0: ruff, ruff
  format, mypy strict (80 файлов), pytest **214 passed** (прежние 193 + 001.15: 22 − 1 снятая
  заглушка `me.profile`), go, web.
- `cd control-plane && pytest tests/e2e/test_me.py` → 21 passed.

## Стенд (через nginx, `curl -k https://127.0.0.1`, сессия после register → verify → login)

```text
GET  /me без cookie                       → 401 unauthenticated
GET  /me                                  → 200 {"id": <id сессии>, "email": "user@example.com", "language": "en", "timezone": "UTC", …}
GET  /me/subscription                     → 200, links: https://sub1.example.com/s/<token>, https://sub2.example.com/s/<token>
POST /me/subscription/reissue (CSRF, без confirm) → 409 confirmation_required «прежняя ссылка перестанет работать немедленно…»
POST /me/subscription/reissue?confirm=true без X-CSRF-Token → 403
GET  /me/traffic                          → 200, поля: active_ips, billable, by_country, by_node, device_limit, limit, period, raw_downlink, raw_total, raw_uplink, remaining; multiplier "2.0"
PATCH /me {"language":"ru","timezone":"Europe/Amsterdam"} (CSRF) → 200 с новыми значениями
GET  /me/onboarding (iPhone UA)           → 200, platform "unknown" (заглушка), клиенты §4.2 ×5
```

Следы прогона убраны (пользователь, события, задачи `send_email`, сессия, ключи).

## Сквозные тесты (`control-plane/tests/e2e/test_me.py`, 21 тест; `_me.py` — операции и тела)

1. `test_eight_operations_in_openapi_with_schemas`: ровно восемь операций `/me` (три на пути
   `/me` — GET/PATCH/DELETE — и пять на подпутях); `TrafficStats` = состав §4.12 (`period`,
   `raw_uplink`, `raw_downlink`, `raw_total`, `billable`, `limit`, `remaining`, `by_node`,
   `by_country`, `active_ips`, `device_limit`), `NodeTraffic` с `multiplier`, `CountryTraffic`,
   `SubscriptionOut.state` — enum, сверенный с `enum_range(null::subscription_state)` базы,
   `Profile`, `OnboardingOut`; у всех операций кроме `DELETE` — JSON-ответ 200; у операций без
   тела нет `requestBody`, схемы `Settings` в компонентах нет.
2. `test_operations_require_a_user_session` ×8: без cookie и с несуществующим `sid` → 401
   `unauthenticated` без cookie в ответе, даже с валидным телом и `confirm=true`.
3. `test_every_mutation_requires_csrf` ×4 (`PATCH /me`, `DELETE /me`, `redeem`, `reissue`): под
   живой сессией без `X-CSRF-Token` → 403 `csrf_failed`; параметризовано по списку мутаций.
4. `test_admin_session_is_not_a_cabinet_user`: сессия вида `admin` с UUID-субъектом → 401.
5. `test_profile_read_and_update`: `GET /me` → профиль под идентификатором сессии (значения —
   заглушка); `PATCH` с CSRF → 200 с новыми языком, поясом и согласием; `de`, `Mars/Olympus`,
   пустой пояс, лишнее поле → 422.
6. `test_uc14_delete_requires_confirmation`: без `confirm` → 409 `confirmation_required`
   (`details.confirm`), с `confirm=true` → 204; запись не удалена (заглушка; логика — 001.17).
7. **TC-E2E-02** `test_subscription_redeem_and_reissue`: подписка активна, `remaining = limit −
   used_billable`, ссылки на `sub1.example.com` и `sub2.example.com` с одним токеном;
   перевыпуск без `confirm=true` → 409 с предупреждением о разрыве устройств; с подтверждением →
   200 и другой токен на обоих доменах; `redeem` → 200; короткий код, лишнее поле, пустое тело →
   422.
8. **TC-E2E-01** `test_traffic_stats_composition`: все поля схемы; `raw_total = uplink +
   downlink = 10 ГБ`; по ноде `multiplier "2.0"`, `billable = 2 × raw`; `billable` = сумма по нодам
   = 20 ГБ; `remaining = limit − billable`; разбивка по стране; `active_ips`/`device_limit` = 1/3;
   `?period=<uuid периода>` — тот же ответ; `?period=current` → 422.
9. `test_onboarding_wizard`: пять клиентов §4.2 со ссылками `https://`, ссылки на двух доменах,
   `qr_payload` = первая ссылка, `import_url` = `sing-box://import-remote-profile?url=` + ссылка
   в процентном кодировании (сверяется раскодированное значение).
10. `test_missing_subscription_domains_is_a_loud_configuration_error`: пустой
    `SUBSCRIPTION_DOMAINS` → 500 `internal_error` на подписке и мастере, а не 200 с пустыми
    ссылками (на старте роли `api` это `ValidationError` — `unit/test_config.py`).
11. `test_redis_failure_mid_request_is_503_not_500`: под живой сессией `SessionStore.get`
    подменён на отказ подключения Redis — все восемь операций → 503 `service_unavailable` с
    `Retry-After: 5` (вторая линия fail-closed).

Скорректированы: `test_security_fail_closed.py` — все восемь операций `/api/v1/me` без Redis →
503 с `Retry-After` и без cookie (сессии в Redis), независимость показывает
`/api/v1/admin/dashboard`; `test_skeleton.py::test_stubs_return_501` — без `me.profile`;
`test_stubs.py::test_current_user_active_other_deps_stubbed` — `current_user` без cookie →
`ApiError` 401 без обращения к Redis; помощники разбора cookie перенесены в `_auth.py`.

## Посадки стражей (до ревью, по одной; файлы восстановлены после каждой, `cmp` совпадает)

| Посадка | Результат |
| :--- | :--- |
| 1 `/me/traffic` без зависимости `current_user` | e2e: `500 == 401` (маршрут без сессии) |
| 2 `PATCH /me` без `require_csrf` | e2e: `200 == 403` |
| 3 перевыпуск без подтверждения | e2e: `200 == 409` (TC-E2E-02) |
| 4 `TrafficStats` без `device_limit` | e2e: «состав §4.12», множества полей различаются |
| 5 ссылка только на первом домене | e2e: «два домена §4.2» |
| 6 роутер `/me` без `redis_required` | fail-closed: `401 == 503` |
| 7 сессия администратора открывает кабинет | e2e: `200 == 401` — после исправления теста: первый вариант использовал не-UUID субъект и был зелёным по ошибке разбора, а не по проверке вида сессии |
| 8 списано без коэффициента (`billable = raw`) | e2e: «пример §4.12: 10 ГБ × 2.0 = 20 ГБ» |
| 9 удаление без подтверждения | e2e: `204 == 409` (UC-14 шаг 1) |

После посадок: 24 passed (`test_me`, fail-closed, `test_stubs`).

## Раунд 2 — правки по ревью и их проверка

Ревью раунда 1 (`sarcasmotron-001-15`, REJECTED): две посадки ревьюера зелёные — C-1 `redeem`
без `require_csrf` не ловился (CSRF проверялся ad hoc на трёх мутациях из четырёх); C-2
fail-closed проверялся на одной операции из восьми, а отказ Redis посреди запроса с cookie давал
500 без `Retry-After`; S-1 `zoneinfo.available_timezones()` на каждый `PATCH` (~10 мс обхода
tzdata в цикле событий); S-2 `Settings.load()` на запрос читает файл ключа шифрования, а
`SecretError` дал бы 500; L-1 пустой `SUBSCRIPTION_DOMAINS` → 200 с пустыми ссылками; D-1
`user_traffic(None, …)`; D-2 `import_url` без процентного кодирования; N-1…N-6 (декоративные
ассерты, enum из копии, импорт помощника из тестового модуля, `emails.copy().pop()`, «банка»,
имя теста).

- **C-1:** `test_every_mutation_requires_csrf` параметризован по `MUTATIONS` (четыре мутации) с
  валидным телом каждой; посадка ревьюера (`redeem` без CSRF) → `200 == 403`.
- **C-2:** fail-closed тест обходит все восемь операций (503, `Retry-After: 5`, без cookie);
  вторая линия — `RedisError` → 503 в `errors.py`. Посадки: роутер без `redis_required` →
  `401 == 503` (анонимный запрос до Redis не доходит — страж различает первую линию); обработчик
  снят при живой первой линии → зелёный, как и должно; обе линии сняты → красный.
- **S-1:** `IANA_TIMEZONES = frozenset(zoneinfo.available_timezones())` при импорте модуля.
- **S-2 / L-1:** `Settings.read()` — окружение без файлов секретов; пустые домены →
  `RuntimeError` → 500 единого формата (тест 10; посадка «тихий 200» → `200 == 500`); кэш
  настроек — 001.16 (объявлено).
- **D-1:** статистика получает подключение из пула (`Pool` → `pool.acquire()`), сигнатура
  `user_traffic(conn: asyncpg.Connection, …)`. **D-2:** `quote(first, safe="")`, тест сверяет
  раскодированное значение и отсутствие `/` в параметре; посадка (без кодирования) → красная.
- **N-1…N-6:** декоративные ассерты сняты; enum подписки — из `enum_range` базы; `cookies_of`
  и `cookie_attributes` перенесены в `_auth.py`; адрес возвращает `logged_in` (`Cabinet`);
  «банка» → «хранилище cookie клиента httpx»; тест переименован.

Посадки раунда 2 (все восстановлены, `cmp` совпадает): 10 `redeem` без CSRF → `200 == 403`;
11a/11b/11c — линии fail-closed (см. выше); 12 пустые домены → `200 == 500`; 13 ссылка импорта
без кодирования → «процентное кодирование ссылки в параметре импорта».

## Раунд 3 — правки по ревью раунда 2

Ревью раунда 2 (`sarcasmotron-001-15`, REJECTED): C-1 вторая линия fail-closed (обработчик
`RedisError`) не охранялась — снята целиком, 212 passed; S-1 `Depends(get_pool)` напрямую:
параметр `settings: Settings | None` у `get_pool` стал телом запроса `GET /me/traffic`, а схема
`Settings` (имена `PG_DSN`, `APP_ENCRYPTION_KEY_FILE`, …) попала в публичный `/openapi.json`;
N-1 весь `RedisError` → 503 глушит диагностику; N-2 конфигурационная ошибка ловится поздно;
N-3 импорт констант из тестового модуля; N-4 docstring `current_user`.

- **C-1:** `test_redis_failure_mid_request_is_503_not_500` — под живой сессией подмена
  `SessionStore.get` на `redis.exceptions.ConnectionError`; все восемь операций → 503 с
  `Retry-After`. Посадка ревьюера (обработчик снят) → `500 == 503`.
- **S-1:** зависимость `db_pool()` в `db/pool.py` (обёртка без параметров), `Depends(get_pool)`
  убран и из `me.py`, и неиспользуемый — из `auth.py`; тест контракта: у операций без тела нет
  `requestBody`, `Settings` не в `components.schemas`. Посадка (снова `Depends(get_pool)`) →
  «`requestBody` not in …» красная.
- **N-1:** обработчик сужен до `redis.exceptions.ConnectionError`/`TimeoutError`; прочие
  `RedisError` — 500 с трассировкой. **N-2:** валидатор `Settings`: роль `api` без доменов не
  стартует (`unit/test_config.py`, посадка → «DID NOT RAISE»). **N-3:** `tests/e2e/_me.py`.
  **N-4:** docstring `current_user` называет вторую линию.

После правок: `make check` → 0, **214 passed**; стенд пересобран, `api` healthy.

## Критерии приёмки

- [x] Восемь маршрутов в OpenAPI — тест 1 (ровно восемь операций), стенд
- [x] Схема `TrafficStats` соответствует §4.12 — тест 1 (состав), тест 7 (значения примера),
      посадки 4 и 8
- [x] Тесты проходят на заглушках — 21 passed; полный набор 214 passed

## Отклонения от описания задачи

- `current_user` реализован здесь (перенос из 001.14): сессия вида `user` по cookie `sid`, 401
  иначе; статус пользователя в базе на каждом запросе не перепроверяется (объявлено).
- Раздел `/me` — fail-closed без Redis (503), заглушка `me.profile` 001.10 снята; мутации под
  CSRF; `confirm=true` как параметр запроса для перевыпуска и удаления; `TrafficStats` сверх
  контракта — `raw_total`, `limit`, `period`; `redeem` — заглушка без лимитов §5.12 (001.20);
  онбординг — платформа `unknown` (001.16). Всё объявлено в «Уточнениях при реализации»; карты
  `app/api/.AGENTS.md`, `app/.AGENTS.md`, `control-plane/.AGENTS.md`.
