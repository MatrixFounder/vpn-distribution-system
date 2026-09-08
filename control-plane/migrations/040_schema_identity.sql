-- 040: схема «учётные записи и аутентификация» — docs/architectures/data-model.md §4.2.1:
-- users, admin_users, admin_recovery_codes, email_tokens, auth_events. Идентификаторы — uuid v7
-- (uuidv7() PostgreSQL 18), время — timestamptz в UTC. Права app_rw (DML) и app_backup (SELECT)
-- приходят из умолчаний привилегий app_owner (0001, §4.6); особых запретов у этой группы нет.
-- Сверх §4.2.1 (объявлено в задаче): умолчания статусов/языка/времени, индекс по FK
-- email_tokens.user_id, CHECK на auth_events.result. Язык — text без CHECK: третий язык
-- добавляется без изменения схемы (R-51, AC-22), список допустимых локалей — у приложения.
-- depends: 0001_extensions_roles_enums

SET LOCAL ROLE app_owner;

-- Пользователи кабинета. Удаление аккаунта (UC-14) стирает PII и ставит deleted_at, строка остаётся.
CREATE TABLE users (
    id                uuid        PRIMARY KEY DEFAULT uuidv7(),
    email             citext      NOT NULL UNIQUE,            -- нормализованный адрес (§16.8)
    email_verified_at timestamptz,
    password_hash     text        NOT NULL,                   -- argon2id (§7.2)
    status            user_status NOT NULL DEFAULT 'active',
    language          text        NOT NULL DEFAULT 'en',      -- ru | en, расширяемо (R-51)
    timezone          text        NOT NULL DEFAULT 'UTC',     -- IANA (R-52)
    aup_version       text        NOT NULL,                   -- принятая версия правил (§16.5)
    aup_accepted_at   timestamptz NOT NULL,
    announce_consent  boolean     NOT NULL DEFAULT false,     -- канал объявлений (§17.11)
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    deleted_at        timestamptz
);

-- Административные учётные записи: пароль + обязательный TOTP (§7.1).
CREATE TABLE admin_users (
    id               uuid         PRIMARY KEY DEFAULT uuidv7(),
    email            citext       NOT NULL UNIQUE,
    password_hash    text         NOT NULL,
    role             admin_role   NOT NULL,
    totp_secret_enc  bytea,                                   -- AES-256-GCM (§7.2)
    totp_enabled_at  timestamptz,
    status           admin_status NOT NULL DEFAULT 'active',
    language         text         NOT NULL DEFAULT 'en',
    created_at       timestamptz  NOT NULL DEFAULT now()
);

-- Резервные коды второго фактора; неиспользованные — по частичному индексу.
CREATE TABLE admin_recovery_codes (
    id             uuid        PRIMARY KEY DEFAULT uuidv7(),
    admin_user_id  uuid        NOT NULL REFERENCES admin_users (id) ON DELETE CASCADE,
    code_hash      text        NOT NULL,
    used_at        timestamptz
);
CREATE INDEX admin_recovery_codes_unused_idx
    ON admin_recovery_codes (admin_user_id) WHERE used_at IS NULL;

-- Токены подтверждения почты и восстановления пароля (UC-15); хранится только хеш (§7.2).
CREATE TABLE email_tokens (
    id          uuid             PRIMARY KEY DEFAULT uuidv7(),
    user_id     uuid             NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind        email_token_kind NOT NULL,
    token_hash  text             NOT NULL UNIQUE,
    expires_at  timestamptz      NOT NULL,                     -- срок ОВ-25
    used_at     timestamptz
);
CREATE INDEX email_tokens_user_id_idx ON email_tokens (user_id);

-- Журнал аутентификации: партиции по суткам, хранение 90 дней (§4.5, §16.2). Партиции создаёт
-- планировщик функцией из 001.08 на семь суток вперёд; здесь только родительская таблица.
CREATE SEQUENCE auth_events_id_seq AS bigint;
CREATE TABLE auth_events (
    id          bigint          NOT NULL DEFAULT nextval('auth_events_id_seq'),
    user_id     uuid,                                          -- без FK: партиции, §4.3
    kind        auth_event_kind NOT NULL,
    ts          timestamptz     NOT NULL DEFAULT now(),
    source_ip   inet,
    user_agent  text,
    result      text            NOT NULL CHECK (result IN ('success', 'denied')),  -- §4.2.1
    PRIMARY KEY (ts, id)
) PARTITION BY RANGE (ts);
ALTER SEQUENCE auth_events_id_seq OWNED BY auth_events.id;
CREATE INDEX auth_events_user_id_ts_idx ON auth_events (user_id, ts DESC);
