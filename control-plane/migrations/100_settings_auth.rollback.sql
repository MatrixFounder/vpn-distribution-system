-- Откат 100: только три ключа настроек этой миграции.

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

DELETE FROM settings WHERE key IN ('registration_mode', 'disposable_email_domains', 'captcha');
