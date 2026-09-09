-- 090: схема «события, доставки, очередь задач, аудит, настройки» — data-model.md §4.2.6:
-- events, email_deliveries, webhook_deliveries, jobs, audit_log, settings. Права app_rw/app_backup —
-- умолчания привилегий app_owner (0001, §4.6).
-- Append-only audit_log (R-37, §7.2, §4.6) — на привилегиях, а не на тексте: UPDATE/DELETE/TRUNCATE
-- отозваны и у app_rw, и у владельца app_owner (владелец может отозвать собственные привилегии);
-- DELETE (и SELECT одной колонки ts) держит только роль app_audit_purge без входа (bootstrap
-- roles.sql), ей же принадлежит purge_audit_log() — SECURITY DEFINER, 12 месяцев (Н-23, §4.5).
-- Триггер audit_log_immutable для всех ролей пропускает только DELETE под current_user =
-- app_audit_purge: подделать роль нельзя, в отличие от текста запроса или настройки сессии.
-- Остаточный путь мимо функции — явный SET ROLE app_audit_purge (app_owner — член без
-- наследования) или GRANT владельцем самому себе: заметные действия, равносильные DISABLE TRIGGER;
-- защиты от владельца базы внутри базы не существует.
-- Сверх §4.2.6 (перечислено поимённо в задаче): умолчания (uuidv7(), now(), '{}', 'pending', 0);
-- NOT NULL у всех колонок без пометки NULL; CHECK attempts >= 0, max_attempts > 0, «locked_at и
-- locked_by заданы вместе», result IN ('success', 'denied', 'error') (§4.16); identity для jobs.id и
-- audit_log.id; ON DELETE CASCADE у доставок к событию (доставка без события бессмысленна, срок
-- хранения общий); индексы по FK email_deliveries (event_id), webhook_deliveries (event_id);
-- триггер запрещает и TRUNCATE; events.user_id/node_id и audit_log.actor_id/impersonated_user_id —
-- без FK (ссылки по идентификатору, B-3; записи переживают удаление аккаунта); строка
-- settings 'state_generation' = 0 — первый старт C-01 без метки на хосте увеличивает поколение
-- безусловно (reliability.md §9.2). Сроки хранения строк остальных таблиц §4.5 — задача 001.37.
-- depends: 080_schema_accounting

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

