# Отчёт о проверке — задача 001.10 «Каркас FastAPI: конфигурация, пул, Redis, формат ошибок, OpenAPI»

Дата: 2026-09-09 (четыре раунда ревью, четвёртый — сверх лимита workflow по решению владельца:
редиректы по слэшу отключены, заголовки прокси перезаписываются nginx и контракт закреплён
тестом, страж `int ≥ 0` на всех курсорах, чтение секретов без обрезки пробелов — см. «Раунд 2»,
«Раунд 3», «Раунд 4»). Стенд: VM (`ssh vm`),
Compose `control-plane` (postgres 18.6, redis,
nginx, api). Тесты — с рабочей машины: приложение в процессе теста через `ASGITransport`, пул и
Redis — против стенда (`PG_DSN` под `app_rw`, `REDIS_URL` → VM:16379).

## Регрессия

- `make check` (nvm use 24, `PG_DSN`/`MIGRATE_DSN`/`REDIS_URL` → стенд) → код 0: ruff, ruff format,
  mypy strict (46 файлов), pytest **111 passed** (прежние 94 + 001.10: e2e 11, unit 6), go, web;
  `shellcheck docker-entrypoint.sh` — чисто.
- `cd control-plane && pytest tests/e2e/test_skeleton.py` → 11 passed; `pytest tests/unit/test_config.py`
  → 4 passed.
- `python -c "import app.main"` без переменных окружения — импорт чистый; `create_app().openapi()`
  → пути `/agent/v1/enroll`, `/agent/v1/state`, `/api/v1/admin/dashboard`, `/api/v1/auth/login`,
  `/api/v1/me`, `/s/{token}`; `info.version = 0.1.0`.

## Стенд

`vm-sync`, `up -d --build api` → **api `Up (healthy)`** впервые (healthcheck `/healthz` 200);
журнал: «Application startup complete», `GET /healthz 200`. nginx пересоздан после правки
конфигурации (`nginx -t` успешно; `--force-recreate` из-за bind-mount одного файла).

Через nginx с рабочей машины (`curl -k`):

```text
GET  https://VM/api/v1/nonexistent      → 404 application/json {"error":{"code":"not_found","message":"Not Found","details":{}}}
GET  https://VM/healthz                 → 200
GET  https://VM/openapi.json            → 200, шесть путей, version 0.1.0
POST https://VM/api/v1/auth/login       → 501 {"error":{"code":"not_implemented",…,"details":{"operation":"auth.login"}}}
GET  https://VM/s/abc                   → 501
GET  https://VM/metrics                 → 404 (публичный server; до правки было 200 — утечка метрик наружу)
GET  https://VM/s/sometoken/            → 404 без Location (раунд 1 отдавал 307 → http://VM/s/sometoken)
GET  https://VM/api/v1/me/              → 404 {"error":{"code":"not_found",…}}
GET  https://VM/api/v1/me с X-Forwarded-For: 203.0.113.77 и X-Forwarded-Proto: http
                                        → журнал api: 10.211.55.2 (адрес рабочей машины), подделка отброшена
GET  :9443 (dev-сертификат, с VM) с X-Forwarded-For: 203.0.113.88, 203.0.113.89
                                        → журнал api: 172.20.0.1 (шлюз сети Compose — источник запроса с VM)
GET  https://VM:9443/agent/v1/state     → 400 без клиентского сертификата (mTLS)
```

На VM с dev-сертификатом ноды (`--cert dev/dev-node.crt --key dev/dev-node.key`, порт 9443):
`/agent/v1/state?config_version=0&users_seq=0&generation=0` → 501 `agent.state`;
`?config_version=x` → 422 `validation_error` с `details.errors[{loc: [query, config_version], …}]`;
`/api/v1/me` на агентском порту → 404 nginx. Внутри сети `api:8000/metrics` → `control_plane_up 1`.

## Сквозные тесты (`control-plane/tests/e2e/test_skeleton.py`, 11 тестов)

