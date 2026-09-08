-- 060: схема «парк нод, inbound, состояние и команды» — docs/architectures/data-model.md §4.2.3:
-- nodes, node_ip_history, node_access_groups, bootstrap_tokens, node_identities, inbounds,
-- inbound_secrets, node_config_versions, node_user_credentials, node_user_state, commands,
-- node_metrics (партиции по суткам, 30 дней), node_country_availability. Плюс внешний ключ
-- node_billing_assignments.node_id → nodes, отложенный миграцией 050 (nodes тогда не было).
-- Права app_rw/app_backup — умолчания привилегий app_owner (0001, §4.6).
-- Сверх §4.2.3 (перечислено поимённо в задаче): умолчания статусов, счётчиков, версий, jsonb,
-- массивов, флагов и меток времени now(); CHECK bandwidth_mbps > 0 и max_conn_per_ip > 0;
-- node_ip_history: valid_to NULL = действующий адрес и CHECK порядка границ (как у историй 050),
-- id — identity; индексы по внешним ключам node_id (node_ip_history, bootstrap_tokens);
-- ON DELETE CASCADE только у node_access_groups (состав групп) и inbound_secrets (секрет без
-- inbound бессмыслен) — остальные связи с nodes без действия при удалении (NO ACTION,
-- умолчание): нода выводится через decommissioned_at, а не удалением.
-- depends: 040_schema_identity 050_schema_catalog

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

-- Ноды: статусы §4.6 постановки, денормализация тарифицируемой группы (R-18) и переопределения.
CREATE TABLE nodes (
    id                       uuid          PRIMARY KEY DEFAULT uuidv7(),
    code                     text          NOT NULL UNIQUE,            -- JP-Tokyo-01
    name                     text          NOT NULL,
    country                  char(2)       NOT NULL,
    city                     text          NOT NULL,
    provider                 text          NOT NULL,
    public_ipv4              inet          NOT NULL,
    public_ipv6              inet,
    fqdn                     text,
    status                   node_status   NOT NULL DEFAULT 'pending',
    status_changed_at        timestamptz   NOT NULL DEFAULT now(),
    last_heartbeat_at        timestamptz,
    missed_heartbeats        int           NOT NULL DEFAULT 0,        -- счётчик для Н-15
    agent_version            text,
    xray_version             text,
    billing_group_id         uuid          NOT NULL REFERENCES billing_groups (id),  -- R-18
    multiplier_milli         int
        CHECK (multiplier_milli BETWEEN 0 AND 10000 AND multiplier_milli % 100 = 0),
    resync_required          boolean       NOT NULL DEFAULT false,    -- полный снапшот (§5.2)
    ok_heartbeats            int           NOT NULL DEFAULT 0,        -- Н-15, §4.7
    bandwidth_mbps           int           NOT NULL CHECK (bandwidth_mbps > 0),      -- §5.9
    max_conn_per_ip          int           NOT NULL CHECK (max_conn_per_ip > 0),     -- Н-29
    desired_config_version   int           NOT NULL DEFAULT 0,
    applied_config_version   int           NOT NULL DEFAULT 0,
    desired_users_seq        bigint        NOT NULL DEFAULT 0,
    applied_users_seq        bigint        NOT NULL DEFAULT 0,
    applied_at               timestamptz,
    monthly_cost             numeric(12,2),
    currency                 char(3),
    provider_account         text,
    cost_valid_from          date,
    cost_valid_to            date,
    traffic_included_bytes   bigint,
    traffic_overage_cost     numeric(12,4),
    legal_profile            jsonb         NOT NULL DEFAULT '{}'::jsonb,  -- Р-10
    created_at               timestamptz   NOT NULL DEFAULT now(),
    decommissioned_at        timestamptz
);
CREATE INDEX nodes_status_idx ON nodes (status);
CREATE INDEX nodes_billing_group_id_idx ON nodes (billing_group_id);

