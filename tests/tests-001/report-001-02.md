# Отчёт о проверке — задача 001.02 «Docker Compose для разработки и стенда»

Дата: 2026-09-08 (раунд 3 после ревью: C-1, L-1…L-6, S-1, S-2 закрыты, см. разделы «Раунд 2» и «Раунд 3»). Стенд: VM Ubuntu 24.04.4 aarch64 (`ssh vm`, `skills/vm-deploy/SKILL.md`),
Docker 28.5.2, Compose v5.4.0. Docker на рабочей машине не устанавливался (решение пользователя).
Порты VM в `.env`: HTTP 80, HTTPS 443, агентский 9443, enrollment 9444, PostgreSQL 15432,
Redis 16379 (5432 и 8443 на VM заняты другими проектами).

Команда стенда (`C`):

```sh
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.dev.yml
```

## Регрессия

- `C config` → код 0; в рендере: `postgres:18.6`, `redis:8.10`, `nginx:1.30`, `control-plane/app:dev`,
  `archive_mode=on`, `archive_timeout=900`, `--appendonly yes`, публикации 80/443/9443/9444/15432/16379.
- `make check` на рабочей машине (nvm use 24) → код 0 (код Python/Go/TS задачей не менялся).
- `shellcheck` для `deploy/scripts/dev-secrets.sh`, `deploy/scripts/vm-sync.sh`,
  `control-plane/docker-entrypoint.sh` → чисто. Оба compose-файла разбираются PyYAML.
- Валидатор скиллов фреймворка для `skills/vm-deploy` → 1/1 passed.

## Сборка и запуск

- `C build` → код 0 за 28 с; образ `control-plane/app:dev` 233 МБ (`python:3.14-slim`, колёса из
  `requirements.lock`, `--only-binary=:all:` — компилятора в образе нет; сборка на aarch64 прошла,
  значит колёса cp314/aarch64 для asyncpg, cryptography, pydantic-core, argon2, uvloop есть).
- AC-19: `C down -v` (тома удалены), затем `C up -d --build` → код 0; повторный `C up -d` без
  изменений → код 0 (пересозданий нет). Итого три запуска `up -d` подряд успешны.

```text
SERVICE             STATUS
api                 Up 2 minutes (unhealthy)
nginx               Up 2 minutes (healthy)
postgres            Up 2 minutes (healthy)
redis               Up 2 minutes (healthy)
scheduler           Exited (1)
worker-background   Exited (1)
worker-critical     Exited (1)
```

Роли приложения завершаются с кодом 1: `No module named app.jobs.worker`,
`No module named app.jobs.scheduler`, `Error loading ASGI app. Could not import module "app.main"`.
Модули создают задачи 001.10 и 001.11 — это ожидаемое состояние Stub-First, compose при их
появлении не меняется. Потребление стенда: nginx 5 МБ, api 33 МБ, postgres 33 МБ, redis 6 МБ.

## Сквозные тесты

1. **TC-E2E-01** — стенд поднимается одной командой: `up -d` код 0; `postgres`, `redis`, `nginx`
   → `healthy`. Часть «все контейнеры healthy; `GET /healthz` через nginx → 200» — **красная по
   плану**: `GET /healthz` → 502 (api не импортирует `app.main` до 001.10). nginx при этом стартует
   и отвечает сам (апстрим резолвится через `resolver 127.0.0.11`, а не при загрузке конфигурации).
2. **TC-E2E-02** — путь подписки не журналируется: после `GET /s/test` и контрольного `GET /healthz`
   ```text
   access.log (stdout контейнера) строк с /s/test: 0
   error.log  (stderr контейнера) строк с /s/test: 0
   access.log GET /healthz (контроль): 1
   error.log  /healthz (контроль: ошибка апстрима вне /s/ журналируется): 1
   ```
   Первый прогон показал строку `[error] … request: "GET /s/test …"` в error.log (ошибка апстрима
   502 пишет request-строку) — исправлено: `location ^~ /s/` с `error_log … crit`. Потоки
   проверялись через `docker logs` раздельно: `docker compose logs` их смешивает.

## Критерии приёмки

- [x] `docker compose up -d` завершается без ошибок дважды подряд на чистом хосте (AC-19) — три
      запуска, включая после `down -v`
