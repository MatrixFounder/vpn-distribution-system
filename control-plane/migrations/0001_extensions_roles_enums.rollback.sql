-- Откат 0001: перечисления, привилегии по умолчанию, расширения. Роли остаются: они кластерные
-- и созданы bootstrap-скриптом, а не этой миграцией.

SET LOCAL ROLE app_owner;

DROP TYPE actor_type;
DROP TYPE job_status;
DROP TYPE job_queue;
DROP TYPE delivery_status;
DROP TYPE event_type;
DROP TYPE reconciliation_kind;
DROP TYPE report_status;
DROP TYPE code_kind;
DROP TYPE balance_source;
DROP TYPE period_source;
DROP TYPE subscription_state;
DROP TYPE command_status;
DROP TYPE command_type;
DROP TYPE user_node_state;
DROP TYPE node_status;
DROP TYPE inbound_profile;
DROP TYPE plan_status;
DROP TYPE auth_event_kind;
DROP TYPE email_token_kind;
DROP TYPE admin_status;
DROP TYPE admin_role;
DROP TYPE user_status;

ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
    REVOKE SELECT ON TABLES FROM app_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
    REVOKE EXECUTE ON FUNCTIONS FROM app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
    REVOKE USAGE, SELECT ON SEQUENCES FROM app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
    REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM app_rw;
-- Возврат умолчания PostgreSQL (EXECUTE у PUBLIC): глобальная запись pg_default_acl исчезает.
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner
    GRANT EXECUTE ON FUNCTIONS TO PUBLIC;

DROP EXTENSION citext;
DROP EXTENSION btree_gist;
