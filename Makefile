# Проверки трёх подпроектов: control-plane (Python), node-agent (Go), web (TypeScript).
# `make check` — то же, что запускает CI (.github/workflows/ci.yml). Требуются: Python 3.14,
# Go 1.27, Node 24 (см. .nvmrc). Перед первым запуском — `make setup`.

SHELL := /bin/bash
.DEFAULT_GOAL := check

PY   := control-plane/.venv/bin/python
RUFF := control-plane/.venv/bin/ruff
MYPY := control-plane/.venv/bin/mypy
PYTEST := control-plane/.venv/bin/pytest
# Проектные инструменты живут в дереве проекта (.bin/, .venv/, node_modules/), а не на машине:
# глобальная установка меняет окружение всех остальных проектов на этой машине.
BIN := $(CURDIR)/.bin
GOLANGCI_VERSION := 2.13.2
GOLANGCI := $(BIN)/golangci-lint

.PHONY: check lint typecheck test fmt setup test-contract \
        lint-py lint-go lint-web typecheck-py typecheck-web test-py test-go test-web fmt-py fmt-go fmt-web tools

check: tools lint typecheck test

lint: lint-py lint-go lint-web
typecheck: typecheck-py typecheck-web
test: test-py test-go test-web
fmt: fmt-py fmt-go fmt-web

# --- инструменты: отсутствие или неверная версия любого — ошибка, а не тихий пропуск
tools:
	@test -x $(PY) || { echo "нет control-plane/.venv — выполните: make setup"; exit 1; }
	@$(PY) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 14) else 1)' || { echo "нужен Python >= 3.14 в control-plane/.venv"; exit 1; }
	@command -v go >/dev/null || { echo "нет go (требуется 1.27)"; exit 1; }
	@test -x $(GOLANGCI) || { echo "нет .bin/golangci-lint — выполните: make setup"; exit 1; }
	@$(GOLANGCI) version 2>/dev/null | grep -q "version $(GOLANGCI_VERSION)" || { echo "нужен golangci-lint $(GOLANGCI_VERSION) в .bin/, найден: $$($(GOLANGCI) version 2>/dev/null)"; exit 1; }
	@command -v node >/dev/null || { echo "нет node (требуется 24, см. .nvmrc)"; exit 1; }
	@node -e 'process.exit(+process.versions.node.split(".")[0] === 24 ? 0 : 1)' || { echo "нужен node 24 (.nvmrc), найден $$(node --version)"; exit 1; }
	@test -d web/node_modules || { echo "нет web/node_modules — выполните: make setup"; exit 1; }

setup:
	python3 -m venv control-plane/.venv
	$(PY) -m pip install -q -r control-plane/requirements-dev.lock
	$(PY) -m pip install -q --no-deps -e control-plane
	$(PY) -m pip check
	cd node-agent && go mod download
	$(GOLANGCI) version 2>/dev/null | grep -q "version $(GOLANGCI_VERSION)" || GOBIN=$(BIN) go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v$(GOLANGCI_VERSION)
	cd web && npm ci --no-audit --no-fund

# --- control-plane
lint-py:
	$(RUFF) check control-plane
	$(RUFF) format --check control-plane
typecheck-py:
	cd control-plane && .venv/bin/mypy
# pytest: пока тестовых файлов нет — честный пропуск по факту их отсутствия;
# с первым test_*.py код pytest пробрасывается как есть (в том числе 5).
# Маска совпадает с python_files в control-plane/pyproject.toml.
test-py:
	@cd control-plane && \
	if [ -z "$$(find tests -name 'test_*.py' -print -quit)" ]; then \
	  echo "pytest: тестовых файлов нет — пропуск"; \
	else .venv/bin/pytest -q; fi
fmt-py:
	$(RUFF) format control-plane
	$(RUFF) check --fix control-plane

# --- node-agent
lint-go:
	@cd node-agent && test -z "$$(gofmt -l .)" || { echo "gofmt: не отформатированы:"; gofmt -l .; exit 1; }
	cd node-agent && go vet ./... && $(GOLANGCI) run ./...
test-go:
	cd node-agent && go build ./... && go test ./...
fmt-go:
	cd node-agent && gofmt -w .

# --- контрактные тесты /agent/v1 и эталоны подписки: наполняются задачами 001.28, 001.43
test-contract:
	@echo "test-contract: не реализовано — контракты появляются в задаче 001.28"; exit 1

# --- web
lint-web:
	cd web && npm run -s lint && npm run -s fmt:check
typecheck-web:
	cd web && npm run -s typecheck
test-web:
	cd web && npm run -s test
fmt-web:
	cd web && npm run -s fmt
