-- 100: настройки регистрации (задача 001.14; data-model.md §4.2.6 settings; постановка §4.1,
-- §16.8): режим регистрации (open | invite | closed), список одноразовых доменов почты, CAPTCHA.
-- Значения — jsonb; строки создаются только если ключа ещё нет (ON CONFLICT DO NOTHING):
-- настройки, изменённые администратором, миграция не перезаписывает. Откат удаляет ровно эти
-- три ключа.
-- depends: 090_schema_ops

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

INSERT INTO settings (key, value) VALUES
    ('registration_mode', '"open"'),
    ('disposable_email_domains', '[]'),
    ('captcha', '{"enabled": false}')
ON CONFLICT (key) DO NOTHING;
