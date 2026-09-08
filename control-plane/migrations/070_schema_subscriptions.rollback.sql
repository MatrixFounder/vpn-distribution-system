-- Откат 070: только объекты этой миграции, в порядке зависимостей; последовательность
-- subscription_access_log_id_seq удаляется вместе с таблицей (OWNED BY), identity — с balance_entries.

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

DROP TABLE payments;
DROP TABLE orders;
DROP TABLE code_redemptions;
DROP TABLE codes;
DROP TABLE subscription_access_log;
DROP TABLE subscription_tokens;
DROP TABLE balance_entries;
DROP TABLE subscriptions;
DROP TABLE subscription_periods;
