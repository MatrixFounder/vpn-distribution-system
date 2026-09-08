#!/bin/sh
# Точка входа образа Control Plane: одна кодовая база, роль процесса задаёт APP_ROLE
# (docs/architectures/system-architecture.md §3.2, C-01…C-03). Аргументы командной строки,
# если заданы, выполняются вместо роли — для `docker compose run api python -m app.cli …`.
# Задача 001.03 добавляет перед стартом роли api шаг `python -m app.cli migrate` (§10.2).
set -eu

# Секреты Compose — файлы хоста с правами хоста (обычно 600 владельца-оператора), а роль
# работает от пользователя app (uid 10001). Контейнер стартует от root: копирует секреты из
# /run/host-secrets в /run/secrets с владельцем app и правами 0400, затем сбрасывает привилегии
# и перезапускает себя от app — по образцу официальных образов postgres/redis.
if [ "$(id -u)" = "0" ]; then
    if [ -d /run/host-secrets ]; then
        mkdir -p /run/secrets
        for src in /run/host-secrets/*; do
            [ -f "$src" ] || continue
            dst="/run/secrets/$(basename "$src")"
            cp "$src" "$dst" && chown app:app "$dst" && chmod 0400 "$dst"
        done
        chown app:app /run/secrets && chmod 0700 /run/secrets
    fi
    exec setpriv --reuid=app --regid=app --init-groups --inh-caps=-all --no-new-privs "$0" "$@"
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

case "${APP_ROLE:-}" in
    api)
        # Переменные названы APP_*: имена UVICORN_* uvicorn читает сам (auto_envvar_prefix)
        # и они перебивали бы явные флаги ниже.
        case "${APP_RELOAD:-}" in
            1|true|yes)
                # Разработка: перезапуск при изменении смонтированных исходников (один процесс).
                exec python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --reload
                ;;
        esac
        exec python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 \
            --workers "${APP_WORKERS:-2}"
        ;;
    worker-critical)
        exec python -m app.jobs.worker --queue critical
        ;;
    worker-background)
        exec python -m app.jobs.worker --queue background
        ;;
    scheduler)
        exec python -m app.jobs.scheduler
        ;;
    *)
        echo "APP_ROLE должен быть одним из: api, worker-critical, worker-background, scheduler" \
             "(получено: '${APP_ROLE:-}')" >&2
        exit 64
        ;;
esac