1. **TC-E2E-01** `test_app_starts_and_publishes_schema`: приложение с `PG_DSN`/`REDIS_URL` на
   закрытые порты и несуществующим файлом ключа собирается, lifespan запускается вручную
   (`router.lifespan_context` — транспорт ASGI его не запускает) и `/healthz` → 200
   `{"status": "ok", "version": "0.1.0"}`; `/openapi.json` → 200, `info.version` = `__version__`,
   «v1» в описании, для каждого из префиксов `/api/v1/`, `/agent/v1/`, `/s/` есть путь, служебных
   маршрутов в схеме нет, `/docs` → 404.
2. **TC-E2E-02** `test_error_format`: `GET /api/v1/nonexistent` → 404 ровно
   `{"error": {"code": "not_found", "message": "Not Found", "details": {}}}`; `POST /healthz` →
   405 `method_not_allowed` с заголовком `Allow`; `GET /agent/v1/state?config_version=abc&users_seq=1`
   → 422 `validation_error`, в `details.errors` — `["query", "config_version"]` и
   `["query", "generation"]`, у каждой записи ровно `loc`, `msg`, `type`; для каждого из трёх
   курсоров `-1` при остальных `0` → 422 ровно `[(["query", <курсор>], "greater_than_equal")]`;
   `/s/sometoken/`, `/api/v1/me/`, `/healthz/` → 404 `not_found` без заголовка `Location`.
3. `test_stubs_return_501` ×6: каждая заглушка → 501 с точным телом (`not_implemented`, сообщение с
   именем операции, `details.operation`).
4. `test_unhandled_exception_is_masked`: маршрут, поднимающий `RuntimeError("секретная
   подробность")` → 500 `internal_error`, текста исключения в теле нет.
5. `test_metrics_exposition`: `/metrics` → 200 `text/plain`, содержит `# TYPE control_plane_up
   gauge\ncontrol_plane_up 1`.
6. `test_pool_and_redis_are_lazy_and_work` (стенд): `get_pool()` создаётся при первом вызове и
   один на процесс, `transaction()` — `current_user = app_rw`, `current_schema = control_plane`,
   вставка в `settings` внутри транзакции с исключением откатывается (строк 0; в `finally` —
   страховочное удаление и закрытие), `close_pool()` идемпотентен, закрытый пул →
   `InterfaceError`; `get_redis()` один на процесс, `ping() → True`, `close_redis()` идемпотентен.

## Модульные тесты — контракт прокси (`control-plane/tests/unit/test_proxy_contract.py`, 2 теста)

- `test_nginx_overwrites_forwarded_headers`: в `deploy/nginx/nginx.conf` без комментариев нет
  `$proxy_add_x_forwarded_for`; в каждом из трёх `server` с `proxy_pass $api` —
  `X-Forwarded-For $remote_addr`, `X-Real-IP $remote_addr`, `X-Forwarded-Proto $scheme|https`.
- `test_uvicorn_trusts_only_overwritten_headers`: обе команды запуска uvicorn в
  `docker-entrypoint.sh` (склеенные строки-продолжения) содержат `--proxy-headers
  --forwarded-allow-ips '*'`.

## Модульные тесты (`control-plane/tests/unit/test_config.py`, 4 теста) — TC-UNIT-01

- `test_settings_parse_stand_environment`: окружение стенда с настоящими файлами секретов во
  временном каталоге → `pg_dsn`, `redis_url`, `app_role = api`, `app_env = stand`,
  `subscription_domains = [sub1…, sub2…]` (пробелы и пустые элементы отброшены),
  `encryption_key_path`; `pg_dsn_with_password` = `postgresql://app_rw:%20p%40ss%3Aword%2F%231%20@…`
  (файл ` p@ss:word/#1 \r\n`: экранирование, перевод строки отброшен, краевые пробелы сохранены).
- `test_missing_or_empty_secret_fails_startup`: отсутствующий файл ключа → `SecretError`
  «недоступен»; файл ` \t\n` → `SecretError` «пуст»; отсутствующий файл пароля → `SecretError`.
