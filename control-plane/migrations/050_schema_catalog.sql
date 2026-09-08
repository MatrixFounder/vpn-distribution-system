-- 050: схема «тарифы, группы, коэффициенты» — docs/architectures/data-model.md §4.2.2:
-- plans, plan_protocols, access_groups, plan_access_groups, billing_groups,
-- billing_group_multipliers, node_billing_assignments. Права app_rw/app_backup — умолчания
-- привилегий app_owner (0001, §4.6). Сверх §4.2.2 (объявлено в задаче): умолчания статусов и
-- времени, CHECK на порядок границ интервалов истории, индексы обратного поиска по внешним ключам,
-- ON DELETE CASCADE на связях состава тарифа (plan_protocols, plan_access_groups): удаление тарифа
-- или группы доступа уносит строки состава. История коэффициентов — без каскада (бессрочно, §4.5).
-- FK node_billing_assignments.node_id → nodes добавляет миграция 060: таблицы nodes здесь ещё нет.
-- depends: 0001_extensions_roles_enums

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

-- Тарифы (R-07): срок, лимит трафика (NULL = Unlimited), лимит устройств; цена справочная (О-2).
CREATE TABLE plans (
    id                   uuid          PRIMARY KEY DEFAULT uuidv7(),
    name                 text          NOT NULL UNIQUE,
    price_amount         numeric(12,2),
    price_currency       char(3),                               -- ОВ-19
    duration_days        int           NOT NULL CHECK (duration_days > 0),
    traffic_limit_bytes  bigint,                                -- NULL = Unlimited
    device_limit         int,
    status               plan_status   NOT NULL DEFAULT 'active',
    created_at           timestamptz   NOT NULL DEFAULT now(),
    updated_at           timestamptz   NOT NULL DEFAULT now()
);

-- Профили inbound, доступные тарифу (§4.4 постановки).
CREATE TABLE plan_protocols (
    plan_id  uuid            NOT NULL REFERENCES plans (id) ON DELETE CASCADE,
    profile  inbound_profile NOT NULL,
    PRIMARY KEY (plan_id, profile)
);

-- Группы доступа: пересечение групп тарифа и ноды даёт состав (§11.3).
CREATE TABLE access_groups (
    id           uuid  PRIMARY KEY DEFAULT uuidv7(),
    name         text  NOT NULL UNIQUE,
    description  text  NOT NULL DEFAULT ''
);

CREATE TABLE plan_access_groups (
    plan_id          uuid NOT NULL REFERENCES plans (id) ON DELETE CASCADE,
    access_group_id  uuid NOT NULL REFERENCES access_groups (id) ON DELETE CASCADE,
    PRIMARY KEY (plan_id, access_group_id)
);
CREATE INDEX plan_access_groups_access_group_id_idx ON plan_access_groups (access_group_id);

-- Тарифицируемые группы (R-18, R-23).
CREATE TABLE billing_groups (
    id    uuid  PRIMARY KEY DEFAULT uuidv7(),
    name  text  NOT NULL UNIQUE
);

-- История коэффициента группы (§4.9): интервалы одной группы не пересекаются (B-1);
-- коэффициент 0.0–10.0 с шагом 0.1 в тысячных (R-23). Хранится бессрочно (§4.5).
CREATE TABLE billing_group_multipliers (
    id                uuid         PRIMARY KEY DEFAULT uuidv7(),
    billing_group_id  uuid         NOT NULL REFERENCES billing_groups (id),
    multiplier_milli  int          NOT NULL
        CHECK (multiplier_milli BETWEEN 0 AND 10000 AND multiplier_milli % 100 = 0),
    valid_from        timestamptz  NOT NULL,
    valid_to          timestamptz,                               -- NULL = действует
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    EXCLUDE USING gist (billing_group_id WITH =, tstzrange(valid_from, valid_to) WITH &&)
);

-- История назначения ноды тарифицируемой группе с переопределением коэффициента (§4.9).
-- nodes.billing_group_id и nodes.multiplier_milli дублируют действующее назначение (R-18).
CREATE TABLE node_billing_assignments (
    id                          uuid         PRIMARY KEY DEFAULT uuidv7(),
    node_id                     uuid         NOT NULL,           -- FK → nodes: миграция 060
    billing_group_id            uuid         NOT NULL REFERENCES billing_groups (id),
    multiplier_override_milli   int
        CHECK (multiplier_override_milli BETWEEN 0 AND 10000 AND multiplier_override_milli % 100 = 0),
    valid_from                  timestamptz  NOT NULL,
    valid_to                    timestamptz,
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    EXCLUDE USING gist (node_id WITH =, tstzrange(valid_from, valid_to) WITH &&)
);
CREATE INDEX node_billing_assignments_billing_group_id_idx
    ON node_billing_assignments (billing_group_id);
