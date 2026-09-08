-- Откат 060: только объекты этой миграции, в порядке зависимостей; внешний ключ истории
-- назначений (050) снимается до удаления nodes, сама таблица node_billing_assignments остаётся.

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

ALTER TABLE node_billing_assignments DROP CONSTRAINT node_billing_assignments_node_id_fkey;

DROP TABLE node_country_availability;
DROP TABLE node_metrics;
DROP TABLE commands;
DROP TABLE node_user_state;
DROP TABLE node_user_credentials;
DROP TABLE node_config_versions;
DROP TABLE inbound_secrets;
DROP TABLE inbounds;
DROP TABLE node_identities;
DROP TABLE bootstrap_tokens;
DROP TABLE node_access_groups;
DROP TABLE node_ip_history;
DROP TABLE nodes;
