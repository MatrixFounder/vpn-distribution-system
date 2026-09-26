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
| `ca_key` | `api` (выпуск сертификатов нод при enrollment, §7.1) | приватный ключ внутреннего CA, PEM, P-256 (другой кривой `api` не стартует) | `openssl ecparam -genkey -name prime256v1 -noout -out ca_key` |
| `ca_cert` → `tls/ca.crt` | `api` (издатель листов нод и `ca_pem` ответа enrollment) | тот же сертификат CA, что у nginx: секрет `ca_cert` объявлен файлом `tls/ca.crt`, отдельной копии нет; при старте роли `api` (lifespan) проверяются: файл — ровно один блок PEM `CERTIFICATE`, пара с `ca_key`, `CA:TRUE`, самоподписанность (издатель — он сам байт в байт, подпись своим ключом, AKI — о нём самом), `keyCertSign` (если есть `keyUsage`), `clientAuth` и `serverAuth` (если есть `extendedKeyUsage`), расширения — только `basicConstraints`, `keyUsage`, `extendedKeyUsage` и некритические SKI и AKI (любое другое — отказ: как OpenSSL применит его к якорю, не проверить), и срок CA — негодный CA не даёт `api` стартовать | см. «Сертификат CA из ключа» ниже |
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
openssl req -x509 -new -key ca_key -days 3650 -subj "/CN=control-plane CA" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" -out tls/ca.crt
```

`pathlen:0` — промежуточных CA нет (как и `ssl_verify_depth 0` у nginx: в OpenSSL глубина считает
промежуточных CA); `keyCertSign` требует
RFC 5280 у сертификата, которым проверяют подписи листов (строгие клиенты без него цепочку не
примут). CA — самоподписанный корень: при глубине 0 OpenSSL nginx признаёт якорем только его
(без `PARTIAL_CHAIN`), и промежуточный CA в `ca_cert` не даёт `api` стартовать — иначе `api`
выпускал бы листы, которые nginx отвергает. `extendedKeyUsage` у CA необязателен; если он задан,
в нём должны быть `clientAuth` и `serverAuth` — OpenSSL сверяет назначение якоря с назначением
листа, `anyExtendedKeyUsage` не засчитывается. CA, истекающий раньше срока листа ноды (90 дней),
`api` принимает с предупреждением в журнале: каждый новый лист укорачивается до срока CA.

Ключ этого CA — онлайн, в `api`, а его сертификат — ещё и якорь, которым агент проверяет
серверный сертификат ниже: это принятый риск (security.md §7.3, решение 2026-09-25). Отдельный
офлайн-CA серверных сертификатов агентских портов — задача 001.66.

Серверный сертификат портов агентов, подписанный CA (замените SAN на реальные адреса):

```sh
openssl ecparam -genkey -name prime256v1 -noout -out tls/agent.key
openssl req -new -key tls/agent.key -subj "/CN=control-plane agents" \
  -addext "subjectAltName=DNS:cp.example.com,IP:203.0.113.10" -out agent.csr
openssl x509 -req -in agent.csr -CA tls/ca.crt -CAkey ca_key -CAcreateserial -days 825 \
  -copy_extensions copy -extfile <(printf '%s\n' "basicConstraints=critical,CA:FALSE" \
    "keyUsage=critical,digitalSignature" "extendedKeyUsage=serverAuth") \
  -out tls/agent.crt && rm agent.csr
```

`serverAuth` и `CA:FALSE` обязательны: без назначения лист годился бы и как клиентский сертификат
агентского порта, а агент проверяет серверный сертификат тем же CA, что и листы нод
(security.md §7.1) — различает их именно назначение и SAN.

## Разработка и стенд

Полный набор для разработки создаёт `deploy/scripts/dev-secrets.sh` (см. заголовок скрипта):
dev CA, серверные сертификаты для `localhost` и адресов из `DEV_TLS_SAN`, тестовый клиентский
сертификат ноды `dev/dev-node.crt` / `dev/dev-node.key` для проверки mTLS. Пароли базы в этом
режиме — `app`, как в `control-plane/tests/conftest.py`; PostgreSQL и Redis при этом публикуются
только на `127.0.0.1` хоста (`docker-compose.dev.yml`), с другой машины — туннелем ssh
(`deploy/scripts/stand-tunnel.sh`).
