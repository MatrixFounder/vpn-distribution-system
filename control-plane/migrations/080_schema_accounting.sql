-- 080: схема «учёт трафика, гранты, адреса, функции обслуживания партиций» —
-- docs/architectures/data-model.md §4.2.5, §4.4, §4.5, §4.6: traffic_reports, traffic_lines
-- (партиции по суткам, 14 дней), traffic_hourly (по суткам, 90 дней), traffic_daily,
-- node_interface_hourly, traffic_gaps, reconciliation_runs, quota_grants, user_online_ips,
-- user_blocked_ips; реестр partition_policies и функции SECURITY DEFINER ensure_partitions /
-- drop_expired_partitions (владелец app_owner, вызывает планировщик под app_rw).
-- Права app_rw/app_backup — умолчания привилегий app_owner (0001, §4.6); особые запреты §4.6:
-- traffic_lines без UPDATE/DELETE (R-23), traffic_hourly без DELETE; те же запреты функция
-- накладывает на каждую новую партицию.
-- Сверх модели (перечислено поимённо в задаче): реестр partition_policies (только чтение у
-- app_rw), CHECK неотрицательности байтов и порядка границ периодов, CHECK коэффициента, триггер
-- монотонности traffic_hourly (правило §4.4 «значения только увеличиваются»), умолчания меток
-- времени и счётчиков, индексы traffic_gaps (node_id, gap_start) и reconciliation_runs
-- (kind, created_at); FK только там, где их ставит модель (§4.3: без FK на партиции и агрегаты).
-- depends: 060_schema_nodes 070_schema_subscriptions

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

-- Отчёты агентов: идемпотентность по ключу (R-21); счётчики интерфейса — второй источник (§5.9).
CREATE TABLE traffic_reports (
    node_id        uuid           NOT NULL REFERENCES nodes (id),
    counter_epoch  uuid           NOT NULL,
    report_seq     bigint         NOT NULL,
    period_start   timestamptz    NOT NULL,
    period_end     timestamptz    NOT NULL,
    received_at    timestamptz    NOT NULL DEFAULT now(),
    status         report_status  NOT NULL,
    node_rx_bytes  bigint         NOT NULL CHECK (node_rx_bytes >= 0),
    node_tx_bytes  bigint         NOT NULL CHECK (node_tx_bytes >= 0),
    PRIMARY KEY (node_id, counter_epoch, report_seq),
    CHECK (period_end > period_start)
);

-- Строки отчётов — факт: партиции по суткам period_start, 14 дней; без FK (§4.3);
-- billable_bytes неизменяем — у app_rw нет UPDATE/DELETE (R-23, §4.6).
CREATE TABLE traffic_lines (
    node_id             uuid         NOT NULL,
    counter_epoch       uuid         NOT NULL,
    report_seq          bigint       NOT NULL,
    user_id             uuid         NOT NULL,
    period_start        timestamptz  NOT NULL,
    period_end          timestamptz  NOT NULL,
    raw_uplink_bytes    bigint       NOT NULL CHECK (raw_uplink_bytes >= 0),
    raw_downlink_bytes  bigint       NOT NULL CHECK (raw_downlink_bytes >= 0),
    billable_bytes      bigint       NOT NULL CHECK (billable_bytes >= 0),
    multiplier_milli    int          NOT NULL
        CHECK (multiplier_milli BETWEEN 0 AND 10000 AND multiplier_milli % 100 = 0),
    billing_group_id    uuid         NOT NULL,
    PRIMARY KEY (period_start, node_id, counter_epoch, report_seq, user_id),
    CHECK (period_end > period_start)
) PARTITION BY RANGE (period_start);
CREATE INDEX traffic_lines_user_id_period_start_idx ON traffic_lines (user_id, period_start);
CREATE INDEX traffic_lines_node_id_period_start_idx ON traffic_lines (node_id, period_start);
REVOKE UPDATE, DELETE ON traffic_lines FROM app_rw;

-- Часовые агрегаты: партиции по суткам hour_start, 90 дней; ключ включает группу и коэффициент
-- (смена коэффициента закрывает строку часа, §4.9); только накопление (§4.4), без DELETE (§4.6).
CREATE TABLE traffic_hourly (
    user_id             uuid         NOT NULL,
    node_id             uuid         NOT NULL,
    hour_start          timestamptz  NOT NULL,
    raw_uplink_bytes    bigint       NOT NULL CHECK (raw_uplink_bytes >= 0),
    raw_downlink_bytes  bigint       NOT NULL CHECK (raw_downlink_bytes >= 0),
    billable_bytes      bigint       NOT NULL CHECK (billable_bytes >= 0),
    multiplier_milli    int          NOT NULL
        CHECK (multiplier_milli BETWEEN 0 AND 10000 AND multiplier_milli % 100 = 0),
    billing_group_id    uuid         NOT NULL,
    PRIMARY KEY (hour_start, user_id, node_id, billing_group_id, multiplier_milli)
) PARTITION BY RANGE (hour_start);
CREATE INDEX traffic_hourly_user_id_hour_start_idx ON traffic_hourly (user_id, hour_start);
CREATE INDEX traffic_hourly_node_id_hour_start_idx ON traffic_hourly (node_id, hour_start);
REVOKE DELETE ON traffic_hourly FROM app_rw;

