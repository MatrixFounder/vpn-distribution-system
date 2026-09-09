-- Откат 090: только объекты этой миграции; триггеры audit_log уходят вместе с таблицей;
-- purge_audit_log() принадлежит app_audit_purge, но снимает её владелец схемы app_owner
-- (владелец схемы удаляет любой её объект — DROP FUNCTION без SET ROLE).

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

DROP TABLE settings;
DROP FUNCTION purge_audit_log();
DROP TABLE audit_log;
DROP FUNCTION audit_log_immutable();
DROP TABLE jobs;
DROP TABLE webhook_deliveries;
DROP TABLE email_deliveries;
DROP TABLE events;
