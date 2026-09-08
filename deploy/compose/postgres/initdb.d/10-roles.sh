#!/bin/bash
# Создание ролей приложения при инициализации кластера PostgreSQL: образ выполняет скрипты
# /docker-entrypoint-initdb.d один раз, на пустом томе, от пользователя postgres.
# SQL — control-plane/migrations/bootstrap/roles.sql (монтируется в /opt/control-plane);
# пароли — /run/pg-secrets/*, подготовленные обёрткой entrypoint.sh из Docker secrets
# pg_app_rw_password, pg_app_migrate_password, pg_app_backup_password.
set -euo pipefail

secret() {
    local f="/run/pg-secrets/$1"
    if [ ! -r "$f" ]; then
        echo "10-roles.sh: нет секрета $f (обёртка entrypoint.sh не отработала?)" >&2
        return 1
    fi
    if [ ! -s "$f" ]; then
        echo "10-roles.sh: секрет $f пуст — роль без пароля не создаётся" >&2
        return 1
    fi
    cat "$f"
}

# Присваивания: ошибка чтения секрета останавливает скрипт (set -e), в отличие от $(…) в аргументах.
rw="$(secret pg_app_rw_password)"
mig="$(secret pg_app_migrate_password)"
bk="$(secret pg_app_backup_password)"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v db="$POSTGRES_DB" -v rw="$rw" -v mig="$mig" -v bk="$bk" \
    -f /opt/control-plane/roles.sql
echo "10-roles.sh: роли app_owner, app_rw, app_migrate, app_backup созданы"