-- Накопление часа: значения строки только увеличиваются; перезапись и уменьшение запрещены (§4.4).
CREATE FUNCTION traffic_hourly_only_grows() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.raw_uplink_bytes < OLD.raw_uplink_bytes
       OR NEW.raw_downlink_bytes < OLD.raw_downlink_bytes
       OR NEW.billable_bytes < OLD.billable_bytes THEN
        RAISE EXCEPTION 'traffic_hourly: значения строки часа только увеличиваются (§4.4)'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER traffic_hourly_only_grows
    BEFORE UPDATE ON traffic_hourly
    FOR EACH ROW EXECUTE FUNCTION traffic_hourly_only_grows();

-- Суточные агрегаты, 24 месяца (§4.5); без FK — переживают удаление аккаунта как история.
CREATE TABLE traffic_daily (
    user_id             uuid    NOT NULL,
    node_id             uuid    NOT NULL,
    day                 date    NOT NULL,
    raw_uplink_bytes    bigint  NOT NULL CHECK (raw_uplink_bytes >= 0),
    raw_downlink_bytes  bigint  NOT NULL CHECK (raw_downlink_bytes >= 0),
    billable_bytes      bigint  NOT NULL CHECK (billable_bytes >= 0),
    PRIMARY KEY (user_id, node_id, day)
);
CREATE INDEX traffic_daily_node_id_day_idx ON traffic_daily (node_id, day);

-- Счётчики интерфейса ноды по часам — второй источник сверки (§5.9), 90 дней.
CREATE TABLE node_interface_hourly (
    node_id     uuid         NOT NULL,
    hour_start  timestamptz  NOT NULL,
    rx_bytes    bigint       NOT NULL CHECK (rx_bytes >= 0),
    tx_bytes    bigint       NOT NULL CHECK (tx_bytes >= 0),
    PRIMARY KEY (node_id, hour_start)
);

-- Разрывы учёта (Н-8): интервал, причина, оценка объёма.
CREATE TABLE traffic_gaps (
    id               uuid         PRIMARY KEY DEFAULT uuidv7(),
    node_id          uuid         NOT NULL REFERENCES nodes (id),
    gap_start        timestamptz  NOT NULL,
    gap_end          timestamptz  NOT NULL,
    reason           text         NOT NULL,
    estimated_bytes  bigint       CHECK (estimated_bytes >= 0),
    CHECK (gap_end > gap_start)
);
CREATE INDEX traffic_gaps_node_id_gap_start_idx ON traffic_gaps (node_id, gap_start);

-- Прогоны сверок (§5.9): арифметика, второй источник, непрерывность.
CREATE TABLE reconciliation_runs (
    id          uuid                 PRIMARY KEY DEFAULT uuidv7(),
    kind        reconciliation_kind  NOT NULL,
    scope       jsonb                NOT NULL DEFAULT '{}'::jsonb,   -- день, нода
    expected    bigint               NOT NULL,
    actual      bigint               NOT NULL,
    delta_pct   numeric(6,3)         NOT NULL,
    status      text                 NOT NULL,
    created_at  timestamptz          NOT NULL DEFAULT now()
);
CREATE INDEX reconciliation_runs_kind_created_at_idx ON reconciliation_runs (kind, created_at);

-- Гранты квоты пары «пользователь × нода» (R-26): новый заменяет прежний (superseded_at).
CREATE TABLE quota_grants (
    id              uuid         PRIMARY KEY DEFAULT uuidv7(),
    user_id         uuid         NOT NULL REFERENCES users (id),
    node_id         uuid         NOT NULL REFERENCES nodes (id),
    period_id       uuid         NOT NULL REFERENCES subscription_periods (id),
    grant_bytes     bigint       NOT NULL CHECK (grant_bytes >= 0),
    consumed_bytes  bigint       NOT NULL DEFAULT 0 CHECK (consumed_bytes >= 0),
    issued_at       timestamptz  NOT NULL DEFAULT now(),
    issued_seq      bigint       NOT NULL,
    superseded_at   timestamptz
);
CREATE INDEX quota_grants_user_id_node_id_issued_at_idx
    ON quota_grants (user_id, node_id, issued_at DESC);
CREATE INDEX quota_grants_period_id_idx ON quota_grants (period_id);

-- Адреса онлайн-пользователей (лимит устройств, ОВ-23) и заблокированные адреса (Н-29).
CREATE TABLE user_online_ips (
    user_id    uuid         NOT NULL,
    node_id    uuid         NOT NULL,
    ip         inet         NOT NULL,
    last_seen  timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, node_id, ip)
);
CREATE INDEX user_online_ips_user_id_last_seen_idx ON user_online_ips (user_id, last_seen DESC);

CREATE TABLE user_blocked_ips (
    user_id        uuid         NOT NULL,
    ip             inet         NOT NULL,
    blocked_since  timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, ip)
);

