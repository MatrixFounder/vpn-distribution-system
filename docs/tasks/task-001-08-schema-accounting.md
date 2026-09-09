# Задача 001.08: Схема: учёт трафика, гранты, адреса, функции обслуживания партиций

Тип задачи: `[CONFIGURATION]`.

## Связь со сценариями
- UC-04 Учёт трафика и списание с коэффициентом
- UC-05 Исчерпание лимита и отзыв доступа
- UC-08 Ограничение числа адресов и перепродажа доступа

Требования RTM: R-01, R-19, R-20, R-21, R-22, R-26, R-27.

<!-- contract:goal -->

## Цель задачи

Создать таблицы, ограничения и индексы по `docs/architectures/data-model.md` для группы «схема: учёт
трафика, гранты, адреса, функции обслуживания партиций» так, чтобы миграция применялась и
откатывалась без ошибок.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/migrations/080_schema_accounting.sql` — таблицы: `traffic_reports`, `traffic_lines`, `traffic_hourly`, `traffic_daily`, `node_interface_hourly`, `traffic_gaps`, `reconciliation_runs`, `quota_grants`, `user_online_ips`, `user_blocked_ips`
- `control-plane/migrations/080_schema_accounting.rollback.sql` — откат

### Интеграция компонентов

Таблицы создаются ролью `app_migrate`; права `app_rw` выдаются в той же миграции по перечню §4.6
(умолчания привилегий `app_owner` из 0001; особые запреты — `REVOKE UPDATE, DELETE ON traffic_lines`,
`REVOKE DELETE ON traffic_hourly`, реестр `partition_policies` только чтение).

Уточнения при реализации. Сверх §4.2.5:

- реестр `partition_policies (table_name PK CHECK '^[a-z_]+$', retention_days CHECK > 0,
  revoke_from_app_rw text[] CHECK ⊆ {SELECT, INSERT, UPDATE, DELETE})` со сроками §4.5 для пяти
  партиционированных таблиц (`auth_events` 90, `subscription_access_log` 30, `node_metrics` 30,
  `traffic_lines` 14, `traffic_hourly` 90) и привилегиями, снимаемыми с каждой новой партиции
  (§4.6); ведётся миграциями;
- `ensure_partitions(days_ahead int DEFAULT 7) RETURNS int` — суточные партиции `<таблица>_pYYYYMMDD`
  от текущих суток на `days_ahead` вперёд (`CREATE TABLE IF NOT EXISTS`: параллельные вызовы не
  мешают друг другу), возвращает число созданных — при гонке двух вызовов проигравший
  засчитывает партицию победителя, `days_ahead` NULL или вне 0…366 — ошибка;
  `drop_expired_partitions() RETURNS int` — отсоединяет и удаляет партиции, чей интервал целиком
  старше срока хранения: сутки `d` уходят, когда `d + 1 <= current_date - retention_days`, то есть
  остаются ровно `retention_days` последних полных суток. Сутки обеих функций — UTC (R-52): `SET
  timezone = 'UTC'` рядом с `SET search_path = control_plane, pg_temp`, timezone сессии
  вызывающего на границы партиций и срок хранения не влияет. Обе `SECURITY DEFINER`, владелец
  `app_owner`, `EXECUTE` только у `app_rw`;
- триггер `traffic_hourly_only_grows` BEFORE UPDATE — правило §4.4 «значения строки часа только
  увеличиваются» (ошибка класса `check_violation`); триггерная функция без EXECUTE у `app_rw`;
- `CHECK` неотрицательности байтов во всех таблицах учёта, `CHECK (period_end > period_start)` в
  `traffic_reports`/`traffic_lines`, `CHECK (gap_end > gap_start)`, `CHECK` коэффициента в
  `traffic_lines`/`traffic_hourly` (R-23), `CHECK (estimated_bytes >= 0)`;
- умолчания: `traffic_reports.received_at now()`, `reconciliation_runs.scope '{}'`, `created_at now()`,
  `quota_grants.consumed_bytes 0`, `issued_at now()`, `user_online_ips.last_seen now()`,
  `user_blocked_ips.blocked_since now()`;
- индексы сверх §4.4: `traffic_gaps (node_id, gap_start)`, `reconciliation_runs (kind, created_at)`;
- FK только там, где их ставит модель: `traffic_reports.node_id`, `traffic_gaps.node_id`,
  `quota_grants.{user_id,node_id,period_id}`; `traffic_lines`, `traffic_hourly`, `traffic_daily`,
  `node_interface_hourly`, `user_online_ips`, `user_blocked_ips` — без FK (§4.3, агрегаты
  переживают удаление аккаунта); NO ACTION у всех FK;
- откат удаляет только объекты этой миграции; партиции таблиц других миграций (`auth_events`,
  `subscription_access_log`, `node_metrics`), созданные `ensure_partitions`, остаются
  присоединёнными вместе с данными — исчезает только их обслуживание;
- сверх задачи: `python -m app.cli migrate --break-lock` снимает замок yoyo, оставшийся от
  клиента, умершего посреди миграции (обнаружено при ревью: после такого замка `migrate` падает
  по таймауту); без флага `migrate` завершается кодом 1 с подсказкой.

Модуль TC-UNIT-01 — `tests/unit/db/test_schema_accounting.py` (команда `-k 'schema_accounting'`), права —
`has_table_privilege`/`has_function_privilege`.

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
   - Проверяемая функция: `tests/unit/db/test_schema_accounting.py::test_app_rw_privileges`
   - Входные данные: `has_table_privilege` для `app_rw`, `app_backup` (каталог `information_schema` из сессии `app_rw` чужих прав не показывает)
   - Ожидаемый результат: права совпадают с перечнем §4.6

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/unit/db -k 'schema_accounting'`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Все таблицы группы существуют с типами и ограничениями §4.2
- [ ] PK `traffic_reports (node_id, counter_epoch, report_seq)`
- [ ] PK `traffic_hourly` включает `billing_group_id` и `multiplier_milli`
- [ ] Функции `SECURITY DEFINER` `ensure_partitions(days_ahead int)` и `drop_expired_partitions()` принадлежат `app_owner`
- [ ] `app_rw` имеет `UPDATE` на `traffic_hourly` и не имеет на `traffic_lines`
- [ ] Откат удаляет только объекты этой миграции

## Примечания

Миграция — конфигурационная задача без пары stub/logic. Партиционированные таблицы получают функцию
создания партиций в 001.08.

Зависимости: 001.06, 001.07. Приоритет: Critical. Оценка: 4 ч. Этап: 1 — схема данных и каркас
Control Plane.
