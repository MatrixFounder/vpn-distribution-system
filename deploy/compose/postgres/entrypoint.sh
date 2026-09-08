#!/bin/bash
# Обёртка штатной точки входа образа postgres. Скрипты /docker-entrypoint-initdb.d образ выполняет
# уже от пользователя postgres (uid 999), которому файлы Docker secrets с правами оператора
# недоступны. Контейнер стартует от root: секреты паролей ролей копируются в /run/pg-secrets
# владельцу postgres (0400), затем управление передаётся docker-entrypoint.sh образа.
set -euo pipefail

if [ "$(id -u)" = "0" ] && [ -d /run/secrets ]; then
    mkdir -p /run/pg-secrets
    for name in pg_app_rw_password pg_app_migrate_password pg_app_backup_password; do
        if [ -f "/run/secrets/$name" ]; then
            install -o postgres -g postgres -m 0400 "/run/secrets/$name" "/run/pg-secrets/$name"
        fi
    done
    chown postgres:postgres /run/pg-secrets
    chmod 0700 /run/pg-secrets
fi

exec docker-entrypoint.sh "$@"