-- Реестр партиционированных таблиц и сроков хранения (§4.5); ведётся миграциями, у app_rw только
-- чтение. revoke_from_app_rw — привилегии, снимаемые с каждой новой партиции (§4.6).
CREATE TABLE partition_policies (
    table_name          text    PRIMARY KEY CHECK (table_name ~ '^[a-z_]+$'),
    retention_days      int     NOT NULL CHECK (retention_days > 0),
    revoke_from_app_rw  text[]  NOT NULL DEFAULT '{}'
        CHECK (revoke_from_app_rw <@ ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE'])
);
REVOKE INSERT, UPDATE, DELETE ON partition_policies FROM app_rw;
INSERT INTO partition_policies (table_name, retention_days, revoke_from_app_rw) VALUES
    ('auth_events',             90, '{}'),
    ('subscription_access_log', 30, '{}'),
    ('node_metrics',            30, '{}'),
    ('traffic_lines',           14, '{UPDATE,DELETE}'),
    ('traffic_hourly',          90, '{DELETE}');

-- Создание суточных партиций на days_ahead суток вперёд для всех таблиц реестра (§4.5);
-- возвращает число суток, для которых партиция создана этим вызовом (при гонке двух вызовов
-- проигравший засчитывает партицию, созданную победителем: CREATE TABLE IF NOT EXISTS не
-- сообщает исход). SECURITY DEFINER: партиции принадлежат app_owner, вызывает
-- планировщик под app_rw; search_path закреплён (инъекция через схему невозможна), timezone
-- закреплён в UTC: сутки и их границы не зависят от сессии вызывающего (R-52).
CREATE FUNCTION ensure_partitions(days_ahead int DEFAULT 7) RETURNS int
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = control_plane, pg_temp
    SET timezone = 'UTC' AS $$
DECLARE
    policy     record;
    day        date;
    part_name  text;
    created    int := 0;
    privilege  text;
BEGIN
    IF days_ahead IS NULL OR days_ahead < 0 OR days_ahead > 366 THEN
        RAISE EXCEPTION 'ensure_partitions: days_ahead вне 0…366 (%)', days_ahead;
    END IF;
    FOR policy IN SELECT table_name, revoke_from_app_rw FROM partition_policies ORDER BY 1 LOOP
        FOR day IN SELECT generate_series(current_date, current_date + days_ahead, '1 day')::date LOOP
            part_name := format('%s_p%s', policy.table_name, to_char(day, 'YYYYMMDD'));
            IF to_regclass(format('control_plane.%I', part_name)) IS NOT NULL THEN
                CONTINUE;
            END IF;
            -- IF NOT EXISTS: два одновременных вызова не роняют друг друга (гонка после проверки).
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS control_plane.%I PARTITION OF control_plane.%I '
                'FOR VALUES FROM (%L) TO (%L)',
                part_name, policy.table_name, day, day + 1);
            FOREACH privilege IN ARRAY policy.revoke_from_app_rw LOOP
                EXECUTE format('REVOKE %s ON control_plane.%I FROM app_rw', privilege, part_name);
            END LOOP;
            created := created + 1;
        END LOOP;
    END LOOP;
    RETURN created;
END
$$;

-- Отсоединение и удаление партиций, чей интервал целиком старше срока хранения (§4.5):
-- сутки partition_day уходят, когда partition_day + 1 <= current_date - retention_days, то есть
-- ровно retention_days последних полных суток остаются. Возвращает число удалённых. Партиции
-- распознаются по имени <таблица>_pYYYYMMDD; timezone закреплён в UTC, как у ensure_partitions.
CREATE FUNCTION drop_expired_partitions() RETURNS int
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = control_plane, pg_temp
    SET timezone = 'UTC' AS $$
DECLARE
    policy    record;
    part      record;
    dropped   int := 0;
    part_day  date;
BEGIN
    FOR policy IN SELECT table_name, retention_days FROM partition_policies ORDER BY 1 LOOP
        FOR part IN
            SELECT c.relname
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            WHERE i.inhparent = format('control_plane.%I', policy.table_name)::regclass
              AND c.relname ~ ('^' || policy.table_name || '_p[0-9]{8}$')
            ORDER BY c.relname
        LOOP
            part_day := to_date(right(part.relname, 8), 'YYYYMMDD');
            IF part_day + 1 <= current_date - policy.retention_days THEN
                EXECUTE format('ALTER TABLE control_plane.%I DETACH PARTITION control_plane.%I',
                               policy.table_name, part.relname);
                EXECUTE format('DROP TABLE control_plane.%I', part.relname);
                dropped := dropped + 1;
            END IF;
        END LOOP;
    END LOOP;
    RETURN dropped;
END
$$;

-- Вызывать функции обслуживания может только планировщик (роль приложения); PUBLIC — нет (0001).
GRANT EXECUTE ON FUNCTION ensure_partitions(int), drop_expired_partitions() TO app_rw;
REVOKE EXECUTE ON FUNCTION traffic_hourly_only_grows() FROM app_rw;