- `test_invalid_values_are_rejected`: `APP_ROLE=cron` → `ValidationError`; без `PG_DSN` →
  `ValidationError`.
- `test_dsn_with_password_keeps_ipv6_and_explicit_password`: `[::1]` сохраняет скобки, пароль в
  URL не перезаписывается, без файла — DSN как есть.

## Посадки стражей (до ревью, по одной; файлы восстановлены после каждой, `diff` пуст)

| Посадка | Результат |
| :--- | :--- |
| без обработчика `HTTPException` | e2e: `{'detail': 'Not Found'}` ≠ `{'error': …}` |
| без обработчика необработанных исключений | e2e 500: тело не JSON (`JSONDecodeError`) |
| тело без обёртки `error` | e2e: `{'code': …}` ≠ `{'error': …}` |
| без роутера `/s` | e2e: `('/s/', [...])` — префикса нет в схеме |
| `docs_url="/docs"` | e2e: «интерактивной документации нет», `200 == 404` |
| `Settings.load()` без проверки файла ключа | unit: `DID NOT RAISE SecretError` |
| пустой секрет принимается | unit: `DID NOT RAISE SecretError` |
| `transaction()` без транзакции | e2e: «транзакция откатилась», `1 == 0` (строка осталась — снята страховкой) |
| lifespan открывает пул при старте | e2e: `SecretError: секрет недоступен: /nonexistent/key` |

Первый вариант теста пула оставил на стенде строку `settings.skeleton-probe` после посадки 8 —
удалена вручную, тест получил страховочное удаление в `finally`. Первый вариант стража старта не
запускал lifespan (транспорт ASGI его не вызывает) и посадку 9 не ловил — добавлен ручной
`lifespan_context`. После посадок: 15 passed (skeleton + config), `make check` → 0, 109 passed;
стенд — строк в `settings` кроме `state_generation` нет.

## Раунд 2 — правки по ревью и их проверка

Ревью раунда 1 (`sarcasmotron-001-10`, REJECTED): (1) редирект по завершающему слэшу отдавал
`Location: http://…/s/{token}` — токен Н-25 в открытый канал (uvicorn не доверял заголовкам
nginx, схема бралась из запроса); (2) объявленная граница курсоров `int ≥ 0` без стража (посадка
`ge=0` зелёная); (3) необъявленная смена `rstrip("\r\n")` → `strip()` в чтении секретов; стиль —
мёртвые `# noqa: ARG001`, ловушка `?full=1` §5.2 не отмечена.

- **(1)** `create_app(redirect_slashes=False)`: лишний слэш — 404 единого формата, редиректов
  нет вовсе; сверх того `docker-entrypoint.sh` запускает uvicorn с `--proxy-headers
  --forwarded-allow-ips '*'` — схема и адрес клиента из `X-Forwarded-Proto`/`X-Forwarded-For`
  (порт 8000 не публикуется, до `api` достаёт только сеть Compose — объявлено в задаче). Стенд
  после пересборки: `https://VM/s/sometoken/` → 404 без `Location`; журнал uvicorn показывает
  адрес рабочей машины (`10.211.55.2`), а не контейнера nginx — заголовки приняты.
- **(2)** `test_error_format`: `config_version=-1` → 422 ровно `greater_than_equal` по
  `["query", "config_version"]`.
- **(3)** `read_secret()` — `rstrip("\r\n")`, краевые пробелы часть секрета (как у Docker
  secrets); файл из одних пробелов — «пуст». Объявлено в задаче; unit-тест использует пароль
  ` p@ss:word/#1 \r\n` и файл ` \t\n`.
- Стиль: `# noqa: ARG001` удалены; форма `?full=1` отмечена в докстринге заглушки и в задаче.

Посадки раунда 2 (по одной, файлы восстановлены, `diff` пуст):