-- Отложенный внешний ключ истории назначений (миграция 050).
ALTER TABLE node_billing_assignments
    ADD CONSTRAINT node_billing_assignments_node_id_fkey FOREIGN KEY (node_id) REFERENCES nodes (id);

-- История публичных адресов ноды (уведомление пользователей при смене, §4.17).
CREATE TABLE node_ip_history (
    id           bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    node_id      uuid         NOT NULL REFERENCES nodes (id),
    public_ipv4  inet         NOT NULL,
    valid_from   timestamptz  NOT NULL,
    valid_to     timestamptz,                                          -- NULL = действует
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);
CREATE INDEX node_ip_history_node_id_idx ON node_ip_history (node_id);

-- Группы доступа ноды (§11.3): пересечение с группами тарифа даёт состав.
CREATE TABLE node_access_groups (
    node_id          uuid NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
    access_group_id  uuid NOT NULL REFERENCES access_groups (id) ON DELETE CASCADE,
    PRIMARY KEY (node_id, access_group_id)
);
CREATE INDEX node_access_groups_access_group_id_idx ON node_access_groups (access_group_id);

-- Одноразовые bootstrap-токены enrollment (§7.1, Н-24): хранится хеш.
CREATE TABLE bootstrap_tokens (
    id          uuid         PRIMARY KEY DEFAULT uuidv7(),
    node_id     uuid         NOT NULL REFERENCES nodes (id),
    token_hash  text         NOT NULL UNIQUE,
    expires_at  timestamptz  NOT NULL,
    used_at     timestamptz,
    created_by  uuid         NOT NULL REFERENCES admin_users (id)
);
CREATE INDEX bootstrap_tokens_node_id_idx ON bootstrap_tokens (node_id);

-- Identity ноды: отпечаток клиентского сертификата (SHA-256) и токен, поколение (§5.3).
CREATE TABLE node_identities (
    id                uuid         PRIMARY KEY DEFAULT uuidv7(),
    node_id           uuid         NOT NULL REFERENCES nodes (id),
    cert_fingerprint  text         NOT NULL UNIQUE,
    token_hash        text         NOT NULL,
    generation        int          NOT NULL,
    issued_at         timestamptz  NOT NULL DEFAULT now(),
    expires_at        timestamptz  NOT NULL,
    revoked_at        timestamptz
);
CREATE INDEX node_identities_active_idx ON node_identities (node_id) WHERE revoked_at IS NULL;

-- Inbound Xray: профиль и порт уникальны в пределах ноды (R-03); параметры REALITY/XHTTP §4.4.
CREATE TABLE inbounds (
    id                       uuid             PRIMARY KEY DEFAULT uuidv7(),
    node_id                  uuid             NOT NULL REFERENCES nodes (id),
    profile                  inbound_profile  NOT NULL,
    port                     int              NOT NULL CHECK (port BETWEEN 1 AND 65535),
    tag                      text             NOT NULL,
    enabled                  boolean          NOT NULL DEFAULT true,
    reality_public_key       text             NOT NULL,
    reality_short_ids        text[]           NOT NULL DEFAULT '{}',   -- hex, чётная длина, ≤ 16
    reality_target           text             NOT NULL,
    reality_server_names     text[]           NOT NULL DEFAULT '{}',
    reality_min_client_ver   text             NOT NULL,
    reality_max_client_ver   text,
    reality_xver             int              NOT NULL DEFAULT 0,
    reality_limit_fb_up      int              NOT NULL DEFAULT 0,
    reality_limit_fb_down    int              NOT NULL DEFAULT 0,
    client_fingerprint       text             NOT NULL,
    client_spider_x          text             NOT NULL DEFAULT '',
    trusted_x_forwarded_for  inet[]           NOT NULL DEFAULT '{}',   -- sockopt, за CDN (Д-14)
    params                   jsonb            NOT NULL DEFAULT '{}'::jsonb,  -- поля профиля §4.4
    error_state              text,                                    -- недоступность цели (Н-30)
    error_since              timestamptz,
    UNIQUE (node_id, profile),
    UNIQUE (node_id, port)
);

