#!/usr/bin/env bash
# Копирование рабочего дерева репозитория на VM стенда (skills/vm-deploy/SKILL.md).
# Источник истины — этот репозиторий; на VM файлы не редактируются. Локальные артефакты
# (.venv, node_modules, .bin, фреймворк агентов, .env и секреты стенда) не передаются.
#
# Использование (из любого каталога): deploy/scripts/vm-sync.sh [--dry-run]
# Переменные: VM_HOST — алиас ssh (по умолчанию vm), VM_DIR — каталог на VM
# (по умолчанию vpn-distribution-system относительно домашнего каталога).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
host="${VM_HOST:-vm}"
dir="${VM_DIR:-vpn-distribution-system}"
dry=()
[ "${1:-}" = "--dry-run" ] && dry=(--dry-run --itemize-changes)

exec rsync -az --delete ${dry[@]+"${dry[@]}"} \
    --exclude .git --exclude .DS_Store --exclude .venv --exclude node_modules --exclude .bin \
    --exclude .agent --exclude .claude --exclude .agentic-development --exclude System \
    --exclude CLAUDE.agentic.md --exclude CLAUDE.local.md \
    --exclude '__pycache__' --exclude '.mypy_cache' --exclude '.ruff_cache' \
    --exclude '.pytest_cache' --exclude '*.egg-info' --exclude 'control-plane/build' \
    --exclude 'web/**/dist' --exclude 'deploy/compose/.env' --exclude 'deploy/compose/secrets/' \
    "$root/" "$host:$dir/"
