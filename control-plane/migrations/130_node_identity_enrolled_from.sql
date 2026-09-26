-- 130: адрес источника enrollment у identity ноды (задача 001.25; постановка §4.5 «Подтверждение
-- ноды», UC-01 шаг 6, R-02; data-model.md §4.2.3 node_identities). Администратор до подтверждения
-- сверяет адрес, с которого нода обменяла bootstrap-токен; адрес — свойство обмена, то есть
-- identity, а не ноды: у пересозданной ноды он свой, и прежний остаётся в её истории.
-- NOT NULL без умолчания: до 001.25 enrollment в базу не писал (строк нет ни на одном стенде), а
-- выдуманного адреса для строк, если бы они были, нет — миграция на непустой таблице падает
-- громко, а не подставляет значение, которое администратор принял бы за сверенное.
-- UNIQUE (node_id, generation): поколение identity ноды (§5.3) выдаётся как max + 1 под
-- блокировкой строки ноды; ограничение держит то же без опоры на дисциплину блокировок — её
-- повторяют обмен (001.25) и ротация (001.31), и ошибка любого из них дала бы два поколения
-- с одним номером.
-- cert_serial: серийный номер листа в форме $ssl_client_serial nginx (hex верхнего регистра, два
-- знака на байт). Отпечаток SHA-256 — ключ поиска identity, но отказ отозванному листу на прокси
-- (CRL или карта отказа, 001.66) знает только серийный номер, а сам лист база не хранит: без этой
-- колонки листы, выпущенные до 001.66, не попали бы ни в один отказ (роаст 001.25, раунд 2).
-- Номер выбирает CA (159 случайных бит) — UNIQUE держит то же, что и у отпечатка.
-- depends: 060_schema_nodes 120_settings_email_tokens

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

ALTER TABLE node_identities ADD COLUMN enrolled_from inet NOT NULL;
ALTER TABLE node_identities
    ADD CONSTRAINT node_identities_node_id_generation_key UNIQUE (node_id, generation);
ALTER TABLE node_identities ADD COLUMN cert_serial text NOT NULL;
ALTER TABLE node_identities
    ADD CONSTRAINT node_identities_cert_serial_key UNIQUE (cert_serial);
