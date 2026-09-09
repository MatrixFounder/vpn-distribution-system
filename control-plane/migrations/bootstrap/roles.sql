-- Роли базы по docs/architectures/data-model.md §4.6 — кластерные объекты, поэтому не миграция yoyo:
-- выполняется один раз под суперпользователем при инициализации кластера
-- (deploy/compose/postgres/initdb.d/10-roles.sh) или в CI перед тестами. Повторный запуск безопасен.
--
-- Переменные psql: :db — имя базы; :rw, :mig, :bk — пароли app_rw, app_migrate, app_backup.
--   psql -v ON_ERROR_STOP=1 -v db=control_plane -v rw=... -v mig=... -v bk=... -f roles.sql
--
--   app_owner   — владелец базы и всех объектов, без входа; миграции выполняют SET LOCAL ROLE app_owner
--   app_rw      — C-01…C-03, только DML (привилегии на объекты — миграциями)
--   app_migrate — миграции yoyo; член app_owner
--   app_backup  — только чтение, для C-11
--   app_audit_purge — без входа; единственный держатель DELETE на audit_log и владелец функции
--                 purge_audit_log() (миграция 090, §4.6, §7.2). app_owner — член без наследования
--                 (INHERIT FALSE, SET TRUE): прав роли не получает, но может SET ROLE — так миграции
--                 создают и удаляют функцию; явный SET ROLE — единственный путь к DELETE мимо функции.
--
-- Все объекты Control Plane живут в схеме control_plane, а не в public: соседство с другими
-- решениями в одном кластере не должно давать конфликтов имён. Роли приложения получают
-- search_path = control_plane; служебные таблицы yoyo тоже создаются там.

\set ON_ERROR_STOP on

-- Пустой (или из одних пробелов) пароль PostgreSQL молча превращает в NULL — роль с LOGIN без
-- пароля; здесь это ошибка.
-- Отсутствующая переменная даёт синтаксическую ошибку сама по себе.
SELECT length(btrim(:'rw')) > 0 AS rw_ok, length(btrim(:'mig')) > 0 AS mig_ok,
       length(btrim(:'bk')) > 0 AS bk_ok \gset
\if :rw_ok
\else
DO $$ BEGIN RAISE EXCEPTION 'roles.sql: пустой пароль app_rw (переменная rw)'; END $$;
\endif
\if :mig_ok
\else
DO $$ BEGIN RAISE EXCEPTION 'roles.sql: пустой пароль app_migrate (переменная mig)'; END $$;
\endif
\if :bk_ok
\else
DO $$ BEGIN RAISE EXCEPTION 'roles.sql: пустой пароль app_backup (переменная bk)'; END $$;
\endif

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_owner') THEN
        CREATE ROLE app_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_rw') THEN
        CREATE ROLE app_rw NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_migrate') THEN
        CREATE ROLE app_migrate NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_backup') THEN
        CREATE ROLE app_backup NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_audit_purge') THEN
        CREATE ROLE app_audit_purge NOLOGIN;
    END IF;
END
$$;

ALTER ROLE app_rw      WITH LOGIN PASSWORD :'rw';
ALTER ROLE app_migrate WITH LOGIN PASSWORD :'mig';
ALTER ROLE app_backup  WITH LOGIN PASSWORD :'bk';

-- Миграции создают объекты от имени app_owner (SET LOCAL ROLE); app_migrate наследует его права.
-- app_owner может стать app_audit_purge (SET ROLE), но не наследует её DELETE на audit_log.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
                   JOIN pg_roles g ON g.oid = m.member
                   WHERE r.rolname = 'app_owner' AND g.rolname = 'app_migrate') THEN
        GRANT app_owner TO app_migrate;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
                   JOIN pg_roles g ON g.oid = m.member
                   WHERE r.rolname = 'app_audit_purge' AND g.rolname = 'app_owner') THEN
        GRANT app_audit_purge TO app_owner WITH INHERIT FALSE, SET TRUE;
    END IF;
END
$$;

-- Владелец базы — app_owner.
ALTER DATABASE :"db" OWNER TO app_owner;
-- Подключаться могут только роли приложения, не любая роль кластера.
REVOKE CONNECT ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO app_rw, app_migrate, app_backup;

-- Схема приложения; public остаётся пустым и в search_path ролей не входит.
CREATE SCHEMA IF NOT EXISTS control_plane AUTHORIZATION app_owner;
ALTER SCHEMA control_plane OWNER TO app_owner;  -- и для схемы, созданной ранее кем-то другим
GRANT USAGE ON SCHEMA control_plane TO app_rw, app_backup, app_audit_purge;
-- Служебные таблицы yoyo (_yoyo_migration, _yoyo_log, yoyo_lock) создаёт сам app_migrate.
GRANT USAGE, CREATE ON SCHEMA control_plane TO app_migrate;
ALTER ROLE app_rw      IN DATABASE :"db" SET search_path = control_plane;
ALTER ROLE app_migrate IN DATABASE :"db" SET search_path = control_plane;
ALTER ROLE app_backup  IN DATABASE :"db" SET search_path = control_plane;
ALTER ROLE app_audit_purge IN DATABASE :"db" SET search_path = control_plane;