-- События §4.17 для уведомлений; dedup_key подавляет повторы за период.
CREATE TABLE events (
    id          uuid         PRIMARY KEY DEFAULT uuidv7(),
    type        event_type   NOT NULL,
    user_id     uuid,                                            -- без FK (B-3)
    node_id     uuid,                                            -- без FK
    payload     jsonb        NOT NULL DEFAULT '{}',
    dedup_key   text         NOT NULL UNIQUE,
    created_at  timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE email_deliveries (
    id          uuid             PRIMARY KEY DEFAULT uuidv7(),
    event_id    uuid             NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    recipient   citext           NOT NULL,
    language    text             NOT NULL,
    status      delivery_status  NOT NULL DEFAULT 'pending',
    attempts    int              NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error  text,
    sent_at     timestamptz
);
CREATE INDEX email_deliveries_event_id_idx ON email_deliveries (event_id);

CREATE TABLE webhook_deliveries (
    id               uuid             PRIMARY KEY DEFAULT uuidv7(),
    event_id         uuid             NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    url              text             NOT NULL,
    status           delivery_status  NOT NULL DEFAULT 'pending',
    attempts         int              NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at  timestamptz,
    response_code    int
);
CREATE INDEX webhook_deliveries_event_id_idx ON webhook_deliveries (event_id);

-- Очередь задач C-02/C-03 (§5.10); идемпотентность по ключу среди активных задач (R-46, §4.4).
CREATE TABLE jobs (
    id               bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    queue            job_queue    NOT NULL,
    type             text         NOT NULL,
    payload          jsonb        NOT NULL DEFAULT '{}',
    idempotency_key  text         NOT NULL,
    run_at           timestamptz  NOT NULL DEFAULT now(),
    attempts         int          NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts     int          NOT NULL CHECK (max_attempts > 0),
    locked_at        timestamptz,
    locked_by        text,
    status           job_status   NOT NULL DEFAULT 'pending',
    last_error       text,
    created_at       timestamptz  NOT NULL DEFAULT now(),
    CHECK ((locked_at IS NULL) = (locked_by IS NULL))
);
CREATE INDEX jobs_pending_idx ON jobs (queue, status, run_at) WHERE status = 'pending';
CREATE UNIQUE INDEX jobs_idempotency_active_idx ON jobs (idempotency_key)
    WHERE status IN ('pending', 'running');

-- Журнал аудита — append-only (§7.2, R-37): привилегии (см. заголовок) плюс триггер для всех ролей.
CREATE TABLE audit_log (
    id                    bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts                    timestamptz  NOT NULL DEFAULT now(),
    actor_type            actor_type   NOT NULL,
    actor_id              uuid,
    actor_role            text,
    session_id            text,
    impersonated_user_id  uuid,                                  -- режим от лица пользователя (§5.11)
    user_agent            text,
    action                text         NOT NULL,
    entity_type           text         NOT NULL,
    entity_id             text         NOT NULL,
    old_value             jsonb,                                 -- без PII (B-3, §4.4)
    new_value             jsonb,
    ip                    inet,
    result                text         NOT NULL CHECK (result IN ('success', 'denied', 'error'))  -- §4.16
);
CREATE INDEX audit_log_ts_idx ON audit_log (ts);
CREATE INDEX audit_log_entity_type_entity_id_idx ON audit_log (entity_type, entity_id);
CREATE INDEX audit_log_actor_id_ts_idx ON audit_log (actor_id, ts);
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM app_rw;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM app_owner;    -- и у владельца: только добавление
GRANT SELECT (ts), DELETE ON audit_log TO app_audit_purge;      -- ts — для условия срока; содержимое
                                                                -- журнала роли очистки не видно

-- Триггерная функция: изменение и удаление строк аудита отклоняются для всех ролей; единственный
-- разрешённый путь — DELETE под ролью app_audit_purge, то есть изнутри purge_audit_log() (§4.6).
-- Проверяется роль, а не текст запроса (PG_CONTEXT) и не настройка сессии: их подделает любой,
-- кто может выполнить оператор.
CREATE FUNCTION audit_log_immutable() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_user = 'app_audit_purge' THEN
        RETURN OLD;  -- удаление по сроку хранения
    END IF;
    RAISE EXCEPTION 'audit_log: журнал аудита только для добавления (% запрещён)', TG_OP;
END;
$$;
REVOKE EXECUTE ON FUNCTION audit_log_immutable() FROM app_rw;
CREATE TRIGGER audit_log_immutable BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
CREATE TRIGGER audit_log_immutable_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable();

-- Удаление записей аудита старше 12 месяцев (Н-23, §4.5): граница ts < now() - 12 месяцев, возвращает
-- число удалённых. Принадлежит app_audit_purge (создаётся под SET LOCAL ROLE; CREATE на схему —
-- только на время создания), SECURITY DEFINER, вызывает планировщик под app_rw. Срок — константа,
-- не параметр: параметр дал бы приложению возможность стереть журнал. timezone закреплён, как у
-- функций 080: арифметика месяцев на timestamptz идёт в поясе сессии (расхождение — на днях
-- перевода часов и на границах месяцев). Один DELETE без порций: планировщик вызывает функцию
-- ежесуточно, за вызов уходит примерно суточный объём по индексу audit_log_ts_idx.
-- Функции app_audit_purge не покрыты умолчаниями привилегий 0001 (они — для функций app_owner):
-- EXECUTE у PUBLIC отзывается и выдаётся app_rw явно.
GRANT CREATE ON SCHEMA control_plane TO app_audit_purge;
SET LOCAL ROLE app_audit_purge;
CREATE FUNCTION purge_audit_log() RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = control_plane, pg_temp
    SET timezone = 'UTC' AS $$
DECLARE
    purged bigint;
BEGIN
    DELETE FROM audit_log WHERE ts < now() - interval '12 months';
    GET DIAGNOSTICS purged = ROW_COUNT;
    RETURN purged;
END;
$$;
REVOKE EXECUTE ON FUNCTION purge_audit_log() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_audit_log() TO app_rw;
SET LOCAL ROLE app_owner;
REVOKE CREATE ON SCHEMA control_plane FROM app_audit_purge;

-- Настройки (§4.2.6): значения jsonb по ключам; state_generation — поколение состояния для агентов
-- (§5.2, reliability.md §9.2).
CREATE TABLE settings (
    key         text         PRIMARY KEY,
    value       jsonb        NOT NULL,
    updated_at  timestamptz  NOT NULL DEFAULT now()
);
INSERT INTO settings (key, value) VALUES ('state_generation', '0');
