-- Откат 150: версия раздела agent у nodes. Раздел возвращается к версии заглушки 1 у всех нод.
-- Повторное применение вычисляет начальные значения заново (1 при max_conn_per_ip 32, иначе 2).
-- Агент с курсором выше пересчитанного получит 409 cursor_ahead и возьмёт снапшот (контракт
-- agent_v1). Агент, чей курсор совпал с пересчитанным, а содержимое старше (поздняя задача повысила
-- версию после того, как он применил прежнюю), раздела не получит до следующего повышения версии
-- его ноды.

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

ALTER TABLE nodes DROP CONSTRAINT nodes_agent_section_version_check;
ALTER TABLE nodes DROP COLUMN agent_section_version;
