-- Откат 120: только ключ срока ссылок.

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

DELETE FROM settings WHERE key = 'email_token_ttl_minutes';
