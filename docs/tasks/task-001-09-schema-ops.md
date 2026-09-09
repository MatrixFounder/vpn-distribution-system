# Задача 001.09: Схема: события, доставки, очередь задач, аудит, настройки

Тип задачи: `[CONFIGURATION]`.

## Связь со сценариями
- UC-13 Поддержка пользователя
- UC-05 Исчерпание лимита и отзыв доступа

Требования RTM: R-01, R-37, R-39, R-40, R-46, R-48.

<!-- contract:goal -->

## Цель задачи

Создать таблицы, ограничения и индексы по `docs/architectures/data-model.md` для группы «схема:
события, доставки, очередь задач, аудит, настройки» так, чтобы миграция применялась и откатывалась
без ошибок.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/migrations/090_schema_ops.sql` — таблицы: `events`, `email_deliveries`, `webhook_deliveries`, `jobs`, `audit_log`, `settings`
- `control-plane/migrations/090_schema_ops.rollback.sql` — откат

### Интеграция компонентов

Таблицы создаются ролью `app_migrate`; права `app_rw` выдаются в той же миграции по перечню §4.6.

Уточнения при реализации. Сверх §4.2.6:

- умолчания: `uuidv7()` для uuid-PK, `now()` для `created_at`/`run_at`/`ts`/`updated_at`, `'{}'`
  для `payload`, `'pending'` для статусов доставок и задач, `0` для `attempts`; все колонки без
  пометки NULL в модели — `NOT NULL` (в том числе `events.dedup_key`, `jobs.idempotency_key`);
- `CHECK`: `attempts >= 0` (доставки, задачи), `max_attempts > 0`, `(locked_at IS NULL) =
  (locked_by IS NULL)` в `jobs`, `result IN ('success', 'denied', 'error')` в `audit_log` (§4.16);
- identity (`GENERATED ALWAYS`) для `jobs.id` и `audit_log.id`; последовательности получают права
  умолчаний (`app_rw` — USAGE, SELECT);
- FK доставок к `events` — `ON DELETE CASCADE` (доставка без события бессмысленна, срок хранения
  общий); индексы по FK `email_deliveries (event_id)`, `webhook_deliveries (event_id)`;
  `events.user_id`/`node_id`, `audit_log.actor_id`/`impersonated_user_id` — без FK (ссылки по
  идентификатору, B-3; записи переживают удаление аккаунта);
- частичные индексы `jobs`: `jobs_pending_idx (queue, status, run_at) WHERE status = 'pending'`,
  `jobs_idempotency_active_idx UNIQUE (idempotency_key) WHERE status IN ('pending', 'running')`;
- append-only `audit_log` (§7.2, R-37) — на привилегиях, не на тексте (ревью раунда 1
  показало: проверка стека вызовов `PG_CONTEXT` подделывается литералом в тексте запроса, как и
  любая настройка сессии): `UPDATE`, `DELETE`, `TRUNCATE` отозваны у `app_rw` **и у владельца
  `app_owner`** (владелец может отозвать собственные привилегии; `app_migrate` наследует
  владельца — тоже без них); `DELETE` и `SELECT` одной колонки `ts` выданы роли
  `app_audit_purge`; триггеры `audit_log_immutable` (BEFORE UPDATE OR DELETE FOR EACH ROW) и
  `audit_log_immutable_truncate` (BEFORE TRUNCATE) для всех ролей пропускают только `DELETE` при
  `current_user = 'app_audit_purge'`; ошибка класса `raise_exception`; `EXECUTE` триггерной
  функции у `app_rw` отозван. Остаточный путь мимо функции — явный `SET ROLE app_audit_purge`
  (членство `app_owner` без наследования) или `GRANT` владельцем самому себе: заметные действия,
  равносильные `DISABLE TRIGGER`; защиты от владельца базы внутри базы не существует;
- новая роль `app_audit_purge` (без входа) — в bootstrap `migrations/bootstrap/roles.sql` (роли —
  кластерные объекты, миграция их не создаёт): `USAGE` на схему, `search_path`, `GRANT
  app_audit_purge TO app_owner WITH INHERIT FALSE, SET TRUE`; data-model.md §4.6 дополнен; на
  уже инициализированном стенде bootstrap выполнен повторно вручную (`secrets/README.md`), CI
  выполняет его перед тестами;
- `purge_audit_log() RETURNS bigint` — удаление записей с `ts < now() - interval '12 months'`
  (Н-23, §4.5), возвращает число удалённых; `SECURITY DEFINER`, владелец `app_audit_purge`
  (создаётся под `SET LOCAL ROLE app_audit_purge`, `CREATE` на схему выдаётся только на время
  создания и отзывается), `SET search_path = control_plane, pg_temp`, `SET timezone = 'UTC'` (как
  у функций 080: арифметика месяцев на `timestamptz` идёт в поясе сессии; расхождение возможно на
  днях перевода часов и на границах месяцев — поведенчески не воспроизводится на фиксированных
  датах, страж — каталог `proconfig`); функции `app_audit_purge` не покрыты умолчаниями
  привилегий 0001 (они для функций `app_owner`), поэтому `EXECUTE` у PUBLIC отозван и выдан
  `app_rw` явно; срок — константа функции, не параметр (параметр дал бы приложению возможность
  стереть журнал); удаление одним `DELETE` без порций — планировщик вызывает функцию ежесуточно,
  за вызов уходит суточный объём по индексу `audit_log_ts_idx`; откат снимает функцию как
  владелец схемы (`DROP` объекта схемы доступен её владельцу без `SET ROLE`);
- `settings`: строка `('state_generation', '0')` — первый старт C-01 без метки на хосте
  увеличивает поколение безусловно (reliability.md §9.2); остальные ключи засевают задачи,
  определяющие формат значений;
- сроки хранения строк `jobs` (done, 7 дней), `events` и доставок (90 дней) и прочих
  непартиционированных таблиц §4.5 — функции задачи 001.37 (зависит от 001.09): по §4.6 удаление
  по сроку не выполняется `DELETE` от роли приложения.

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
   - Проверяемая функция: `tests/unit/db/test_schema_ops.py::test_app_rw_privileges`
   - Входные данные: `has_table_privilege` / `has_sequence_privilege` (как в 001.04–001.08;
     `information_schema.role_table_grants` показывает только права текущей роли)
   - Ожидаемый результат: права совпадают с перечнем §4.6

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/unit/db -k 'schema_ops'`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Все таблицы группы существуют с типами и ограничениями §4.2
- [ ] Триггер `audit_log_immutable` отклоняет `UPDATE` и `DELETE` кроме функции ретенции
- [ ] Частичный `UNIQUE (idempotency_key) WHERE status IN ('pending','running')` на `jobs`
- [ ] `settings` содержит строку `state_generation`
- [ ] Откат удаляет только объекты этой миграции

## Примечания

Миграция — конфигурационная задача без пары stub/logic. Партиционированные таблицы получают функцию
создания партиций в 001.08.

Зависимости: 001.04. Приоритет: Critical. Оценка: 3 ч. Этап: 1 — схема данных и каркас Control
Plane.
