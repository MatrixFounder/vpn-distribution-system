-- Откат 110: снять индекс и колонки времён очереди.

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

DROP INDEX IF EXISTS jobs_claimed_at_idx;
ALTER TABLE jobs
    DROP COLUMN claimed_at,
    DROP COLUMN finished_at;
