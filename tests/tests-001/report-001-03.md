# Отчёт о проверке — задача 001.03 «Инструмент миграций, расширения, роли и перечисления базы»

Дата: 2026-09-08 (раунд 3 после ревью: C-1, L-1…L-6 и фиктивный страж L-1 закрыты, см. разделы «Раунд 2», «Раунд 3»). Стенд: VM Ubuntu (`ssh vm`, `skills/vm-deploy/SKILL.md`), Compose
`control-plane`, PostgreSQL 18.6. Тесты выполнялись с рабочей машины против базы стенда
(`PG_DSN`/`MIGRATE_DSN` → VM:15432) и через `docker exec` на VM.

## Решение по ролям (уточнение контракта)

Роли базы — кластерные объекты. Миграция под `app_migrate` не может создать `app_migrate`,
поэтому роли создаёт `control-plane/migrations/bootstrap/roles.sql` при инициализации кластера
(`deploy/compose/postgres/initdb.d/10-roles.sh`, под суперпользователем `postgres`, пароли из
Docker secrets), а в CI — тот же SQL через `docker exec … psql`. Миграция 0001 создаёт объекты
базы: расширения, привилегии по умолчанию для `app_rw`/`app_backup`, 22 перечисления §4.2.
Каждая миграция начинается с `SET LOCAL ROLE app_owner` — владелец объектов `app_owner`, учёт
yoyo (`_yoyo_migration`) ведёт `app_migrate`. Откат удаляет перечисления, привилегии и
расширения; роли остаются. Зафиксировано в `docs/tasks/task-001-03-migrations-tooling.md`.

## Регрессия

- `make check` (nvm use 24; `PG_DSN`, `MIGRATE_DSN`, `REDIS_URL` → стенд) → код 0: ruff, ruff
  format, mypy strict (14 файлов, 0 замечаний; для `yoyo` и `asyncpg` без `py.typed` —
  `ignore_missing_imports`), pytest 4 passed, go, web.
- Без базы (`PG_DSN` на закрытый порт) `make test-py` → код 2 с `psycopg.OperationalError:
  connection refused` — падение, не пропуск.
- Регрессионная команда задачи через CLI yoyo и `yoyo.ini`
  (`MIGRATE_DSN=postgresql+psycopg://app_migrate:app@VM:15432/control_plane`):
  ```text
  yoyo apply --batch          → rc=0
  yoyo rollback --batch --all → rc=0
  yoyo apply --batch          → rc=0
  yoyo list                   → A  0001_extensions_roles_enums
  ```
- `shellcheck`: `deploy/compose/postgres/entrypoint.sh`, `initdb.d/10-roles.sh`,
  `docker-entrypoint.sh`, `dev-secrets.sh` → чисто. `pip check` → чисто; `requirements.lock` ⊂
  `requirements-dev.lock` с теми же версиями (добавлены `psycopg==3.3.5`, `psycopg-binary==3.3.5`;
  колёса cp314 для macOS arm64 и manylinux aarch64 проверены, образ собран на aarch64).

## Стенд: инициализация кластера и старт api

`down -v` (тома удалены; данных на стенде нет), `up -d --build` → код 0.

```text
postgres log: running /docker-entrypoint-initdb.d/10-roles.sh
              10-roles.sh: роли app_owner, app_rw, app_migrate, app_backup созданы
pg_authid:    app_backup|super=f|login=t|has_pw=t
              app_migrate|f|t|t
              app_owner|f|f|f
              app_rw|f|t|t
pg_database:  control_plane → владелец app_owner
api log:      migrate: применено — 1, всего в источнике — 1   (затем ожидаемая ошибка импорта app.main)
pg_extension: btree_gist owner=app_owner, citext owner=app_owner
pg_type:      22 enums, owners: app_owner
_yoyo_migration: 0001_extensions_roles_enums; таблица принадлежит app_migrate
/run/pg-secrets в контейнере postgres: три файла -r-------- 999 999
```

