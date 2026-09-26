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
        # Конфигурация uvicorn — только строка запуска ниже. Опции, которых в ней нет, uvicorn
        # берёт из окружения UVICORN_* (auto_envvar_prefix; флаг командной строки перебивает
        # окружение, окружение — умолчание), а .env оператора роли приложения получают целиком:
        # UVICORN_LOG_LEVEL=trace включил бы журнал сообщений ASGI с путём /s/<токен> (роаст
        # 001.25, раунд 8). Поэтому все UVICORN_* снимаются здесь, а переменные роли названы APP_*.
        for name in $(env | sed -n 's/^\(UVICORN_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$name"; done
        # За nginx: схема и адрес клиента — из X-Forwarded-Proto / X-Forwarded-For (§5.1, Н-25,
        # лимиты частоты). Контракт с deploy/nginx/nginx.conf: nginx ПЕРЕЗАПИСЫВАЕТ оба заголовка
        # ($remote_addr, $scheme), поэтому в них ровно одно значение и оно не от клиента;
        # --forwarded-allow-ips '*' лишь принимает заголовки от любого узла сети Compose (порт 8000
        # не публикуется). При цепочке в X-Forwarded-For uvicorn с '*' взял бы крайний левый,
        # клиентский элемент — поэтому дополнение ($proxy_add_x_forwarded_for) в nginx запрещено;
        # страж — tests/unit/test_proxy_contract.py.
        # --no-access-log: журнал запросов ведёт nginx (путь /s/ там исключён, Н-25); access-log
        # uvicorn писал строку запроса целиком — токен подписки в журнале контейнера api (стенд,
        # роаст 001.25, раунд 6). --ws none: маршрутов WebSocket в API нет, а строки рукопожатия
        # uvicorn пишет с путём мимо --no-access-log (раунд 7). --log-level info: уровень trace
        # включает журнал сообщений ASGI (Started scope=… с путём); после --log-config uvicorn
        # выставляет свои журналы по этому уровню (раунд 8). --lifespan on: CA узлов загружается
        # и проверяется при старте, в lifespan приложения (негодный CA — отказ старта); с off
        # api стартовал бы здоровым и отказывал бы обмену в бою (роаст 001.25, раунд 9).
        case "${APP_RELOAD:-}" in
            1|true|yes)
                # Разработка: перезапуск при изменении смонтированных исходников (один процесс).
                exec python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 \
                    --proxy-headers --forwarded-allow-ips '*' --no-access-log --ws none \
                    --log-level info --lifespan on --reload
                ;;
        esac
        exec python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 \
            --proxy-headers --forwarded-allow-ips '*' --no-access-log --ws none \
            --log-level info --lifespan on --workers "${APP_WORKERS:-2}"
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
