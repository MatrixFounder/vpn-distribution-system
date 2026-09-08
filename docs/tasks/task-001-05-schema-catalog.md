# Задача 001.05: Схема: тарифы, группы, коэффициенты

Тип задачи: `[CONFIGURATION]`.

## Связь со сценариями
- UC-09 Управление тарифами, группами и кодами

Требования RTM: R-01, R-18, R-23, R-30.

<!-- contract:goal -->

## Цель задачи

Создать таблицы, ограничения и индексы по `docs/architectures/data-model.md` для группы «схема:
тарифы, группы, коэффициенты» так, чтобы миграция применялась и откатывалась без ошибок.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/migrations/050_schema_catalog.sql` — таблицы: `plans`, `plan_protocols`, `access_groups`, `plan_access_groups`, `billing_groups`, `billing_group_multipliers`, `node_billing_assignments`
- `control-plane/migrations/050_schema_catalog.rollback.sql` — откат

### Интеграция компонентов

Таблицы создаются ролью `app_migrate`; права `app_rw` выдаются в той же миграции по перечню §4.6
(через умолчания привилегий `app_owner` из миграции 0001, как в 001.04).

Уточнения при реализации: `node_billing_assignments.node_id` без внешнего ключа — таблицы `nodes`
до 001.06 нет, FK добавляет миграция 060 (и снимает её откат). Сверх §4.2.2: умолчания `status`,
`description`, `created_at`/`updated_at`; `CHECK (valid_to IS NULL OR valid_to > valid_from)` на двух
таблицах истории (иначе `tstzrange` отвергал бы строку с ошибкой диапазона, а пустой интервал
проходил бы `EXCLUDE`); индексы обратного поиска `plan_access_groups (access_group_id)` и
`node_billing_assignments (billing_group_id)`; `ON DELETE CASCADE` на `plan_protocols.plan_id`,
`plan_access_groups.plan_id`, `plan_access_groups.access_group_id` (модель каскадов не задаёт:
удаление тарифа или группы доступа уносит строки состава; история коэффициентов — без каскада,
§4.5 «бессрочно»). Модуль TC-UNIT-01 — `tests/unit/db/test_schema_catalog.py`
(команда регрессии `-k 'schema_catalog'`), функция `test_app_rw_privileges`; проверка прав —
`has_table_privilege` (каталог `information_schema` из сессии `app_rw` чужих прав не видит).

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
   - Проверяемая функция: `tests/unit/db/test_schema_catalog.py::test_app_rw_privileges` (см. уточнения)
   - Входные данные: `has_table_privilege` для ролей `app_rw`, `app_backup` (каталог `information_schema.role_table_grants` из сессии `app_rw` чужих прав не показывает)
   - Ожидаемый результат: права совпадают с перечнем §4.6

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/unit/db -k 'schema_catalog'`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Все таблицы группы существуют с типами и ограничениями §4.2
- [ ] `EXCLUDE USING gist` на интервалах двух таблиц истории действует
- [ ] `CHECK` на `multiplier_milli`: 0…10000, кратно 100
- [ ] Откат удаляет только объекты этой миграции

## Примечания

Миграция — конфигурационная задача без пары stub/logic. Партиционированные таблицы получают функцию
создания партиций в 001.08.

Зависимости: 001.03. Приоритет: Critical. Оценка: 3 ч. Этап: 1 — схема данных и каркас Control
Plane.
