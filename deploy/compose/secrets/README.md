# deploy/compose/secrets/

Файлы Docker secrets и сертификатов стенда (§10.3 архитектуры). В репозиторий попадает только
этот README (`.gitignore`); всё остальное создаётся на хосте с правами `600` и хранится в
резервной копии секретов конфигурации (§9.2).

## Секреты приложения (Docker secrets, `/run/secrets/<имя>` в контейнерах)

| Файл | Кто читает | Содержимое | Как создать |
| :--- | :--- | :--- | :--- |
| `pg_password` | `postgres` (пароль суперпользователя `postgres` при инициализации); задача 001.03 — создание ролей | одна строка без перевода строки | `openssl rand -base64 24 \| tr -d '\n' > pg_password` |
| `pg_app_rw_password` | все роли приложения (`PG_DSN` под `app_rw`, data-model §4.6) | одна строка | `openssl rand -base64 24 \| tr -d '\n' > pg_app_rw_password` |
| `pg_app_migrate_password` | `api` — миграции при старте под `app_migrate` (001.03) | одна строка | `openssl rand -base64 24 \| tr -d '\n' > pg_app_migrate_password` |
| `pg_app_backup_password` | резервное копирование под `app_backup` (только чтение, 001.67) | одна строка | `openssl rand -base64 24 \| tr -d '\n' > pg_app_backup_password` |
| `app_encryption_key` | все роли приложения | ключ AES-256-GCM (§7.2), 32 байта в base64 | `openssl rand -base64 32 \| tr -d '\n' > app_encryption_key` |
| `ca_key` | `api` (выпуск сертификатов нод при enrollment, §7.1) | приватный ключ внутреннего CA, PEM | `openssl ecparam -genkey -name prime256v1 -noout -out ca_key` |
| `smtp_password` | `worker-background` | пароль SMTP-реле; пустой файл — почта отключена | `printf '%s' "$SMTP_PASSWORD" > smtp_password` |
| `pgbackrest_key` | резервное копирование (задача 001.67) | ключ шифрования репозитория pgbackrest (Н-12) | `openssl rand -hex 32 \| tr -d '\n' > pgbackrest_key` |

`pgbackrest_key` в `docker-compose.yml` пока не объявлен: его подключает задача 001.67.
Роли `app_owner`, `app_rw`, `app_migrate`, `app_backup`, `app_audit_purge` (без входа) создаёт `deploy/compose/postgres/initdb.d/10-roles.sh`
при инициализации кластера (SQL — `control-plane/migrations/bootstrap/roles.sql`) с паролями из
`pg_app_*_password` (обёртка `entrypoint.sh` при каждом старте копирует их в `/run/pg-secrets`
пользователю `postgres`, скрипт initdb.d читает их только при инициализации). Приложение
суперпользователя не использует. На уже инициализированном томе скрипт не выполняется: роли
создаются вручную тем же SQL (`docker exec -i … psql -U postgres -d control_plane -v rw=… -v mig=… -v bk=… -v db=control_plane -f - < control-plane/migrations/bootstrap/roles.sql`).

## Сертификаты nginx (`tls/`, монтируется в `/etc/nginx/certs`)

Только серверные файлы и CA; клиентские сертификаты (в том числе тестовый `dev/dev-node.*`) в этот
каталог не кладутся — он целиком виден nginx.

| Файл | Назначение |
| :--- | :--- |
| `tls/ca.crt` | сертификат внутреннего CA (пара к `ca_key`); nginx проверяет им клиентские сертификаты нод |
| `tls/agent.crt`, `tls/agent.key` | серверный сертификат портов агентов (8443) и enrollment (8444); SAN — адрес, по которому ноды видят Control Plane |
| `tls/public.crt`, `tls/public.key` | серверный сертификат публичных доменов; в промышленном контуре заменяется ACME (001.66) |

Сертификат CA из ключа:

```sh
openssl req -x509 -new -key ca_key -days 3650 -subj "/CN=control-plane CA" -out tls/ca.crt
```

Серверный сертификат портов агентов, подписанный CA (замените SAN на реальные адреса):

```sh
openssl ecparam -genkey -name prime256v1 -noout -out tls/agent.key
openssl req -new -key tls/agent.key -subj "/CN=control-plane agents" \
  -addext "subjectAltName=DNS:cp.example.com,IP:203.0.113.10" -out agent.csr
openssl x509 -req -in agent.csr -CA tls/ca.crt -CAkey ca_key -CAcreateserial -days 825 \
  -copy_extensions copy -out tls/agent.crt && rm agent.csr
```

## Разработка и стенд

Полный набор для разработки создаёт `deploy/scripts/dev-secrets.sh` (см. заголовок скрипта):
dev CA, серверные сертификаты для `localhost` и адресов из `DEV_TLS_SAN`, тестовый клиентский
сертификат ноды `dev/dev-node.crt` / `dev/dev-node.key` для проверки mTLS. Пароли базы в этом
режиме — `app`, как в `control-plane/tests/conftest.py`; публикация портов при этом ограничена
`DEV_BIND_ADDR` (`.env.example`).
