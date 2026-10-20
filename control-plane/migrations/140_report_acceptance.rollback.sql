-- Откат 140: состояние интервала, правила частей, граница полосы ноды.
-- Значение перечисления PostgreSQL не удаляет — тип пересоздаётся без interval_closed. Приведение
-- через текст падает на строке с этим статусом, и откат останавливается, пока такие строки есть:
-- удалить отказанные части или переклассифицировать их решает оператор, а не откат.

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

ALTER TABLE nodes DROP CONSTRAINT nodes_bandwidth_mbps_check;
ALTER TABLE nodes ADD CONSTRAINT nodes_bandwidth_mbps_check CHECK (bandwidth_mbps > 0);

DROP INDEX traffic_lines_interval_user_key;
DROP TABLE report_intervals;

ALTER TABLE traffic_reports DROP CONSTRAINT traffic_reports_report_seq_check;
ALTER TABLE traffic_reports DROP COLUMN parts_total;

ALTER TYPE report_status RENAME TO report_status_140;
CREATE TYPE report_status AS ENUM ('accepted', 'duplicate', 'rejected_time', 'held_anomaly');
ALTER TABLE traffic_reports
    ALTER COLUMN status TYPE report_status USING status::text::report_status;
DROP TYPE report_status_140;
