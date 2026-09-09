-- Откат 080: только объекты этой миграции (реестр, функции обслуживания, таблицы группы с их
-- партициями и триггером). Партиции таблиц других миграций, созданные ensure_partitions
-- (auth_events, subscription_access_log, node_metrics), остаются присоединёнными и продолжают
-- принимать строки — исчезает только обслуживание; их данные откат не трогает.

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

DROP FUNCTION drop_expired_partitions();
DROP FUNCTION ensure_partitions(int);
DROP TABLE partition_policies;
DROP TABLE user_blocked_ips;
DROP TABLE user_online_ips;
DROP TABLE quota_grants;
DROP TABLE reconciliation_runs;
DROP TABLE traffic_gaps;
DROP TABLE node_interface_hourly;
DROP TABLE traffic_daily;
DROP TABLE traffic_hourly;            -- вместе с триггером traffic_hourly_only_grows
DROP FUNCTION traffic_hourly_only_grows();
DROP TABLE traffic_lines;
DROP TABLE traffic_reports;