-- Приватный ключ REALITY (x25519), зашифрован на уровне приложения (§7.2).
CREATE TABLE inbound_secrets (
    inbound_id       uuid         PRIMARY KEY REFERENCES inbounds (id) ON DELETE CASCADE,
    private_key_enc  bytea        NOT NULL,
    key_version      int          NOT NULL DEFAULT 1,
    rotated_at       timestamptz  NOT NULL DEFAULT now()
);

-- Версии структурной конфигурации Xray без credentials (§5.2).
CREATE TABLE node_config_versions (
    node_id         uuid         NOT NULL REFERENCES nodes (id),
    config_version  int          NOT NULL,
    config_json     jsonb        NOT NULL,
    checksum        text         NOT NULL,
    created_at      timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id, config_version)
);

-- Credentials пары «пользователь × нода» (R-19): один на пару, шифрование §7.2.
CREATE TABLE node_user_credentials (
    id                   uuid         PRIMARY KEY DEFAULT uuidv7(),
    user_id              uuid         NOT NULL REFERENCES users (id),
    node_id              uuid         NOT NULL REFERENCES nodes (id),
    xray_email           text         NOT NULL,                       -- устойчивый тег u{user_id}
    vless_uuid_enc       bytea        NOT NULL,
    trojan_password_enc  bytea        NOT NULL,
    version              int          NOT NULL DEFAULT 1,
    rotated_at           timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (user_id, node_id)
);
CREATE INDEX node_user_credentials_node_id_idx ON node_user_credentials (node_id);

-- Материализованный поток состава ноды (§5.2): updated_seq выделяется только через
-- UPDATE nodes … RETURNING desired_users_seq (M-2), не из последовательности.
CREATE TABLE node_user_state (
    node_id            uuid             NOT NULL REFERENCES nodes (id),
    user_id            uuid             NOT NULL REFERENCES users (id),
    state              user_node_state  NOT NULL,
    quota_grant_bytes  bigint           NOT NULL DEFAULT 0,
    blocked_ips        inet[]           NOT NULL DEFAULT '{}',
    updated_seq        bigint           NOT NULL,
    updated_at         timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id, user_id)
);
CREATE INDEX node_user_state_node_id_updated_seq_idx ON node_user_state (node_id, updated_seq);

-- Команды агенту (§5.2): срок действия, статус доставки и результат.
CREATE TABLE commands (
    id          uuid            PRIMARY KEY DEFAULT uuidv7(),           -- command_id
    node_id     uuid            NOT NULL REFERENCES nodes (id),
    type        command_type    NOT NULL,
    payload     jsonb           NOT NULL DEFAULT '{}'::jsonb,
    issued_at   timestamptz     NOT NULL DEFAULT now(),
    expires_at  timestamptz     NOT NULL,
    status      command_status  NOT NULL DEFAULT 'issued',
    result      jsonb
);
CREATE INDEX commands_node_id_status_idx ON commands (node_id, status);

-- Метрики ноды: партиции по суткам, хранение 30 дней (§4.5); без FK (партиции, §4.3).
-- Партиции создаёт планировщик функцией из 001.08.
CREATE TABLE node_metrics (
    node_id       uuid         NOT NULL,
    ts            timestamptz  NOT NULL,
    cpu_pct       real         NOT NULL,
    mem_pct       real         NOT NULL,
    disk_pct      real         NOT NULL,
    net_rx_bytes  bigint       NOT NULL,
    net_tx_bytes  bigint       NOT NULL,
    online_ips    int          NOT NULL,
    connections   int          NOT NULL,                              -- по данным ОС (§4.7)
    PRIMARY KEY (node_id, ts)
) PARTITION BY RANGE (ts);

-- Доступность ноды из стран (R-06, приоритет S).
CREATE TABLE node_country_availability (
    node_id     uuid         NOT NULL REFERENCES nodes (id),
    country     char(2)      NOT NULL,
    available   boolean      NOT NULL,
    checked_at  timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id, country)
);
