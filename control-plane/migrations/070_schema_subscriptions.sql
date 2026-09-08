-- 070: схема «подписки, баланс, токены, коды» — docs/architectures/data-model.md §4.2.4:
-- subscription_periods, subscriptions, balance_entries, subscription_tokens,
-- subscription_access_log (партиции по суткам, 30 дней), codes, code_redemptions, orders, payments
-- (заглушки О-2). Права app_rw/app_backup — умолчания привилегий app_owner (0001, §4.6); особый
-- запрет §4.6: balance_entries без UPDATE/DELETE у app_rw (журнал изменений баланса, R-21, R-38).
-- Сверх §4.2.4 (перечислено поимённо в задаче): умолчания статусов, счётчиков, меток времени;
-- CHECK period_end > period_start, CHECK на счётчики кодов (max_uses > 0, max_uses_per_user > 0,
-- uses_count >= 0, бонусы >= 0), CHECK «reason обязателен для adjustment» (§4.2.4);
-- индекс subscription_periods (period_end) без предиката — предикат WHERE period_end > now()
-- из модели PostgreSQL не допускает (now() не IMMUTABLE); индексы по FK code_redemptions (user_id),
-- orders (user_id), payments (order_id); identity для balance_entries.id; ON DELETE CASCADE у
-- subscriptions (строка состояния без пользователя бессмысленна), у остальных связей — NO ACTION.
-- Типы полей orders/payments (заглушки О-2) выбраны здесь: amount numeric(12,2), currency char(3),
-- status text.
-- depends: 040_schema_identity 050_schema_catalog

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

-- Периоды подписки: срок, лимит трафика периода (NULL = Unlimited), пороги 80/95 % (§4.17).
CREATE TABLE subscription_periods (
    id                   uuid           PRIMARY KEY DEFAULT uuidv7(),
    user_id              uuid           NOT NULL REFERENCES users (id),
    plan_id              uuid           NOT NULL REFERENCES plans (id),
    period_start         timestamptz    NOT NULL,
    period_end           timestamptz    NOT NULL,
    traffic_limit_bytes  bigint,                                        -- NULL = Unlimited
    used_billable_bytes  bigint         NOT NULL DEFAULT 0,             -- денормализация balance_entries
    notified_80_at       timestamptz,
    notified_95_at       timestamptz,
    exhausted_at         timestamptz,
    source               period_source  NOT NULL,                       -- redeem | admin | order
    source_id            uuid,
    created_at           timestamptz    NOT NULL DEFAULT now(),
    CHECK (period_end > period_start)
);
CREATE INDEX subscription_periods_user_id_period_end_idx
    ON subscription_periods (user_id, period_end DESC);
CREATE INDEX subscription_periods_period_end_idx ON subscription_periods (period_end);

-- Состояние подписки пользователя: одна строка на пользователя, текущий период — ссылка.
CREATE TABLE subscriptions (
    user_id            uuid                PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    state              subscription_state  NOT NULL DEFAULT 'none',
    current_period_id  uuid                REFERENCES subscription_periods (id),
    device_limit       int,                                               -- снимок из тарифа
    state_changed_at   timestamptz         NOT NULL DEFAULT now()
);

-- Журнал изменений баланса периода (R-21, R-38): только вставка от приложения (§4.6).
CREATE TABLE balance_entries (
    id                   bigint          GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    period_id            uuid            NOT NULL REFERENCES subscription_periods (id),
    source               balance_source  NOT NULL,   -- report | adjustment | bonus | late_report
    delta_billable_bytes bigint          NOT NULL,
    ref_key              text            NOT NULL,   -- ключ отчёта, id кода или id корректировки
    actor_id             uuid,
    reason               text,                       -- обязательно для adjustment
    created_at           timestamptz     NOT NULL DEFAULT now(),
    CHECK (source <> 'adjustment' OR reason IS NOT NULL)
);
CREATE INDEX balance_entries_period_id_created_at_idx ON balance_entries (period_id, created_at);
REVOKE UPDATE, DELETE ON balance_entries FROM app_rw;

