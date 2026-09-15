#!/usr/bin/env bash
# Копирование рабочего дерева репозитория на VM стенда (skills/vm-deploy/SKILL.md).
# Источник истины — этот репозиторий; на VM файлы не редактируются. Локальные артефакты
# (.venv, node_modules, .bin, фреймворк агентов, .env и секреты стенда) не передаются.
#
# Использование (из любого каталога): deploy/scripts/vm-sync.sh [--dry-run]
# Переменные: VM_HOST — алиас ssh (по умолчанию vm), VM_DIR — каталог на VM
# (по умолчанию vpn-distribution-system относительно домашнего каталога).
#
# Файлы, смонтированные в контейнер по одному (bind mount одного файла), rsync заменяет по
# rename: контейнер держит прежний inode, и `nginx -s reload` перечитывает старый текст
# (стенд, 2026-09-08 и 2026-09-14: серия проб измерена на прежней конфигурации). После
# переноса такого файла контейнер пересоздаётся, а не перечитывает конфигурацию; скрипт
# называет файл и службу и печатает команды пересоздания и сверки (WI-17).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
host="${VM_HOST:-vm}"
dir="${VM_DIR:-vpn-distribution-system}"
dry=()
[ "${1:-}" = "--dry-run" ] && dry=(--dry-run)

# путь в репозитории : служба Compose : путь внутри контейнера
BIND_FILES=(
    "deploy/nginx/nginx.conf:nginx:/etc/nginx/nginx.conf"
    "deploy/compose/postgres/entrypoint.sh:postgres:/opt/control-plane/entrypoint.sh"
    "deploy/compose/postgres/initdb.d/10-roles.sh:postgres:/docker-entrypoint-initdb.d/10-roles.sh"
    "control-plane/migrations/bootstrap/roles.sql:postgres:/opt/control-plane/roles.sql"
)

# Печатает подсказку по каждому перенесённому файлу под bind mount одного файла
# (строки --itemize-changes вида `>f.st...... путь`).
warn_bind_mounts() {
    local itemized="$1" entry path service inside found=0
    for entry in "${BIND_FILES[@]}"; do
        path="${entry%%:*}"; service="${entry#*:}"; inside="${service#*:}"; service="${service%%:*}"
        if grep -Eq "^[<>]f[^ ]* ${path}\$" "$itemized"; then
            found=1
            printf '\nvm-sync: %s изменён — это bind mount одного файла, контейнер %s держит прежний inode.\n' "$path" "$service"
            printf '  Пересоздайте контейнер (reload не читает новый файл):\n'
            printf '    ssh %s '"'"'cd %s && docker compose --env-file deploy/compose/.env -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.dev.yml up -d --force-recreate %s'"'"'\n' "$host" "$dir" "$service"
            printf '  Сверьте смонтированный файл с репозиторием:\n'
            printf '    ssh %s '"'"'docker exec control-plane-%s-1 cat %s | shasum -a 256'"'"' ; shasum -a 256 %s\n' "$host" "$service" "$inside" "$path"
        fi
    done
    [ "$found" = 1 ] && [ "${#dry[@]}" -gt 0 ] && printf '  (--dry-run: файл ещё не перенесён)\n'
    return 0
}

itemized="$(mktemp)"
trap 'rm -f "$itemized"' EXIT
rsync -az --delete --itemize-changes ${dry[@]+"${dry[@]}"} \
    --exclude .git --exclude .DS_Store --exclude .venv --exclude node_modules --exclude .bin \
    --exclude .agent --exclude .claude --exclude .agentic-development --exclude System \
    --exclude CLAUDE.agentic.md --exclude CLAUDE.local.md \
    --exclude '__pycache__' --exclude '.mypy_cache' --exclude '.ruff_cache' \
    --exclude '.pytest_cache' --exclude '*.egg-info' --exclude 'control-plane/build' \
    --exclude 'web/**/dist' --exclude 'deploy/compose/.env' --exclude 'deploy/compose/secrets/' \
    "$root/" "$host:$dir/" | tee "$itemized"
warn_bind_mounts "$itemized"