| Посадка | Результат |
| :--- | :--- |
| `redirect_slashes=True` | e2e: `/s/sometoken/` → `307 == 404` |
| без `ge=0` (посадка ревьюера) | e2e: «курсоры §5.2 — int ≥ 0», `501 == 422` |
| `strip()` вместо `rstrip("\r\n")` | unit: DSN без `%20` — «пробелы сохранены» |
| файл из пробелов принимается | unit: `DID NOT RAISE SecretError` |

После посадок: 15 passed; `make check` → 0, 109 passed; api на стенде пересобран — `healthy`.

## Раунд 3 — правки по ревью раунда 2 и их проверка

Ревью раунда 2 (`sarcasmotron-001-10`, REJECTED): CRITICAL — `--forwarded-allow-ips '*'` в uvicorn
0.52.4 (`always_trust`) берёт крайний левый элемент `X-Forwarded-For`, а nginx дополнял цепочку
(`$proxy_add_x_forwarded_for`) — подделка адреса клиента снаружи воспроизведена на стенде
(`203.0.113.77` в журнале api), у флага не было стража (посадка ревьюера зелёная); MAJOR — страж
`int ≥ 0` закрывал только `config_version` (посадка на `generation` зелёная).

- **Подделка адреса → перезапись в nginx.** Во всех трёх `server` `X-Forwarded-For $remote_addr`
  (было `$proxy_add_x_forwarded_for`): nginx — единственный и первый прокси, заголовок клиента
  отбрасывается, uvicorn видит ровно один адрес; `X-Forwarded-Proto` и раньше перезаписывался.
  `'*'` оставлен: он лишь принимает заголовки от любого узла сети Compose (порт 8000 не
  публикуется), а содержимое задаёт nginx. Обоснование в задаче переписано. Страж —
  `tests/unit/test_proxy_contract.py` (см. выше). Стенд: nginx пересоздан (`nginx -t` успешно),
  api пересобран; подделанные `X-Forwarded-For` (одиночный и цепочкой) и `X-Forwarded-Proto` в
  журнале api не появляются — см. блок «Через nginx».
- **Курсоры** — цикл по трём именам, для каждого `-1` → 422 `greater_than_equal` по своему `loc`.

Посадки раунда 3 (по одной, файлы восстановлены, `diff` пуст, права `docker-entrypoint.sh` на
месте):

| Посадка | Результат |
| :--- | :--- |
| агентский `server` снова дополняет XFF (`$proxy_add_x_forwarded_for`) | contract: «дополнение цепочки XFF — подделка адреса» |
| ветка `--workers` без `--proxy-headers --forwarded-allow-ips` (посадка ревьюера) | contract: строка запуска без `--proxy-headers` |
| ветка `--reload` без флагов | contract: то же |
| `ge=0` снят с `generation` (посадка ревьюера) | e2e: `generation`, `501 == 422` |

Первый вариант стража entrypoint проверял окно в 300 символов и видел флаги соседней ветки —
посадка на ветку `--workers` осталась зелёной; страж переписан на проверку каждой команды запуска
со склеенными строками-продолжениями, обе посадки красные. После посадок: 17 passed
(skeleton + config + contract); `make check` → 0, 111 passed.

## Раунд 3 — вердикт и правка после него (лимит раундов исчерпан)

`sarcasmotron-001-10`, раунд 3: **REJECTED** по одному пункту — страж `test_proxy_contract.py`
проверял наличие правильной строки на уровне `server` и оставался зелёным, когда
`proxy_set_header X-Forwarded-For $http_x_forwarded_for;` добавлялся внутрь `location ^~ /s/`
(по правилам наследования nginx одна директива `proxy_set_header` в `location` отменяет весь
набор уровня `server`, включая обнуление `X-Client-Fingerprint`). Подделка адреса на стенде
ревьюером больше не воспроизводится (четыре атаки: одиночный XFF, цепочка, `X-Real-IP`, по
порту 80 — везде адрес рабочей машины); курсоры закрыты на всех именах (посадка на `users_seq`
красная); `make check` → 0, 111 passed.

