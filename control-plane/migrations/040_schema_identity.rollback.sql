-- Откат 040: только объекты этой миграции, в порядке зависимостей. Индексы и последовательность
-- auth_events_id_seq (OWNED BY) удаляются вместе с таблицами; типы из 0001 остаются.

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

DROP TABLE auth_events;
DROP TABLE email_tokens;
DROP TABLE admin_recovery_codes;
DROP TABLE admin_users;
DROP TABLE users;