-- Токены subscription URL (R-14): один действующий на пользователя, хранится хеш (§7.2).
CREATE TABLE subscription_tokens (
    id          uuid         PRIMARY KEY DEFAULT uuidv7(),
    user_id     uuid         NOT NULL REFERENCES users (id),
    token_hash  text         NOT NULL UNIQUE,
    issued_at   timestamptz  NOT NULL DEFAULT now(),
    revoked_at  timestamptz
);
CREATE UNIQUE INDEX subscription_tokens_active_user_idx
    ON subscription_tokens (user_id) WHERE revoked_at IS NULL;

-- Обращения к subscription-эндпоинту: партиции по суткам, 30 дней (§4.5, §16.2); без FK (§4.3).
-- Партиции создаёт планировщик функцией из 001.08; здесь только родительская таблица.
CREATE SEQUENCE subscription_access_log_id_seq AS bigint;
CREATE TABLE subscription_access_log (
    id          bigint       NOT NULL DEFAULT nextval('subscription_access_log_id_seq'),
    user_id     uuid         NOT NULL,
    ts          timestamptz  NOT NULL DEFAULT now(),
    domain      text         NOT NULL,
    format      text         NOT NULL,
    user_agent  text,                                                  -- заголовок может отсутствовать
    source_ip   inet         NOT NULL,
    country     char(2),
    PRIMARY KEY (ts, id)
) PARTITION BY RANGE (ts);
ALTER SEQUENCE subscription_access_log_id_seq OWNED BY subscription_access_log.id;
CREATE INDEX subscription_access_log_user_id_ts_idx ON subscription_access_log (user_id, ts DESC);

-- Redeem- и promo-коды (§4.14): хеш для поиска, шифрованный оригинал для экспорта.
CREATE TABLE codes (
    id                   uuid         PRIMARY KEY DEFAULT uuidv7(),
    kind                 code_kind    NOT NULL,                          -- redeem | promo
    code_hash            text         NOT NULL UNIQUE,
    code_enc             bytea        NOT NULL,
    batch_id             uuid,
    plan_id              uuid         REFERENCES plans (id),
    expires_at           timestamptz,
    max_uses             int          NOT NULL DEFAULT 1 CHECK (max_uses > 0),
    max_uses_per_user    int          NOT NULL DEFAULT 1 CHECK (max_uses_per_user > 0),
    uses_count           int          NOT NULL DEFAULT 0 CHECK (uses_count >= 0),
    traffic_bonus_bytes  bigint       NOT NULL DEFAULT 0 CHECK (traffic_bonus_bytes >= 0),
    duration_bonus_days  int          NOT NULL DEFAULT 0 CHECK (duration_bonus_days >= 0),
    created_by           uuid         NOT NULL REFERENCES admin_users (id),
    created_at           timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX codes_batch_id_idx ON codes (batch_id);

-- Активации кодов: код × пользователь × созданный или продлённый период.
CREATE TABLE code_redemptions (
    id           uuid         PRIMARY KEY DEFAULT uuidv7(),
    code_id      uuid         NOT NULL REFERENCES codes (id),
    user_id      uuid         NOT NULL REFERENCES users (id),
    period_id    uuid         NOT NULL REFERENCES subscription_periods (id),
    redeemed_at  timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX code_redemptions_code_id_user_id_idx ON code_redemptions (code_id, user_id);
CREATE INDEX code_redemptions_user_id_idx ON code_redemptions (user_id);

-- Заказы и платежи — заглушки О-2: подключение оплаты позже, структура зафиксирована сейчас.
CREATE TABLE orders (
    id          uuid           PRIMARY KEY DEFAULT uuidv7(),
    user_id     uuid           NOT NULL REFERENCES users (id),
    plan_id     uuid           NOT NULL REFERENCES plans (id),
    amount      numeric(12,2)  NOT NULL CHECK (amount >= 0),
    currency    char(3)        NOT NULL,
    status      text           NOT NULL,
    created_at  timestamptz    NOT NULL DEFAULT now()
);
CREATE INDEX orders_user_id_idx ON orders (user_id);

CREATE TABLE payments (
    id            uuid           PRIMARY KEY DEFAULT uuidv7(),
    order_id      uuid           NOT NULL REFERENCES orders (id),
    provider      text           NOT NULL,
    provider_ref  text           NOT NULL,
    amount        numeric(12,2)  NOT NULL CHECK (amount >= 0),
    currency      char(3)        NOT NULL,
    status        text           NOT NULL,
    created_at    timestamptz    NOT NULL DEFAULT now()
);
CREATE INDEX payments_order_id_idx ON payments (order_id);
