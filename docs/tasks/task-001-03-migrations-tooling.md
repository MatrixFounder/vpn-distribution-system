# Задача 001.03: Инструмент миграций, расширения, роли и перечисления базы

Тип задачи: `[CONFIGURATION]`.

## Связь со сценариями
- UC-10 Восстановление Control Plane из резервной копии

Требования RTM: R-42, R-44.

<!-- contract:goal -->

## Цель задачи

Настроить `yoyo-migrations` и применить первую миграцию: расширения, роли базы, перечисления по
`docs/architectures/data-model.md` §4.2 и §4.6.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/migrations/0001_extensions_roles_enums.sql` — `CREATE EXTENSION btree_gist, citext`; привилегии ролей `app_rw`, `app_backup` на объекты `app_owner`; все `enum`-типы §4.2. Сами роли — кластерные объекты, и миграция под `app_migrate` создать их не может (`app_migrate` не существует до них): они создаются `control-plane/migrations/bootstrap/roles.sql` при инициализации кластера (`deploy/compose/postgres/initdb.d/10-roles.sh`) или в CI тем же SQL под суперпользователем; откат миграции роли не удаляет (уточнено при реализации)
- `control-plane/migrations/0001_extensions_roles_enums.rollback.sql` — откат первой миграции
- `control-plane/app/cli.py` — команда `migrate` — запуск yoyo под ролью `app_migrate`; команда `admin create` (заглушка до 001.47)
- `control-plane/yoyo.ini` — источник миграций и подключение из переменной `MIGRATE_DSN`

### Изменения в существующих файлах

#### Файл: `control-plane/Dockerfile`

- точка входа (`docker-entrypoint.sh`) выполняет `python -m app.cli migrate` при `APP_ROLE=api` до старта uvicorn (§10.2); в образ добавлены `migrations/` и `yoyo.ini`

### Интеграция компонентов

Все последующие миграции добавляются в `control-plane/migrations/NNNN_*.sql` парами apply/rollback
по правилу expand/contract (§17.2 источника).

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Миграция применяется и откатывается
   - Входные данные: чистая база; `yoyo apply`, затем `yoyo rollback`
   - Ожидаемый результат: обе команды завершаются с кодом 0; после отката перечислений и расширений нет (роли — объекты bootstrap, остаются; уточнено при реализации)

### Модульные тесты

- Не требуются: задача не содержит логики сверх заглушек или конфигурации.

### Регрессионные тесты

- Команда: `cd control-plane && yoyo apply --batch && yoyo rollback --batch --all && yoyo apply --batch`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Расширения `btree_gist` и `citext` установлены
- [ ] Четыре роли существуют; `app_rw` не является владельцем объектов
- [ ] Перечисления §4.2 созданы

## Примечания

Данных для миграции нет (Д-10).

Зависимости: 001.02. Приоритет: Critical. Оценка: 3 ч. Этап: 0 — репозиторий и стенд.
