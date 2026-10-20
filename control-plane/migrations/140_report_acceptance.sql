-- 140: приём отчёта о трафике — состояние интервала и правила частей (задача 001.34; постановка
-- §5.9; data-model.md §4.2.5 traffic_reports, report_intervals, traffic_lines; §4.2.3 nodes;
-- contracts/agent_v1/README.md «Отчёт о трафике»; UC-04 шаги 5, 6, 9).
-- report_status: значение interval_closed — часть к закрытому интервалу или вне диапазона
-- открытого записывается с ним (§4.2.5). ADD VALUE в транзакции миграции допустим (PostgreSQL
-- 12+); само значение эта миграция не использует.
-- traffic_reports: parts_total — объявленное нодой число частей интервала (контракт 001.33,
-- предел модели REPORT_MAX_PARTS = 1000); CHECK report_seq >= 1 — нумерация с единицы (0 в
-- ответе — «ничего не принято»), до 140 правило жило только на кромке API. Умолчание 1 —
-- только для строк, которые могли остаться от тестов: писателя таблицы до 001.34 не было.
-- report_intervals: состояние интервала между частями — номер первой принятой, последнее
-- объявленное число частей, счётчик принятых (не пересчёт строк), нарастающие суммы байтов по
-- направлениям (порог §5.9 — к их сумме: байт пользователя проходит порт дважды — решение
-- заказчика 2026-10-20), признак удержания, моменты приёма, закрытие и его
-- причина (capped — часть сверх потолка открытых интервалов: интервал рождается закрытым и
-- удержанным, без списания — решение заказчика 2026-10-20; так он держит монотонность и правила
-- частей, не занимая места среди открытых; CHECK держит capped удержанным и с нулевыми суммами —
-- роаст 001.34, раунд 3); индексы — последний интервал ноды (монотонность,
-- якорь), смены эпохи за сутки и возраст, открытые интервалы; EXCLUDE — интервалы ноды не
-- перекрываются.
-- traffic_lines: уникальность пользователя в интервале (period_start, node_id, counter_epoch,
-- period_end, user_id) — страховка правила «пользователь в одной части интервала», которое
-- приём проверяет по контракту (422) до вставки 001.77; PK таблицы держит report_seq и
-- межчастевой дубль не ловит. Ключ партиционирования period_start входит в индекс.
-- nodes.bandwidth_mbps: верхняя граница 100 000 Мбит/с (100 Гбит/с). Порог §5.9 — полоса ×
-- длительность на сумму uplink + downlink — держит границу строки и агрегатов в bigint только
-- при ограниченной полосе: при 10.0 и полном канале год одной ноды —
-- 10 × 100 000 × 125 000 × 31 536 000 ≈ 3,9·10¹⁸ байт ≤ 2⁶³ − 1 (передано из 001.23).
-- depends: 060_schema_nodes 080_schema_accounting

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

ALTER TYPE report_status ADD VALUE 'interval_closed';

ALTER TABLE traffic_reports
    ADD COLUMN parts_total int NOT NULL DEFAULT 1 CHECK (parts_total BETWEEN 1 AND 1000);
ALTER TABLE traffic_reports ALTER COLUMN parts_total DROP DEFAULT;
ALTER TABLE traffic_reports
    ADD CONSTRAINT traffic_reports_report_seq_check CHECK (report_seq >= 1);

CREATE TABLE report_intervals (
    node_id                  uuid         NOT NULL REFERENCES nodes (id),
    counter_epoch            uuid         NOT NULL,
    period_start             timestamptz  NOT NULL,
    period_end               timestamptz  NOT NULL,
    first_seq                bigint       NOT NULL CHECK (first_seq >= 1),
    parts_total              int          NOT NULL CHECK (parts_total BETWEEN 1 AND 1000),
    parts_accepted           int          NOT NULL,
    accepted_uplink_bytes    bigint       NOT NULL CHECK (accepted_uplink_bytes >= 0),
    accepted_downlink_bytes  bigint       NOT NULL CHECK (accepted_downlink_bytes >= 0),
    held                     boolean      NOT NULL DEFAULT false,
    first_received_at        timestamptz  NOT NULL,
    last_received_at         timestamptz  NOT NULL,
    closed_at                timestamptz,
    close_reason             text
        CHECK (close_reason IN ('completed', 'superseded', 'deadline', 'age', 'capped')),
    PRIMARY KEY (node_id, counter_epoch, period_start, period_end),
    CHECK (period_end > period_start),
    CHECK (parts_accepted BETWEEN 1 AND parts_total),
    CHECK (last_received_at >= first_received_at),
    CHECK ((closed_at IS NULL) = (close_reason IS NULL)),
    CHECK (close_reason IS DISTINCT FROM 'capped'
        OR (held AND accepted_uplink_bytes = 0 AND accepted_downlink_bytes = 0)),
    EXCLUDE USING gist (node_id WITH =, tstzrange(period_start, period_end) WITH &&)
);
CREATE INDEX report_intervals_node_id_period_end_idx
    ON report_intervals (node_id, period_end DESC);
CREATE INDEX report_intervals_node_id_first_received_at_idx
    ON report_intervals (node_id, first_received_at);
CREATE INDEX report_intervals_open_idx ON report_intervals (node_id) WHERE closed_at IS NULL;

CREATE UNIQUE INDEX traffic_lines_interval_user_key
    ON traffic_lines (period_start, node_id, counter_epoch, period_end, user_id);

ALTER TABLE nodes DROP CONSTRAINT nodes_bandwidth_mbps_check;
ALTER TABLE nodes
    ADD CONSTRAINT nodes_bandwidth_mbps_check CHECK (bandwidth_mbps BETWEEN 1 AND 100000);
