# Задача 001.01: Каркас монорепозитория и инструменты проверки

Тип задачи: `[CONFIGURATION]`.

## Связь со сценариями
- UC-01 Ввод ноды в эксплуатацию (косвенно: сборка агента)
- UC-16 Работа в личном кабинете (косвенно: сборка web)

Требования RTM: R-01, R-42, R-50.

<!-- contract:goal -->

## Цель задачи

Создать структуру каталогов по `docs/ARCHITECTURE.md` §1.1 и настроить проверки так, чтобы команда
`make check` проходила на пустом проекте.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `Makefile` — цели `check`, `test`, `lint`, `fmt` для трёх подпроектов
- `control-plane/pyproject.toml` — Python 3.14, зависимости: fastapi, uvicorn, asyncpg, pydantic, jinja2, yoyo-migrations, cryptography, argon2-cffi, pyotp; ruff, mypy, pytest, pytest-asyncio
- `control-plane/app/__init__.py` — пакет приложения
- `control-plane/tests/conftest.py` — фикстуры: `pg_dsn`, `redis_url`, `app_client` (httpx.AsyncClient)
- `node-agent/go.mod` — модуль `node-agent`, Go 1.27
- `node-agent/cmd/node-agent/main.go` — точка входа: разбор флагов, запуск `internal/app.Run`
- `web/package.json` — workspaces: `apps/cabinet`, `apps/admin`, `packages/api-client`, `packages/i18n`; eslint, tsc, vitest
- `deploy/README.md` — назначение каталогов `compose/`, `nginx/`, `node/`, `prometheus/`
- `.github/workflows/ci.yml` — стадии по `docs/architectures/deployment.md` §10.2: lint, unit, contract, build
- `control-plane/app/.AGENTS.md` — описание пакета `app` и его подпакетов

### Интеграция компонентов

Каталоги соответствуют §1.1 архитектуры. CI запускает `make check`. Дальнейшие задачи добавляют
модули внутрь созданных пакетов.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Пустой проект проходит проверки
   - Входные данные: чистый клон, `make check`
   - Ожидаемый результат: ruff, mypy, go vet, eslint и tsc завершаются с кодом 0
   - Примечание: тестов пока нет; pytest выходит с кодом 5 «no tests» — принимается

### Модульные тесты

- Не требуются: задача не содержит логики сверх заглушек или конфигурации.

### Регрессионные тесты

- Команда: `make check`
- Полный набор: `make check` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Структура каталогов совпадает с `docs/ARCHITECTURE.md` §1.1
- [ ] `make check` завершается с кодом 0
- [ ] CI-файл содержит стадии lint, unit, contract, build
- [ ] `control-plane/app/.AGENTS.md` создан

## Примечания

Задача конфигурационная: без пары stub/logic (`planning-decision-tree` §1). Версии закреплены по
О-8: Xray-core v26.3.27, sing-box v1.14.0.

Зависимости: нет. Приоритет: Critical. Оценка: 3 ч. Этап: 0 — репозиторий и стенд.
