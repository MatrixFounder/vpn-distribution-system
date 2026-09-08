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

Таблицы создаются ролью `app_migrate`; права `app_rw` выдаются в той же миграции по перечню §4.6.

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
   - Проверяемая функция: `tests/unit/db/test_grants.py::test_app_rw_privileges`
   - Входные данные: каталог `information_schema.role_table_grants`
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
