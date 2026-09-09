-- 120: срок действия ссылок подтверждения адреса и восстановления пароля (ОВ-25 — решение
-- владельца 2026-09-09: значение в settings, по умолчанию 15 минут; data-model.md §4.2.6 settings;
-- постановка §4.1). Ключ email_token_ttl_minutes — целое число минут, общее для обоих видов
-- ссылок. Строка создаётся только если ключа ещё нет (ON CONFLICT DO NOTHING); откат удаляет ключ.
-- depends: 110_jobs_retry

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

INSERT INTO settings (key, value) VALUES ('email_token_ttl_minutes', '15')
ON CONFLICT (key) DO NOTHING;
