#!/bin/sh
# Точка входа образа Control Plane: одна кодовая база, роль процесса задаёт APP_ROLE
# (docs/architectures/system-architecture.md §3.2, C-01…C-03). Аргументы командной строки,
# если заданы, выполняются вместо роли — для `docker compose run api python -m app.cli …`.
# Роль api перед стартом применяет миграции (`python -m app.cli migrate`, §10.2): под ролью
# app_migrate с блокировкой yoyo, поэтому несколько экземпляров api не мешают друг другу.
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
    # setpriv сохраняет окружение root, включая HOME=/root: asyncpg ищет клиентский сертификат
    # в ~/.postgresql/ и на закрытом /root падает PermissionError (найдено в 001.11 на ролях
    # worker/scheduler). HOME — домашний каталог app из passwd; USER — для журналов.
    export HOME=/app USER=app
    exec setpriv --reuid=app --regid=app --init-groups --inh-caps=-all --no-new-privs "$0" "$@"
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

case "${APP_ROLE:-}" in
    api)
        python -m app.cli migrate
        # Переменные названы APP_*: имена UVICORN_* uvicorn читает сам (auto_envvar_prefix)
        # и они перебивали бы явные флаги ниже.
        # За nginx: схема и адрес клиента — из X-Forwarded-Proto / X-Forwarded-For (§5.1, Н-25,
        # лимиты частоты). Контракт с deploy/nginx/nginx.conf: nginx ПЕРЕЗАПИСЫВАЕТ оба заголовка
        # ($remote_addr, $scheme), поэтому в них ровно одно значение и оно не от клиента;
        # --forwarded-allow-ips '*' лишь принимает заголовки от любого узла сети Compose (порт 8000
        # не публикуется). При цепочке в X-Forwarded-For uvicorn с '*' взял бы крайний левый,
        # клиентский элемент — поэтому дополнение ($proxy_add_x_forwarded_for) в nginx запрещено;
        # страж — tests/unit/test_proxy_contract.py.
        case "${APP_RELOAD:-}" in
            1|true|yes)
                # Разработка: перезапуск при изменении смонтированных исходников (один процесс).
                exec python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 \
                    --proxy-headers --forwarded-allow-ips '*' --reload
                ;;
        esac
        exec python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 \
            --proxy-headers --forwarded-allow-ips '*' --workers "${APP_WORKERS:-2}"
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
