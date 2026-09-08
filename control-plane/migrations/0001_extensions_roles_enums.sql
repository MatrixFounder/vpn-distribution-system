-- 0001: расширения, привилегии ролей на уровне базы, перечисления §4.2 модели данных
-- (docs/architectures/data-model.md). Роли созданы заранее bootstrap-скриптом
-- migrations/bootstrap/roles.sql (кластерные объекты); здесь — только объекты базы.
-- Выполняется под app_migrate; владелец объектов — app_owner (SET LOCAL ROLE действует до конца
-- транзакции миграции, учёт yoyo ведётся уже от app_migrate).

SET LOCAL ROLE app_owner;

-- Расширения (§4.6): btree_gist — EXCLUDE по uuid и tstzrange; citext — адреса почты.
-- Обе отмечены trusted: устанавливаются владельцем базы без суперпользователя. Без IF NOT EXISTS:
-- чужое (не app_owner) расширение — ошибка здесь, а не при откате.
CREATE EXTENSION btree_gist;
CREATE EXTENSION citext;

-- Привилегии по умолчанию на будущие объекты app_owner: app_rw — DML, app_backup — чтение.
-- Запреты UPDATE/DELETE для audit_log, traffic_lines, balance_entries вводят их миграции (§4.6).
-- EXECUTE у PUBLIC отзывается: иначе функции SECURITY DEFINER app_owner (§4.6) вызывала бы
-- любая роль кластера, включая «только читающего» app_backup. Только глобальной записью
-- (без IN SCHEMA): схемные умолчания складываются со встроенными и отозвать их не могут.
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
    GRANT EXECUTE ON FUNCTIONS TO app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
    GRANT SELECT ON TABLES TO app_backup;

-- Перечисления §4.2 (значения только добавляются: ALTER TYPE ... ADD VALUE, §4.6).
-- event_type — по строкам таблицы «События» §4.17 постановки (docs/idea.md), по порядку:
--   subscription_activated      Подписка активирована
--   subscription_expiring       Подписка истекает
--   subscription_expired        Подписка истекла
--   traffic_80                  Израсходовано 80 % лимита трафика
--   traffic_95                  Израсходовано 95 % лимита трафика
--   traffic_exhausted           Лимит трафика исчерпан, доступ отозван
--   node_address_changed        Адрес ноды изменён
--   node_offline                Нода недоступна
--   node_recovered              Нода восстановлена
--   node_suspended_by_provider  Нода приостановлена провайдером
--   reconciliation_mismatch     Расхождение сверки трафика
--   node_report_buffer_full     Буфер отчётов на ноде заполнен
CREATE TYPE user_status AS ENUM ('active', 'blocked', 'deleted');  -- §4.2.1 users
CREATE TYPE admin_role AS ENUM ('super_admin', 'admin', 'operator', 'support');  -- §4.2.1 admin_users
CREATE TYPE admin_status AS ENUM ('active', 'blocked');  -- §4.2.1 admin_users
CREATE TYPE email_token_kind AS ENUM ('verify', 'reset');  -- §4.2.1 email_tokens
CREATE TYPE auth_event_kind AS ENUM ('register', 'login', 'logout', 'reset');  -- §4.2.1 auth_events
CREATE TYPE plan_status AS ENUM ('active', 'archived');  -- §4.2.2 plans
CREATE TYPE inbound_profile AS ENUM ('vless_raw_vision', 'vless_xhttp', 'trojan_reality');  -- §4.2.2 plan_protocols, §4.2.3 inbounds
CREATE TYPE node_status AS ENUM ('pending', 'provisioning', 'active', 'degraded', 'offline', 'maintenance', 'disabled', 'suspended');  -- §4.2.3 nodes
CREATE TYPE user_node_state AS ENUM ('active', 'suspended_quota', 'suspended_admin', 'expired', 'removed');  -- §4.2.3 node_user_state
CREATE TYPE command_type AS ENUM ('restart_xray', 'rotate_credentials', 'collect_diagnostics', 'update_agent');  -- §4.2.3 commands
CREATE TYPE command_status AS ENUM ('issued', 'delivered', 'applied', 'failed', 'expired');  -- §4.2.3 commands
CREATE TYPE subscription_state AS ENUM ('none', 'active', 'suspended_quota', 'suspended_admin', 'expired');  -- §4.2.4 subscriptions
CREATE TYPE period_source AS ENUM ('redeem', 'admin', 'order');  -- §4.2.4 subscription_periods
CREATE TYPE balance_source AS ENUM ('report', 'adjustment', 'bonus', 'late_report');  -- §4.2.4 balance_entries
CREATE TYPE code_kind AS ENUM ('redeem', 'promo');  -- §4.2.4 codes
CREATE TYPE report_status AS ENUM ('accepted', 'duplicate', 'rejected_time', 'held_anomaly');  -- §4.2.5 traffic_reports
CREATE TYPE reconciliation_kind AS ENUM ('arithmetic', 'cross_source', 'continuity');  -- §4.2.5 reconciliation_runs
CREATE TYPE event_type AS ENUM ('subscription_activated', 'subscription_expiring', 'subscription_expired', 'traffic_80', 'traffic_95', 'traffic_exhausted', 'node_address_changed', 'node_offline', 'node_recovered', 'node_suspended_by_provider', 'reconciliation_mismatch', 'node_report_buffer_full');  -- §4.2.6 events — 12 событий §4.17 постановки
CREATE TYPE delivery_status AS ENUM ('pending', 'sent', 'bounced', 'failed');  -- §4.2.6 email_deliveries, webhook_deliveries
CREATE TYPE job_queue AS ENUM ('critical', 'background');  -- §4.2.6 jobs
CREATE TYPE job_status AS ENUM ('pending', 'running', 'done', 'failed', 'dead');  -- §4.2.6 jobs
CREATE TYPE actor_type AS ENUM ('admin', 'user', 'system');  -- §4.2.6 audit_log
