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
