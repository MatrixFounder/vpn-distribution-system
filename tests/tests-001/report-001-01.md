# Отчёт о проверке — задача 001.01 «Каркас монорепозитория и инструменты проверки»

Дата: 2026-09-08 (раунд 3 после ревью). Команда: `make check`. Код завершения: 0.

Инструменты: Python 3.14.4 (control-plane/.venv), go1.27.1, golangci-lint 2.13.2 (проектный `.bin/`, ставится `make setup`), node v24.20.0, npm 11.19.0 (web/node_modules).

## TC-E2E-01 — пустой проект проходит проверки

```
control-plane/.venv/bin/ruff check control-plane
All checks passed!
control-plane/.venv/bin/ruff format --check control-plane
11 files already formatted
cd node-agent && go vet ./... && .bin/golangci-lint run ./...
cd web && npm run -s lint && npm run -s fmt:check
cd control-plane && .venv/bin/mypy
Success: no issues found in 11 source files
cd web && npm run -s typecheck
pytest: тестовых файлов нет — пропуск
cd node-agent && go build ./... && go test ./...
cd web && npm run -s test
No test files found, exiting with code 0
```

Результат: ruff, ruff format, mypy, pytest (файлов нет → пропуск по факту), gofmt, go vet, golangci-lint, go build, go test, eslint, prettier, tsc -b, vitest — успешно; итоговый код `make check` = 0.

## Проверка гейта тестов (замечание C-1 ревью)

Временный `tests/test_tmp_fail.py` с `assert False` → `make test-py` завершился кодом 2 (ошибка); файл удалён.

Замечание L-11 (раунд 2): маска `*_test.py`. После фикса (`python_files = ["test_*.py"]` в `pyproject.toml`) временный `tests/tmp_fail_test.py` с `assert False` не собирается ни pytest (`no tests ran`, код 5 при прямом запуске без сторожа), ни сторожем — расхождения между конфигурацией pytest и `make test-py` нет; файл удалён. Повторный зонд `test_tmp.py` → код 2.

## Критерии приёмки

- [x] Структура каталогов совпадает с `docs/ARCHITECTURE.md` §1.1; сверх §1.1 — `node-agent/internal/app/` (нужен для компиляции `main.go`, сигнатура по контракту 001.53)
- [x] `make check` завершается с кодом 0
- [x] CI-файл содержит стадии lint, unit, contract, build (YAML валиден; `contract` — fail-fast при появлении каталога контрактов)
- [x] `control-plane/app/.AGENTS.md` создан
