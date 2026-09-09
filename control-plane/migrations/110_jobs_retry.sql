-- 110: очередь задач — времена выборки и завершения (задача 001.74; data-model.md §4.2.6 jobs;
-- interfaces.md §5.4). claimed_at — момент последней выборки (остаётся после завершения, в отличие
-- от locked_at: по разности claimed_at − run_at считается ожидание задачи для метрики
-- jobs_wait_seconds и бюджета Н-13); finished_at — момент done/failed/dead. Частичный индекс по
-- claimed_at — для окна метрики (done-строки живут 7 дней, §4.5). Расширение (expand): колонки
-- NULL, старые строки не трогаются; откат снимает индекс и обе колонки.
-- depends: 100_settings_auth

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

ALTER TABLE jobs
    ADD COLUMN claimed_at  timestamptz,
    ADD COLUMN finished_at timestamptz;
CREATE INDEX jobs_claimed_at_idx ON jobs (claimed_at) WHERE claimed_at IS NOT NULL;
