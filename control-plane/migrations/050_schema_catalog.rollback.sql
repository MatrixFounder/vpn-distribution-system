-- Откат 050: только объекты этой миграции, в порядке зависимостей (FK на nodes, добавленный
-- миграцией 060, к этому моменту уже снят её откатом).

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

DROP TABLE node_billing_assignments;
DROP TABLE billing_group_multipliers;
DROP TABLE billing_groups;
DROP TABLE plan_access_groups;
DROP TABLE access_groups;
DROP TABLE plan_protocols;
DROP TABLE plans;
