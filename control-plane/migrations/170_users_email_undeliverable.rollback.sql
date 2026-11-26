-- Откат 170: пометка недоставляемого адреса снимается вместе с колонкой; повторное применение
-- заводит колонку пустой (все адреса — доставляемые до первого отказа, 001.78).

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

ALTER TABLE users DROP COLUMN email_undeliverable_at;
