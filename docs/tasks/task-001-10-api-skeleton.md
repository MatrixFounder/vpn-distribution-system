# Задача 001.10: Каркас FastAPI: конфигурация, пул, Redis, формат ошибок, OpenAPI

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-03 Получение подписки клиентом (общий каркас)
- UC-16 Работа в личном кабинете (общий каркас)

Требования RTM: R-01, R-50, R-51.

<!-- contract:goal -->

## Цель задачи

Поднять приложение C-01 с маршрутизаторами `/api/v1`, `/agent/v1`, `/s`, единым форматом ошибок и
публикацией OpenAPI; все обработчики — заглушки.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/main.py` — `create_app() -> FastAPI`: подключение роутеров, middleware ошибок, `/healthz`, `/metrics`
- `control-plane/app/config.py` — `class Settings(BaseSettings)`: `pg_dsn`, `redis_url`, `app_role`, `subscription_domains: list[str]`, `encryption_key_path`
- `control-plane/app/db/pool.py` — `async def get_pool() -> asyncpg.Pool`; `@asynccontextmanager async def transaction(pool) -> Connection`
- `control-plane/app/redis.py` — `async def get_redis() -> redis.asyncio.Redis`
- `control-plane/app/errors.py` — `class ApiError(Exception)` с `code`, `status`, `details`; обработчик → `{"error": {...}}`
- `control-plane/app/api/router.py` — `router = APIRouter(prefix="/api/v1")` с подроутерами `auth`, `me`, `admin` (заглушки 501)
- `control-plane/app/agent_api/router.py` — `router = APIRouter(prefix="/agent/v1")` (заглушки 501)
- `control-plane/app/subscription/router.py` — `router = APIRouter(prefix="/s")` (заглушка 501)
- `control-plane/tests/e2e/test_skeleton.py` — сквозные проверки каркаса
- `control-plane/app/api/.AGENTS.md` — карта пакета `api`
- `control-plane/app/i18n.py` — `negotiate(user, accept_language) -> str` — заглушка, возвращает `en`

### Интеграция компонентов

Все последующие STUB-задачи регистрируют роутеры в `app/api/router.py`, `app/agent_api/router.py`,
`app/subscription/router.py`. OpenAPI доступна по `/openapi.json`.

Уточнения при реализации:

- `Settings` читает переменные с именами из `.env.example`/docker-compose (`PG_DSN`,
  `PG_PASSWORD_FILE`, `REDIS_URL`, `APP_ROLE`, `APP_ENV`, `LOG_LEVEL`, `SUBSCRIPTION_DOMAINS` через
  запятую, `APP_ENCRYPTION_KEY_FILE`); `encryption_key_path` — путь к файлу ключа, сам ключ не
  читается в настройки; `Settings.load()` проверяет файлы секретов (`SecretError` — ошибка старта;
  пустой файл или файл из одних пробелов тоже ошибка); `read_secret()` отбрасывает только
  завершающие переводы строки (`\r\n`), краевые пробелы — часть секрета, как у Docker secrets;
  `read_secret()`/`dsn_with_password()` вынесены в `app/config.py`, и `app/cli.py` использует их
  (пустой файл пароля миграций теперь ошибка, а не пустой пароль);
- `get_pool()`/`get_redis()` — ленивые синглтоны процесса (`min_size=1`, `max_size=10`,
  `command_timeout=30`; Redis `decode_responses=True`), с `close_pool()`/`close_redis()` в
  lifespan; `transaction(pool)` — `acquire` + `transaction` (commit/rollback). Сборка приложения и
  lifespan к базе не обращаются: страж запускает lifespan с закрытыми портами;
- формат ошибок распространён и на ошибки фреймворка: 404/405 (`not_found`,
  `method_not_allowed`, заголовок `Allow` сохраняется), 422 (`validation_error`,
  `details.errors[loc, msg, type]`), 500 (`internal_error`, без текста исключения — он в
  журнале); `not_implemented(operation)` — 501 с `details.operation`;
- заглушки: `POST /api/v1/auth/login`, `GET /api/v1/me`, `GET /api/v1/admin/dashboard`,
  `POST /agent/v1/enroll`, `GET /agent/v1/state` (курсоры `config_version`, `users_seq`,
  `generation` — обязательные `int ≥ 0` по §5.2, чтобы формат 422 проверялся на каркасе),
  `GET /s/{token}`; остальные маршруты §5.1–5.3 добавляют их STUB-задачи;
- `/healthz` — живость процесса (healthcheck Compose); готовность базы и Redis — задача 001.68
  вместе с настоящими метриками; `/metrics` — текст Prometheus с одним показателем
  `control_plane_up`, наружу не публикуется: в `deploy/nginx/nginx.conf` публичный `server`
  отдаёт на него 404 (C-10 снимает метрики внутри сети Compose с `api:8000`);
- `/docs` и `/redoc` отключены (R-50 — схема `/openapi.json`); версия приложения
  `app.__version__` = `0.1.0` → `info.version`, версия API — в путях и описании;
- редиректов по завершающему слэшу нет (`redirect_slashes=False`): 307 строится из схемы
  запроса и за прокси без доверенных заголовков отдавал `http://…/s/{token}` — токен Н-25 в
  открытый канал (ревью раунда 1); лишний слэш — 404 единого формата. Сверх того uvicorn в
  `docker-entrypoint.sh` запускается с `--proxy-headers --forwarded-allow-ips '*'`: схема и
  адрес клиента берутся из `X-Forwarded-Proto`/`X-Forwarded-For` (лимиты частоты,
  `subscription_access_log`). Контракт с `deploy/nginx/nginx.conf` (ревью раунда 2): nginx
  **перезаписывает** оба заголовка (`$remote_addr`, `$scheme`/`https`), а не дополняет цепочку
  `$proxy_add_x_forwarded_for` — при `'*'` uvicorn берёт крайний левый элемент цепочки, и
  дополнение позволило бы клиенту подставить любой адрес; с перезаписью в заголовке ровно одно
  значение, заданное nginx, а `'*'` лишь принимает его от любого узла сети Compose (порт 8000
  не публикуется). Страж — `tests/unit/test_proxy_contract.py` (статически: во всех трёх
  `server` `X-Forwarded-For $remote_addr`, `$proxy_add_x_forwarded_for` в файле нет, обе ветки
  запуска uvicorn с флагами); на стенде подделанный заголовок в журнале api не появляется;