- [x] PostgreSQL запущен с `archive_mode = on`:
  ```text
  show archive_mode      → on
  show archive_command   → mkdir -p …/wal_archive && test ! -f …/%f && cp %p …/%f && find …/wal_archive -type f -mtime +3 -delete
  show archive_timeout   → 15min
  version                → PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2) on aarch64
  pg_stat_archiver после pg_switch_wal(): archived_count=2, last=000000010000000000000002, failed_count=0
  ls /var/lib/postgresql/wal_archive → 000000010000000000000001, 000000010000000000000002 (16 МиБ)
  ```
- [x] Redis запущен с `appendonly yes` (`config get appendonly → yes`, `maxmemory-policy → noeviction`,
      `redis_version:8.10.1`)
- [x] Агентский `server` требует клиентский сертификат; enrollment-`server` — нет:
  ```text
  9443 без сертификата                 → 400 (No required SSL certificate was sent)
  9443 с dev/dev-node.crt              → 502 (TLS и проверка сертификата пройдены, апстрим отсутствует)
  9443 /agent/v1/enroll с сертификатом → 404 (enrollment только на 9444)
  9444 POST /agent/v1/enroll без серт. → 502 (проксирован)
  9444 GET /agent/v1/heartbeat         → 404
  80   GET /agent/v1/heartbeat         → 404 (Node API закрыт на публичном server)
  443  TLS 1.0                         → отказ рукопожатия (curl exit 35); TLS 1.2+ (ssl_protocols)
  nginx -t в контейнере                → configuration file /etc/nginx/nginx.conf test is successful
  ```

## Секреты

Первый прогон: чтение `/run/secrets/pg_password` из контейнера роли → `Permission denied` (Compose
без Swarm монтирует файлы с правами хоста, `-rw------- 1000`; `mode:` в длинном синтаксисе
не действует — проверено отдельным контейнером). Исправлено по образцу образов postgres/redis:
секреты монтируются в `/run/host-secrets`, точка входа стартует от root, копирует их в
`/run/secrets` владельцу `app` с правами 0400 и переключается на `app` через `setpriv`.

```text
$ C run --rm --no-deps -T api sh -c 'id -u; for f in /run/secrets/*; do [ -r "$f" ] && echo "$f $(stat -c %a $f) $(wc -c < $f) bytes"; done'
10001
/run/secrets/app_encryption_key 400 44 bytes
/run/secrets/ca_key 400 227 bytes
/run/secrets/pg_app_migrate_password 400 3 bytes
/run/secrets/pg_app_rw_password 400 3 bytes
```

Содержимое секретов в отчёт не выводится (раунд 1 печатал dev-пароль — убрано по L-6).
Файлы секретов на VM: `deploy/scripts/dev-secrets.sh` (dev CA, серверные сертификаты с SAN
`localhost, 127.0.0.1, IP VM, vm` в `tls/`, клиентский `dev/dev-node` для проверки mTLS — в nginx
не монтируется: `ls /etc/nginx/certs` в контейнере → `agent.crt agent.key ca.crt public.crt
public.key`); `openssl verify` по `tls/ca.crt` → OK для agent, public, dev-node. В репозиторий
не попадают (`git status` после создания пробных файлов в `secrets/` и `.env` их не показывает).

## Раунд 2 — правки по ревью и их проверка

- **C-1** (фильтр `/s/` обходился ненормализованными URL): `map $uri $loggable` вместо
  `$request_uri` плюс `access_log off` в `location ^~ /s/`. Посадка `--path-as-is` шести форм
  (`/s/…`, `//s/…`, `/%73/…`, `/x/../s/…`, `/./s/…`, `/s/…?x=1`) на пересозданном nginx:
  ```text
  access.log: строк с маркером всего 2, из них с /s/: 0   (две — контрольные /static и /healthz)
  error.log:  строк с /s/ в request-строке: 0; контроль /healthz в error.log: 1
  ```
