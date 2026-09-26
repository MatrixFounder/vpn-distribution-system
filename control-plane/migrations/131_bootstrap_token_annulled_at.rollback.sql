-- Откат 131: отметка аннулирования и порядковый номер выдачи bootstrap-токена.
-- Аннулированный токен без отметки ожил бы: срок жизни (expires_at) аннулирование не трогает, и
-- повторное применение вернуло бы столбец пустым — токен, аннулированный отзывом identity, снова
-- годен до конца часа (роаст 001.25, раунд 4). Поэтому до удаления отметки срок таких токенов
-- сводится к моменту аннулирования — отказ держится сроком, как до миграции.

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

ALTER TABLE bootstrap_tokens DROP COLUMN issue_seq;
UPDATE bootstrap_tokens SET expires_at = LEAST(expires_at, annulled_at) WHERE annulled_at IS NOT NULL;
ALTER TABLE bootstrap_tokens DROP CONSTRAINT bootstrap_tokens_used_or_annulled_check;
ALTER TABLE bootstrap_tokens DROP COLUMN annulled_at;