Правка внесена в рабочее дерево **после** третьего вердикта, без четвёртого ревью (лимит трёх
раундов, workflow vdd-03-develop): страж теперь сверяет **все** вхождения по файлу
(`re.findall` — `X-Forwarded-For` и `X-Real-IP` ровно `["$remote_addr"] * 3`,
`X-Forwarded-Proto` — три значения из `{$scheme, https}`) и запрещает любой `proxy_set_header`
внутри `location` трёх `server`, проксирующих на `$api` (сопоставление скобок для `server` и
`location`). Посадки: строка ревьюера в `location ^~ /s/` → красный (`$http_x_forwarded_for` в
списке); безобидный `proxy_set_header Host $host` внутри `location ^~ /agent/v1/` → красный
(«отменяет набор заголовков server»); `X-Real-IP $http_x_real_ip` в enrollment `server` →
красный. Первая версия правки не находила `location` вовсе (регулярное выражение требовало `{`
сразу после слова) — посадка с `Host` была зелёной, извлечение блоков переписано. После правки
`make check` → 0, 111 passed. Владелец разрешил четвёртый раунд.

## Раунд 4 — вердикт (по решению владельца, сверх лимита)

`sarcasmotron-001-10`: **APPROVED**. Проверено ревьюером: код приложения, `nginx.conf` и entrypoint
с раунда 3 не менялись (сверка md5), `make check` → 0, 111 passed; посадка раунда 3 (`X-Forwarded-For
$http_x_forwarded_for` внутри `location ^~ /s/`) → красная; четвёртый `server` в домашнем стиле
(`set $api` + `proxy_pass $api`) без заголовков → красная (`len(proxying) == 3`); на стенде
подделка `X-Forwarded-For` на `/s/probe` не проходит. Два остаточных наблюдения: (3) четвёртый
`server` с литеральным `proxy_pass http://api:8000` без заголовков страж пропускал — закрыто сразу
после вердикта одним символом (фильтр `"proxy_pass" in block`), посадка ревьюера P2a → красная
(`4 == 3`), `make check` → 0, 111 passed; (4) удаление `proxy_set_header X-Client-Fingerprint ""`
из enrollment-`server` стражем не ловится — правило границы 1 C-09 вне диффа 001.10, страж
ставит задача 001.2x, где появляется потребитель отпечатка (записано в задаче 001.10 как
передача).

## Критерии приёмки

- [x] `create_app()` собирает приложение без обращения к базе на импорте — импорт без окружения,
      сборка и lifespan с закрытыми портами и без файла ключа; посадка 9 красная
- [x] Формат ошибок соответствует `interfaces.md` §5.1 — 404/405/422/501/500 в теле
      `{"error": {"code", "message", "details"}}`; через nginx — то же; редиректов нет
- [x] OpenAPI публикуется и содержит версию API — `info.version = 0.1.0`, `v1` в описании и путях;
      `/docs` отключён

## Отклонения от описания задачи

- Обработчики единого формата покрывают и ошибки фреймворка (404/405/422/500), не только `ApiError`;
  заглушка `/agent/v1/state` типизирует курсоры §5.2, чтобы 422 проверялся на каркасе; `/healthz`
  — только живость (готовность — 001.68); `/metrics` — один показатель до 001.68 и закрыт на
  публичном `server` nginx (`deploy/nginx/nginx.conf`, `deploy/.AGENTS.md`).
- `read_secret()`/`dsn_with_password()` перенесены в `app/config.py`; `app/cli.py` использует их
  (пустой файл пароля миграций — ошибка старта, раньше давал пустой пароль).
- Фикстура `app_client` в `tests/conftest.py` заменена с пропуска на реальный клиент.
- Все пункты объявлены в «Уточнениях при реализации» задачи; карты `app/.AGENTS.md`,
  `app/api/.AGENTS.md` (новая), `control-plane/.AGENTS.md`, `deploy/.AGENTS.md` обновлены.
- Передача в 001.2x: страж правила границы 1 (`proxy_set_header X-Client-Fingerprint ""` в
  агентском и enrollment `server`) — вместе с потребителем отпечатка.