- **L-2** (токен через Referer): `add_header Referrer-Policy "no-referrer" always` в `/s/`
  (проверено: ответ `/s/…` несёт заголовок) и `map $http_referer $log_referer` — referer с `/s/`
  пишется как `-`: посаженный `Referer: …/s/ref-…-TOKEN` на `/static/app.css` → в access.log
  `"-"`. Остаток: error.log при ошибке апстрима дописывает referrer запроса вне `/s/`; формат не
  настраивается, браузер после `no-referrer` такой Referer не отправляет — зафиксировано
  комментарием в `nginx.conf`.
- **L-1** (приложение под суперпользователем `app_owner`): суперпользователь образа —
  `postgres` (`pg_password`), приложение — `app_rw` (`pg_app_rw_password`), миграции `api` —
  `app_migrate` (`MIGRATE_DSN`, `pg_app_migrate_password`); роли создаёт 001.03. Стенд пересоздан
  с `down -v`: `select rolname, rolsuper from pg_roles where rolname like 'app_%' or rolname =
  'postgres'` → `postgres|t` (ролей `app_*` до 001.03 нет, коллизии с `CREATE ROLE app_owner` нет).
- **L-3** (`UVICORN_*` читает сам uvicorn): переменные переименованы в `APP_WORKERS`,
  `APP_RELOAD` (compose, `.env.example`, точка входа, `.env` на VM). Лог api после исправления:
  строк `"workers" flag is ignored` — 0 (до: 1).
- **L-4** (рост архива WAL): в `archive_command` добавлено удаление сегментов старше 3 суток,
  оценка роста — в комментарии compose; `pg_stat_archiver` после `pg_switch_wal()` → `2|0`.
- **L-5** (`vm-sync.sh` не описан): добавлен в `deploy/README.md` и `deploy/.AGENTS.md`.
- **L-6** (скилл предписывал `cat` секрета): таблица проверок в `skills/vm-deploy/SKILL.md`
  печатает размер и права, не содержимое; добавлены проверки ролей базы и обходов URL.
- Стиль: `location ^~ /agent` (без слэша → `GET /agent` = 404), клиентские сертификаты в
  `secrets/dev/`, `umask 077` до `mkdir` в `dev-secrets.sh`, `deploy/compose/.gitkeep` удалён,
  пустой referer в журнале — `-`.

## Раунд 3

- **S-1**: в `skills/vm-deploy/SKILL.md` команды `psql` переведены на `-U postgres`
  (`grep -c app_owner` → 0); команда чек-листа выполнена на стенде: `show archive_mode → on`,
  `pg_stat_archiver → 2|0`.
- **S-2**: контракт задачи перечисляет `pg_app_rw_password` и `pg_app_migrate_password`.
- Стиль: `location = /agent` + `location ^~ /agent/` (`/agent`, `/agent/`, `/agent/v1/…` → 404;
  `/agentx/v1/x` → проксируется, 502 до 001.10); `--help` скрипта печатает весь заголовок;
  каталоги секретов на VM приведены к 700. nginx пересоздан, inode хоста и контейнера совпадают,
  `nginx -t` успешен.
- Найдено при проверке: одиночный файл `nginx.conf` в bind-mount после rsync (замена через
  rename) остаётся старым inode в контейнере, `nginx -s reload` читает прежний конфиг
  (inode хоста 3804059, в контейнере 3803064). Правильно — `up -d --force-recreate nginx`;
  правило внесено в `skills/vm-deploy/SKILL.md`.

## Отклонения от описания задачи

- `nginx` 1.30 вместо 1.26 из плана — по утверждённому `docs/architectures/stack.md`.
- Секреты `pg_app_rw_password`, `pg_app_migrate_password` сверх перечня задачи — следствие ролей
  data-model §4.6 (ревью L-1); `pg_password` — пароль суперпользователя `postgres`.
- Добавлены `deploy/scripts/dev-secrets.sh` (без него AC-19 «одной командой» недостижим),
  `deploy/scripts/vm-sync.sh` и `skills/vm-deploy/SKILL.md` (стенд на VM по решению пользователя),
  `control-plane/docker-entrypoint.sh`, `control-plane/requirements.lock`, `control-plane/.dockerignore`
  (части образа из описания задачи), `deploy/.AGENTS.md`, `control-plane/.AGENTS.md`.
- `pgbackrest_key` в compose не объявлен (подключает 001.67); в README секретов перечислен.
