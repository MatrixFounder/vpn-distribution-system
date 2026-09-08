# Задача 001.06: Схема: парк нод, inbound, состояние и команды

Тип задачи: `[CONFIGURATION]`.

## Связь со сценариями
- UC-01 Ввод ноды в эксплуатацию
- UC-07 Отказ и возврат ноды
- UC-11 Обновление парка нод
- UC-12 Компрометация ноды

Требования RTM: R-01, R-02, R-03, R-04, R-05, R-06, R-07.

<!-- contract:goal -->

## Цель задачи

Создать таблицы, ограничения и индексы по `docs/architectures/data-model.md` для группы «схема: парк
нод, inbound, состояние и команды» так, чтобы миграция применялась и откатывалась без ошибок.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/migrations/060_schema_nodes.sql` — таблицы: `nodes`, `node_ip_history`, `node_access_groups`, `bootstrap_tokens`, `node_identities`, `inbounds`, `inbound_secrets`, `node_config_versions`, `node_user_credentials`, `node_user_state`, `commands`, `node_metrics`, `node_country_availability`
- `control-plane/migrations/060_schema_nodes.rollback.sql` — откат

### Интеграция компонентов

Миграция 050 (001.05) создала `node_billing_assignments.node_id` без внешнего ключа: `nodes` тогда не
существовало. Миграция 060 добавляет `ALTER TABLE node_billing_assignments ADD CONSTRAINT
node_billing_assignments_node_id_fkey FOREIGN KEY (node_id) REFERENCES nodes (id)`, её откат снимает
это ограничение до `DROP TABLE nodes`; ожидание в `tests/unit/db/test_schema_catalog.py` дополняется.

Таблицы создаются ролью `app_migrate`; права `app_rw` выдаются в той же миграции по перечню §4.6
(умолчания привилегий `app_owner` из 0001).

Уточнения при реализации. Сверх §4.2.3:

- умолчания: `nodes.status 'pending'`, `missed_heartbeats 0`, `ok_heartbeats 0`,
  `resync_required false`, `desired_config_version 0`, `applied_config_version 0`,
  `desired_users_seq 0`, `applied_users_seq 0`, `legal_profile '{}'`; `inbounds.enabled true`,
  `reality_short_ids '{}'`, `reality_server_names '{}'`, `reality_xver 0`, `reality_limit_fb_up 0`,
  `reality_limit_fb_down 0`, `client_spider_x ''`, `trusted_x_forwarded_for '{}'`, `params '{}'`;
  `inbound_secrets.key_version 1`; `node_user_credentials.version 1`; `node_user_state.quota_grant_bytes 0`,
  `blocked_ips '{}'`; `commands.payload '{}'`, `status 'issued'`; `now()` для
  `nodes.status_changed_at`, `nodes.created_at`, `node_identities.issued_at`,
  `inbound_secrets.rotated_at`, `node_config_versions.created_at`, `node_user_credentials.rotated_at`,
  `node_user_state.updated_at`, `commands.issued_at`, `node_country_availability.checked_at`;
- `CHECK (bandwidth_mbps > 0)`, `CHECK (max_conn_per_ip > 0)`;
- `node_ip_history`: `valid_to` допускает NULL (действующий адрес — открытый интервал; модель
  пометки `NULL` здесь не ставит), `CHECK (valid_to IS NULL OR valid_to > valid_from)` как у историй
  050, `id` — `GENERATED ALWAYS AS IDENTITY`;
- индексы по FK сверх §4.4: `node_ip_history (node_id)`, `bootstrap_tokens (node_id)`
  (`node_access_groups (access_group_id)` — из модели);
- каскады только у `node_access_groups` (состав групп) и `inbound_secrets` (секрет без inbound
  бессмыслен); остальные связи с `nodes` — без действия при удалении (NO ACTION, умолчание
  PostgreSQL): нода выводится через `decommissioned_at`, не удалением;
- `node_metrics` без FK на `nodes` (партиции, §4.3).

Модуль TC-UNIT-01 — `tests/unit/db/test_schema_nodes.py` (команда `-k 'schema_nodes'`), права —
`has_table_privilege`/`has_sequence_privilege`; ожидание FK в `test_schema_catalog.py` дополнено.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Миграция применяется и откатывается
   - Входные данные: база после предыдущих миграций
   - Ожидаемый результат: `yoyo apply` и `yoyo rollback` завершаются с кодом 0
2. **TC-E2E-02:** Ограничения действуют
   - Входные данные: вставка строк, нарушающих `UNIQUE`, `CHECK`, `EXCLUDE` из §4.4
   - Ожидаемый результат: каждая вставка отклонена с ошибкой ограничения

### Модульные тесты

1. **TC-UNIT-01:** Проверка прав роли `app_rw`
   - Проверяемая функция: `tests/unit/db/test_schema_nodes.py::test_app_rw_privileges` (соглашение `test_schema_<группа>.py`, как в 001.04/001.05)
   - Входные данные: `has_table_privilege` для `app_rw`, `app_backup` (каталог `information_schema` из сессии `app_rw` чужих прав не показывает)
   - Ожидаемый результат: права совпадают с перечнем §4.6

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/unit/db -k 'schema_nodes'`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Все таблицы группы существуют с типами и ограничениями §4.2
- [ ] `nodes.billing_group_id NOT NULL`
- [ ] `UNIQUE (node_id, profile)`, `UNIQUE (node_id, port)` на `inbounds`
- [ ] `node_metrics` партиционирована по суткам
- [ ] Откат удаляет только объекты этой миграции

## Примечания

Миграция — конфигурационная задача без пары stub/logic. Партиционированные таблицы получают функцию
создания партиций в 001.08.

Зависимости: 001.05. Приоритет: Critical. Оценка: 4 ч. Этап: 1 — схема данных и каркас Control
Plane.
