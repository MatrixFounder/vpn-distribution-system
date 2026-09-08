#!/usr/bin/env bash
# Секреты и сертификаты для окружения «Разработка» / стенда без реальных доменов
# (deploy/compose/secrets/README.md). Создаёт недостающие файлы, существующие не трогает
# (--force — пересоздать всё). Только для разработки: пароли базы фиксированы («app», как в
# control-plane/tests/conftest.py), CA самоподписанный.
#
# Использование (из корня репозитория):
#   deploy/scripts/dev-secrets.sh [--force] [--dir deploy/compose/secrets]
# Переменные:
#   DEV_TLS_SAN — дополнительные SAN серверных сертификатов через запятую,
#                 например DEV_TLS_SAN="IP:10.211.55.3,DNS:vm" (по умолчанию localhost и 127.0.0.1)
#   PG_PASSWORD     — пароль суперпользователя postgres вместо «app»
#   PG_APP_PASSWORD — пароль ролей app_rw, app_migrate и app_backup вместо «app»
# Результат: секреты в каталоге, серверные сертификаты в tls/ (монтируются в nginx),
# клиентский сертификат ноды для проверки mTLS — в dev/ (в nginx не попадает).
set -euo pipefail

force=0
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/compose/secrets"
while [ $# -gt 0 ]; do
    case "$1" in
        --force) force=1 ;;
        --dir) dir="$2"; shift ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "неизвестный аргумент: $1" >&2; exit 64 ;;
    esac
    shift
done

command -v openssl >/dev/null || { echo "нужен openssl" >&2; exit 1; }
umask 077
mkdir -p "$dir/tls" "$dir/dev"

san="DNS:localhost,IP:127.0.0.1${DEV_TLS_SAN:+,$DEV_TLS_SAN}"
created=()

# need <файл> — 0, если файл нужно создать (отсутствует или --force).
need() {
    if [ "$force" -eq 1 ] || [ ! -e "$dir/$1" ]; then created+=("$1"); return 0; fi
    return 1
}

if need pg_password; then printf '%s' "${PG_PASSWORD:-app}" > "$dir/pg_password"; fi
if need pg_app_rw_password; then printf '%s' "${PG_APP_PASSWORD:-app}" > "$dir/pg_app_rw_password"; fi
if need pg_app_migrate_password; then printf '%s' "${PG_APP_PASSWORD:-app}" > "$dir/pg_app_migrate_password"; fi
if need pg_app_backup_password; then printf '%s' "${PG_APP_PASSWORD:-app}" > "$dir/pg_app_backup_password"; fi
if need app_encryption_key; then openssl rand -base64 32 | tr -d '\n' > "$dir/app_encryption_key"; fi
if need smtp_password; then : > "$dir/smtp_password"; fi
if need pgbackrest_key; then openssl rand -hex 32 | tr -d '\n' > "$dir/pgbackrest_key"; fi

# Внутренний CA: ключ — Docker secret ca_key, сертификат — tls/ca.crt для nginx.
if need ca_key || [ ! -e "$dir/tls/ca.crt" ]; then
    openssl ecparam -genkey -name prime256v1 -noout -out "$dir/ca_key"
    openssl req -x509 -new -key "$dir/ca_key" -days 3650 -sha256 \
        -subj "/CN=control-plane dev CA" -out "$dir/tls/ca.crt" 2>/dev/null
    # Сертификаты, выпущенные прежним CA, недействительны — пересоздать.
    rm -f "$dir/tls/agent.crt" "$dir/tls/public.crt" "$dir/dev/dev-node.crt"
    created+=(tls/ca.crt)
fi

# issue <подкаталог/имя> <subject> <расширения> — ключ и сертификат <имя>.{key,crt}, подписанные CA.
issue() {
    local name="$1" subject="$2" ext="$3" cnf
    cnf="$(mktemp)"
    printf '[req]\ndistinguished_name=dn\n[dn]\n[ext]\n%b\n' "$ext" > "$cnf"
    [ -e "$dir/$name.key" ] && [ "$force" -eq 0 ] || \
        openssl ecparam -genkey -name prime256v1 -noout -out "$dir/$name.key"
    openssl req -new -key "$dir/$name.key" -subj "$subject" -config "$cnf" 2>/dev/null \
        | openssl x509 -req -CA "$dir/tls/ca.crt" -CAkey "$dir/ca_key" -CAcreateserial \
            -days 825 -sha256 -extfile "$cnf" -extensions ext -out "$dir/$name.crt" 2>/dev/null
    rm -f "$cnf"
}

server_ext="basicConstraints=CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=$san"
client_ext="basicConstraints=CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage=clientAuth"

if need tls/agent.crt; then issue tls/agent "/CN=control-plane agents (dev)" "$server_ext"; fi
if need tls/public.crt; then issue tls/public "/CN=control-plane public (dev)" "$server_ext"; fi
if need dev/dev-node.crt; then issue dev/dev-node "/CN=dev-node" "$client_ext"; fi

rm -f "$dir/tls/ca.srl"
chmod 644 "$dir"/tls/*.crt "$dir"/dev/*.crt
echo "каталог: $dir"
if [ "${#created[@]}" -eq 0 ]; then
    echo "все файлы уже существуют (пересоздать: --force)"
else
    printf 'создано: %s\n' "${created[@]}"
fi
