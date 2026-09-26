-- 131: отметка аннулирования bootstrap-токена (задача 001.25; постановка §4.5 «Повторная выдача»,
-- UC-01 A1, UC-12; data-model.md §4.2.3 bootstrap_tokens). Повторная выдача и отзыв identity
-- аннулируют неиспользованный токен ноды. До этой миграции аннулирование укорачивало срок
-- (expires_at := момент операции), и обмен сравнивал срок с часами базы: часы, шагнувшие назад
-- (NTP, возобновление VM), вернули бы аннулированному токену годность — identity после отзыва
-- (роаст 001.25, раунд 2). Отметка решает без часов, как used_at у погашения и revoked_at у
-- отзыва identity; expires_at остаётся только сроком жизни Н-24.
-- CHECK: токен либо погашен, либо аннулирован, но не то и другое — погашение аннулированного
-- (путь, забывший проверку отметки) база отвергнет, а не выпустит по нему identity.
-- issue_seq: порядок выдачи без часов. «Последний токен» ноды (состояние на шаге 6 UC-01) —
-- наибольший номер: последовательность не шагает назад, а выдачи одной ноды идут под блокировкой
-- её строки, и номер позже выданного больше. uuidv7 в id растёт с часами базы и после их шага
-- назад упорядочил бы токены неверно (роаст 001.25, раунд 4). Строки, которые уже есть в таблице
-- (первое применение и повторное после отката), нумеруются явно — порядком id, лучшим из
-- сохранившегося порядка выдачи: столбец identity, добавленный к таблице с данными, нумеровал бы
-- их физическим порядком строк, а UPDATE (откат 131 сводит срок аннулированных) переносит строку в
-- конец — прежний аннулированный токен снова оказался бы «последним» (роаст 001.25, раунд 6).
-- Затем последовательность ставится за наибольшим номером.
-- depends: 060_schema_nodes 130_node_identity_enrolled_from

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

ALTER TABLE bootstrap_tokens ADD COLUMN annulled_at timestamptz;
ALTER TABLE bootstrap_tokens
    ADD CONSTRAINT bootstrap_tokens_used_or_annulled_check
    CHECK (used_at IS NULL OR annulled_at IS NULL);
ALTER TABLE bootstrap_tokens ADD COLUMN issue_seq bigint;
UPDATE bootstrap_tokens AS t SET issue_seq = numbered.seq
FROM (SELECT id, row_number() OVER (ORDER BY id) AS seq FROM bootstrap_tokens) AS numbered
WHERE t.id = numbered.id;
ALTER TABLE bootstrap_tokens ALTER COLUMN issue_seq SET NOT NULL;
ALTER TABLE bootstrap_tokens ALTER COLUMN issue_seq ADD GENERATED ALWAYS AS IDENTITY;
SELECT setval(
    pg_get_serial_sequence('bootstrap_tokens', 'issue_seq'),
    (SELECT coalesce(max(issue_seq), 0) + 1 FROM bootstrap_tokens),
    false
);
