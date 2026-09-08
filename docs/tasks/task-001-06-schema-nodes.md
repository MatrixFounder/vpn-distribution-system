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