- заглушка `/agent/v1/state` требует курсоры; форма `?full=1` без курсоров (§5.2, полный снапшот)
  до 001.2x даёт 422;
- передача в 001.2x (ревью раунда 4): страж правила границы 1 C-09 — обнуление
  `X-Client-Fingerprint` в агентском и enrollment `server` nginx — добавить вместе с потребителем
  отпечатка; `tests/unit/test_proxy_contract.py` сегодня утверждает только заголовки адреса и
  схемы;
- `app/i18n.py` — `negotiate()` всегда `en`; `SUPPORTED_LANGUAGES = ("en", "ru")` для 001.65;
- модуль `app/redis.py` совпадает по имени с пакетом `redis` — внутри только абсолютные импорты;
- фикстура `app_client` — `ASGITransport(raise_app_exceptions=False)` (ответ 500 проверяется как
  ответ), без lifespan; после 001.10 роль `api` на стенде проходит healthcheck.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Приложение стартует и публикует схему
   - Входные данные: `GET /healthz`, `GET /openapi.json`
   - Ожидаемый результат: 200; в схеме присутствуют три префикса маршрутов
2. **TC-E2E-02:** Единый формат ошибок
   - Входные данные: `GET /api/v1/nonexistent`
   - Ожидаемый результат: 404 с телом `{"error": {"code": "not_found", ...}}`

### Модульные тесты

1. **TC-UNIT-01:** Загрузка настроек
   - Проверяемая функция: `app/config.py::Settings`
   - Входные данные: переменные окружения стенда
   - Ожидаемый результат: значения разобраны, отсутствующий секрет даёт ошибку старта

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_skeleton.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] `create_app()` собирает приложение без обращения к базе на импорте
- [ ] Формат ошибок соответствует `docs/architectures/interfaces.md` §5.1
- [ ] OpenAPI публикуется и содержит версию API

## Примечания

Заглушки возвращают 501 с кодом `not_implemented`; каждая последующая STUB-задача заменяет 501 на
фиксированный ответ, LOGIC — на реальный.

Зависимости: 001.09. Приоритет: Critical. Оценка: 4 ч. Этап: 1 — схема данных и каркас Control
Plane.