Первый прогон выявил: скрипты initdb.d выполняются от пользователя `postgres` (uid 999), и
секреты с правами хоста ему недоступны; ошибка чтения внутри `$(…)` в аргументах psql не
останавливала скрипт, роли получали пустые пароли, api не проходил аутентификацию. Исправлено:
обёртка `deploy/compose/postgres/entrypoint.sh` (от root) копирует секреты в `/run/pg-secrets`
владельцу `postgres`, скрипт читает их присваиваниями (`set -e` прерывает при ошибке).

В журнале postgres при каждом запуске yoyo одна строка `ERROR: table "yoyo_tmp_…" does not
exist` — штатная проба yoyo для определения схемы, не ошибка миграции.

## Сквозные тесты (`control-plane/tests/e2e/test_migrations.py`, 4 passed)

1. **TC-E2E-01** `test_migration_0001_apply_rollback_apply`: `app.cli migrate` → код 0;
   расширения `{btree_gist, citext}`, 22 перечисления, владелец всех типов `app_owner`, четыре роли
   без суперпользователя, вход только у `app_rw`, `app_migrate`, `app_backup`; `app_rw` не владеет
   ни одним объектом (`pg_class` + `pg_type`). `migrate --rollback-all` → код 0; расширений и
   перечислений нет, роли на месте. `migrate` → код 0, «применено — 1», объекты снова на месте.
2. `test_migrate_is_idempotent`: повторный `migrate` → код 0, «применено — 0» (старт api, §10.2).
3. `test_admin_create_is_a_stub`: `admin create` → код 69, сообщение про 001.47.
4. `test_every_migration_sets_owner_role`: статический страж — каждая миграция и откат начинаются
   с `SET LOCAL ROLE app_owner;` (посадка файла без него → тест красный, файл удалён).

## Раунд 2 — правки по ревью и их проверка

- **C-1** (пустой секрет → login-роль без пароля): `10-roles.sh` проверяет `[ -s ]`, `roles.sql`
  проверяет `length(:'rw'|'mig'|'bk') > 0` через `\gset`/`\if` и падает `RAISE EXCEPTION`.
  Посадки: пустой файл секрета → `10-roles.sh: секрет … пуст — роль без пароля не создаётся`,
  rc=1, пароль `app_backup` на месте; `-v bk=` пустой → `ERROR: roles.sql: пустой пароль
  app_backup`, rc=3, пароль на месте. Повторный запуск `roles.sql` → rc=0, 0 строк NOTICE
  (`GRANT` членства под условием).
- **L-1** (`app_backup` мог выполнять функции через PUBLIC): `ALTER DEFAULT PRIVILEGES FOR ROLE
  app_owner REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` — глобально, не `IN SCHEMA`: схемные
  умолчания складываются со встроенными и отозвать право PUBLIC не могут (первая попытка со
  схемой оставила `=X` — проверено пробной функцией). Итог: `pg_default_acl` `f|-|{app_owner=X}`,
  `f|public|{app_rw=X}`; пробная функция `app_owner` → ACL `{app_owner=X,app_rw=X}`,
  `app_backup` → `permission denied for function`, `app_rw` → результат. Откат возвращает
  умолчание глобальным `GRANT … TO PUBLIC`, `pg_default_acl` для `app_owner` пуст.
- **L-2** (владение без стража): тест проверяет владельцев расширений, перечислений и всех
  объектов `public` кроме `_yoyo_*`/`yoyo_lock` и членов расширений (у trusted-расширений они
  принадлежат bootstrap-суперпользователю — исключены по `pg_depend deptype = 'e'`), значения
  перечислений побуквенно, записи `pg_default_acl` (DML `app_rw`, SELECT `app_backup`, нет `=X`);
  плюс статический страж `SET LOCAL ROLE` по файлам миграций.
