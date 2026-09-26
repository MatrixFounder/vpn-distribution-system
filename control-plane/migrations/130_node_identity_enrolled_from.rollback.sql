-- Откат 130: серийный номер листа, уникальность поколения и колонка адреса источника enrollment.
-- Только на пустой node_identities: у выданных identity откат молча стёр бы серийные номера листов
-- (по ним отказ на прокси, 001.66) и адреса обмена, а повторное применение 130 упало бы на
-- NOT NULL и оставило базу ниже 130 при работающем api (роаст 001.25, раунд 8). Непустая таблица —
-- отказ отката со схемой на месте; identity снимают до отката (или восстанавливают базу из копии).

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

-- Проверка — под блокировкой таблицы, которую откат всё равно возьмёт для DROP COLUMN: без неё
-- обмен, вставивший identity, но не зафиксировавший её к моменту проверки, проверку миновал бы,
-- DROP дождался бы его фиксации и стёр бы серийный номер уже выданного листа (роаст 001.25,
-- раунд 9). Блокировка ждёт незавершённые вставки и не пускает новые до конца отката.
LOCK TABLE node_identities IN ACCESS EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM node_identities) THEN
        RAISE EXCEPTION 'откат 130 на непустой node_identities: серийные номера листов и адреса '
            'обмена были бы потеряны, а повторное применение 130 не прошло бы NOT NULL';
    END IF;
END
$$;

ALTER TABLE node_identities DROP CONSTRAINT node_identities_cert_serial_key;
ALTER TABLE node_identities DROP COLUMN cert_serial;
ALTER TABLE node_identities DROP CONSTRAINT node_identities_node_id_generation_key;
ALTER TABLE node_identities DROP COLUMN enrolled_from;
