#!/usr/bin/env bash
# Туннель ssh к PostgreSQL и Redis стенда (skills/vm-deploy/SKILL.md). docker-compose.dev.yml
# публикует их только на 127.0.0.1 VM (роаст 001.25, раунд 6: с 0.0.0.0 Redis без пароля и база
# были открыты машине разработчика и контейнерам соседних проектов VM), и тесты с рабочей машины
# ходят к ним через этот туннель — на те же номера портов своего 127.0.0.1. Петлевой адрес не
# закрывает их от процессов самой VM и контейнеров с сетью хоста (Redis стенда без пароля —
# остаточный риск стенда разработки, раунд 8).
#
# Использование (из любого каталога): deploy/scripts/stand-tunnel.sh start|status|stop
# Переменные: VM_HOST — алиас ssh (по умолчанию vm); PG_HOST_PORT и REDIS_HOST_PORT — порты из
# .env VM (по умолчанию стенда — 15432 и 16379, skills/vm-deploy §3).
#
# После start:
#   PG_DSN=postgresql://app_rw:app@127.0.0.1:15432/control_plane
#   MIGRATE_DSN=postgresql://app_migrate:app@127.0.0.1:15432/control_plane
#   REDIS_URL=redis://127.0.0.1:16379/0
# nginx стенда (443, 9443, 9444) по-прежнему на адресе VM — скрипты tests/stand/ берут его из
# STAND_HOST или `ssh -G vm`.
set -euo pipefail

host="${VM_HOST:-vm}"
pg_port="${PG_HOST_PORT:-15432}"
redis_port="${REDIS_HOST_PORT:-16379}"
# Управляющий сокет ssh: по нему status и stop находят процесс туннеля без поиска по списку
# процессов.
socket="${TMPDIR:-/tmp}/stand-tunnel-${host}.sock"

case "${1:-}" in
    start)
        if ssh -S "$socket" -O check "$host" 2>/dev/null; then
            echo "туннель к $host уже поднят"
            exit 0
        fi
        # Сокет мёртвого туннеля (процесс убит, файл остался) — удалить: с ним ssh -M поднял бы
        # проброс без управляющего сокета, и status/stop его не нашли бы (роаст 001.25, раунд 7).
        rm -f "$socket"
        # Проверка живости соединения: оборванный туннель выходит, и следующий start поднимает
        # новый, а не держит мёртвые порты (длинные прогоны тестов идут через него часами).
        ssh -f -N -M -S "$socket" -o ExitOnForwardFailure=yes \
            -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
            -L "127.0.0.1:${pg_port}:127.0.0.1:${pg_port}" \
            -L "127.0.0.1:${redis_port}:127.0.0.1:${redis_port}" \
            "$host"
        echo "туннель поднят: 127.0.0.1:${pg_port} и 127.0.0.1:${redis_port} → ${host}"
        ;;
    status)
        ssh -S "$socket" -O check "$host"
        ;;
    stop)
        ssh -S "$socket" -O exit "$host"
        ;;
    *)
        echo "использование: $0 start|status|stop" >&2
        exit 64
        ;;
esac