- **L-3**: `CREATE EXTENSION` без `IF NOT EXISTS`.
- **L-4**: `migrate_dsn()` возвращает скобки IPv6-хосту.
- **L-5**: `test_migrate_is_idempotent` сам приводит базу к «всё применено», порядок не важен.
- **L-6**: комментарии `docker-compose.yml`, `.dockerignore`, `conftest.py`, `secrets/README.md`
  переписаны под bootstrap-схему.
- Стиль: соответствие `event_type` строкам §4.17 выписано в миграции; `REVOKE CONNECT … FROM
  PUBLIC` (`datacl` → `=T/app_owner`, без `c`); `requirements.lock` регенерирован `pip freeze`;
  ошибка подключения в `migrate` — одна строка вместо трассировки.
- Регрессия после правок: `make check` → 0 (pytest 4 passed); `yoyo apply/rollback --all/apply`
  через `yoyo.ini` → 0/0/0; стенд пересоздан с `down -v` — bootstrap отработал, api применил 0001.
- Замечание ревью о `pg_hba` внесено в скилл: `psql` внутри контейнера по `127.0.0.1` идёт по
  `trust`, пароли ролей проверяются только через опубликованный порт.

## Раунд 3 — страж L-1

Ревью раунда 2 показало, что ассерт «нет записи `=X` в `pg_default_acl`» не может покраснеть:
встроенное умолчание PostgreSQL там не хранится, а отзыв виден только как глобальная запись
`{app_owner=X/app_owner}`. Заменено двумя стражами в `test_migrations.py`:

- точная запись: `pg_default_acl` для `app_owner` разбирается по (тип, область) —
  `("f", "global") == {"app_owner=X/app_owner"}`, `("r", "public") == {app_rw=arwd, app_backup=r}`,
  `("S", "public") == {app_rw=rU}`, `("f", "public") == {app_rw=X}`;
- операционный: под `MIGRATE_DSN` в откатываемой транзакции `SET LOCAL ROLE app_owner; CREATE
  FUNCTION _acl_probe()`, затем `has_function_privilege('app_backup', …) = false`,
  `has_function_privilege('app_rw', …) = true`, в ACL нет `=X`; транзакция откатывается
  (`psycopg.Rollback`), остатков `_acl_probe` в базе — 0.

Инварианты мигрированной базы вынесены в `assert_migrated_state()` и проверяются и после первого
`migrate`, и после повторного применения: первый запуск на мигрированной базе ничего не применяет,
и только повторное применение выполняет текущие файлы миграций. Посадки: файл 0001 без глобального
`REVOKE` → `AssertionError: EXECUTE у PUBLIC … отозван глобальной записью`; без `REVOKE` и без
проверки записи → операционный страж: `app_backup выполняет функцию app_owner: acl=['=X/app_owner',
…]`. Файлы восстановлены, `make check` → 0, pytest 4 passed. Стиль: `btrim` в проверке паролей
`roles.sql`; статический страж отвергает python-миграции.

## Критерии приёмки

- [x] Расширения `btree_gist` и `citext` установлены (владелец `app_owner`, без суперпользователя —
      расширения trusted)
- [x] Четыре роли существуют; `app_rw` не является владельцем объектов (запрос по `pg_class` и
      `pg_type` → 0)
- [x] Перечисления §4.2 созданы — 22 типа, включая `event_type` с 12 событиями §4.17

## Отклонения от описания задачи

- Роли создаёт bootstrap при инициализации кластера, а не миграция (см. выше); откат роли не
  удаляет. Добавлены `control-plane/migrations/bootstrap/roles.sql`,
  `deploy/compose/postgres/entrypoint.sh`, `deploy/compose/postgres/initdb.d/10-roles.sh`,
  секрет `pg_app_backup_password`, шаг bootstrap в `.github/workflows/ci.yml`.
- Зависимость `psycopg[binary]` (драйвер yoyo для PostgreSQL) добавлена в `pyproject.toml` и
  оба lock-файла.
- Сквозной тест оформлен как pytest (`tests/e2e/`), чтобы TC-E2E-01 выполнялся в CI и на стенде.
