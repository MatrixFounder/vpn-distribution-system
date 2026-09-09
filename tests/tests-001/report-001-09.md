# Отчёт о проверке — задача 001.09 «Схема: события, доставки, очередь задач, аудит, настройки»

Дата: 2026-09-09 (раунд 2 после ревью: append-only на привилегиях и роли `app_audit_purge`
вместо проверки текста стека, граница 12 месяцев ±1 с, исключение триггера только для DELETE —
см. «Раунд 2»). Стенд: VM (`ssh vm`), PostgreSQL 18.6, схема `control_plane`;
`_yoyo_migration` → `0001`, `040`, `050`, `060`, `070`, `080`, `090`. Тесты — с рабочей машины
против базы стенда (`PG_DSN` под `app_rw`, `MIGRATE_DSN` под `app_migrate` → VM:15432).

## Регрессия

- `make check` (nvm use 24, `PG_DSN`/`MIGRATE_DSN`/`REDIS_URL` → стенд) → код 0: ruff, ruff format,
  mypy strict (33 файла), pytest **94 passed** (001.03 — 5, 001.04 — 11, 001.05 — 10, bootstrap — 3,
  001.06 — 18, 001.07 — 15, 001.08 — 18, 001.09 — 14), go, web.
- `cd control-plane && pytest tests/unit/db -k 'schema_ops'` → 11 passed.
- `app.cli migrate`: «применено — 1, всего в источнике — 7»; `--rollback` → «откачено — 1»;
  повторный `migrate` → «применено — 1». Стенд после `vm-sync` и `up -d --build api`: api при
  старте «применено — 0, всего в источнике — 7».
- Bootstrap на стенде выполнен повторно (`docker exec … psql -f - < roles.sql`, процедура
  `secrets/README.md`): роль `app_audit_purge` создана, `pg_auth_members` → `app_owner` член с
  `inherit_option = f`, `set_option = t`; вход по-прежнему только у `app_rw`, `app_migrate`,
  `app_backup`.
- Ручные пробы до тестов (psycopg, откатываемые транзакции): владелец после `REVOKE` собственных
  прав — `DELETE`/`UPDATE`/`TRUNCATE` → `42501`; функция под `app_audit_purge` без `SELECT` на `ts`
  падала на условии `WHERE` (`42501`) — выдан `SELECT (ts)`; функция, созданная `app_audit_purge`,
  получала `EXECUTE` у PUBLIC (умолчания 0001 — только для функций `app_owner`) — отозван явно;
  `DROP FUNCTION` объекта `app_audit_purge` под `app_owner` проходит: владелец схемы удаляет любой
  её объект — откат без `SET ROLE`.

## Сквозные тесты (`tests/e2e/test_schema_ops.py`, 3 теста)

1. **TC-E2E-01** `test_migration_090_apply_rollback_reapply`: после `migrate` шесть таблиц есть;
   `rollback_through("090_schema_ops")` — таблиц группы нет, функций `purge_audit_log`,
   `audit_log_immutable` нет, таблицы 080 на месте; повторный `migrate` возвращает группу.
2. **TC-E2E-02** `test_constraints_and_privileges` (под `app_rw`, откатываемая транзакция): дубль
   `dedup_key` → `UniqueViolationError`; `type` вне enum → ошибка; доставка с чужим `event_id` →
   FK; `attempts = -1` → `CheckViolationError`; `recipient` — citext (поиск без регистра);
   `DELETE events` каскадом убирает `email_deliveries` и `webhook_deliveries`; `jobs`: `done` +
   `pending` с одним ключом приняты, второй `pending` и `running` → `UniqueViolationError`,
   `max_attempts = 0` → `CheckViolationError`, `locked_at` без `locked_by` → `CheckViolationError`,
   после перевода в `running` ключ по-прежнему занят; `audit_log`: `result = 'oops'` →
   `CheckViolationError`, `UPDATE`/`DELETE` → `InsufficientPrivilegeError`; `settings`:
   `state_generation` = `0`, `UPDATE` под `app_rw` → `1`; `purge_audit_log()` (граница считается
   базой той же арифметикой `now() - interval '12 months'`, в той же транзакции — `now()`
   постоянен): записи на 100 суток и на 1 с за границей удалены (= 2), на 1 с до границы и на 65
   суток позже остаются вместе с `n1`, повтор → 0.
3. `test_audit_log_immutable_for_owner` (psycopg под `app_migrate` + `SET ROLE app_owner`, в
   откатываемой транзакции), три слоя: (1) `UPDATE`, `DELETE`, `TRUNCATE` → `InsufficientPrivilege`
   (привилегии отозваны и у владельца); (2) после `GRANT UPDATE, DELETE, TRUNCATE … TO app_owner`
   те же три → `RaiseException` «только для добавления», подделка текста запроса из функции
   `pg_temp.spoof()` с литералом `\nPL/pgSQL function purge_audit_log() line 1 at SQL statement`
   → `RaiseException`; строка на месте; (3) `SET LOCAL ROLE app_audit_purge`: с выданным `UPDATE`
   → `RaiseException` «UPDATE запрещён» (триггер пропускает только DELETE), без него →
   `InsufficientPrivilege`, `SELECT entity_id` → `InsufficientPrivilege` (виден только `ts`),
   `DELETE … WHERE ts IS NOT NULL` проходит — объявленный остаточный путь; после отката строк 0.

## Модульные тесты (`tests/unit/db/test_schema_ops.py`, 10 тестов)

- `test_audit_log_privileges`: `app_owner` и `app_migrate` — `{SELECT, INSERT}`, `app_audit_purge`
  — `{DELETE}` на таблицу и `SELECT` ровно колонки `ts` (`has_column_privilege` по всем колонкам).
- `test_catalog_matches_data_model`: колонки, ограничения, индексы 6 таблиц по §4.2.6/§4.4 с
  объявленными отклонениями (частичные индексы `jobs`, CHECK, каскад FK); оба триггера `audit_log`
  включены и определены как `BEFORE DELETE OR UPDATE … FOR EACH ROW` и `BEFORE TRUNCATE … FOR EACH
  STATEMENT EXECUTE FUNCTION audit_log_immutable()`; `EXECUTE` триггерной функции у `app_rw`
  отозван; `jobs_id_seq`/`audit_log_id_seq` — identity ALWAYS колонки `id`; строка
  `state_generation` = `0`.
- `test_app_rw_privileges[<таблица>]` ×6: `audit_log` — `{SELECT, INSERT}`, остальные — DML;
  `app_backup` — `{SELECT}`.
- `test_sequence_privileges[<последовательность>]` ×2: `app_rw` — USAGE, SELECT; `app_backup` — ∅.
- `test_purge_audit_log`: владелец `app_audit_purge`, `SECURITY DEFINER`, `proconfig` ровно
  `["search_path=control_plane, pg_temp", "TimeZone=UTC"]`, `bigint`, без параметров, `EXECUTE` у
  `app_rw`, не у `app_backup`, не у `app_owner` и не у PUBLIC (для функций `app_audit_purge` это
  реальный страж: без явного `REVOKE` PUBLIC получает `EXECUTE`).
- Смежные стражи обновлены: `test_migrations.py` — пять ролей, вход у трёх, `purge_audit_log`
  единственный объект не `app_owner` помимо таблиц yoyo; `test_bootstrap_schema.py` — `USAGE`
  у `app_audit_purge`, членство `app_owner` без наследования и с SET, `search_path` у четырёх
  ролей, других членов у `app_audit_purge` нет.

## Посадки стражей (до ревью, по одной; файлы восстановлены после каждой, `cmp` — идентичны)

| Посадка | Результат |
| :--- | :--- |
| без `REVOKE UPDATE, DELETE ON audit_log FROM app_rw` | e2e: `DID NOT RAISE InsufficientPrivilegeError` |
| без триггера `audit_log_immutable_truncate` (раунд 1) | e2e владельца: `DID NOT RAISE RaiseException` (TRUNCATE прошёл) |
| триггерная функция пропускает всех (`IF true`, раунд 1) | e2e владельца: `DID NOT RAISE RaiseException` |
| срок `interval '6 months'` вместо 12 (раунд 1) | e2e: `assert 2 == 1` (запись 300 суток удалена) |
| уникальность ключа только `WHERE status = 'pending'` | e2e: `DID NOT RAISE UniqueViolationError` (`running`) |
| без `jobs_pending_idx` | unit: «jobs: индексы расходятся с §4.2» |
| откат без `DROP FUNCTION purge_audit_log` | e2e отката: «функции 090 удалены откатом», `1 == 0`; остаток функции снесён вручную, `migrate` восстановлен |
| без строки `state_generation` | e2e: `assert None == '0'`; unit — та же строка |
| `purge_audit_log` без `SET timezone` | unit: `proconfig` без `TimeZone=UTC` |
| без `CHECK` пары `locked_at`/`locked_by` | e2e: `DID NOT RAISE CheckViolationError` |

После посадок: 13 passed, стенд — 7 миграций, строк в `audit_log`/`jobs`/`events` 0,
`state_generation` = 0, `make check` → 0 (93 passed).

## Раунд 2 — правки по ревью и их проверка

Ревью раунда 1 (`sarcasmotron-001-09`, REJECTED): C-1 — триггер append-only проверял
`PG_CONTEXT`, куда попадает текст оператора, и литерал `'function purge_audit_log()'` в `DELETE`
из plpgsql под `app_owner` обходил его (PoC ревьюера); C-2 — граница 12 месяцев без стража
(посадка `13 months` зелёная); L-1 — `GRANT EXECUTE … TO app_rw` мёртв при умолчаниях 0001;
L-2 — исключение триггера шире контракта (UPDATE тоже); L-3 — `timezone` проверен только
каталогом; L-4 — `DELETE` без порций.

- **C-1 → защита на привилегиях.** Текстовая проверка стека (как и любая настройка сессии) не
  является контролем доступа — её подделает любой, кто может выполнить оператор. Новая роль
  `app_audit_purge` (bootstrap `roles.sql`, без входа; `app_owner` — член `INHERIT FALSE, SET
  TRUE`): единственный держатель `DELETE` на `audit_log` плюс `SELECT (ts)`; `UPDATE`, `DELETE`,
  `TRUNCATE` отозваны у `app_rw` **и у владельца `app_owner`** (а значит, и у `app_migrate`);
  `purge_audit_log()` создаётся под `SET LOCAL ROLE app_audit_purge` (`CREATE` на схему — только
  на время создания), `SECURITY DEFINER`; триггер пропускает только `DELETE` при `current_user =
  'app_audit_purge'`. Остаточный путь — явный `SET ROLE app_audit_purge` или `GRANT` владельцем
  самому себе: заметные действия, равносильные `DISABLE TRIGGER`; объявлено в задаче, заголовке
  миграции, §4.6 модели. PoC ревьюера (подделка текста из `pg_temp`-функции) теперь в тесте
  владельца: `RaiseException` при возвращённых правах, `InsufficientPrivilege` без них.
- **C-2 → граница ±1 с.** Тест вставляет записи с `ts = now() - interval '12 months' ± 1 s` (и
  −100 суток, +65 суток) той же арифметикой и в той же транзакции; `purge_audit_log()` = 2,
  остаются `kept`, `n1`, `recent`. Посадка ревьюера (`13 months`) → `assert 1 == 2`.
- **L-1 → грант живой.** Функция принадлежит `app_audit_purge`, умолчания 0001 (`FOR ROLE
  app_owner`) её не касаются: без явного `REVOKE … FROM PUBLIC` PUBLIC получает `EXECUTE`, без
  `GRANT … TO app_rw` планировщик не вызовет функцию. Посадки ревьюера (без `GRANT`) → e2e
  `permission denied for function purge_audit_log`; без `REVOKE` → unit `app_owner: True`.
- **L-2 → `TG_OP = 'DELETE'`.** Посадка «пропускать роль очистки на любой операции» → e2e
  «UPDATE запрещён» не поднят.
- **L-3 → честно.** Поведенческий тест пояса для «минус 12 месяцев» на фиксированных смещениях
  (`Etc/GMT±N`) не может покраснеть без закрепления: расхождение возникает только на днях
  перевода часов и на границах месяцев, а `now()` тесту не подвластен. Страж остаётся
  каталожным (`proconfig` ровно с `TimeZone=UTC`, посадка без `SET timezone` красная); мотив в
  задаче переформулирован без завышения.
- **L-4 → решение зафиксировано:** один `DELETE` без порций — планировщик вызывает функцию
  ежесуточно, за вызов уходит суточный объём по `audit_log_ts_idx`; первый годовой массив
  возникает лишь при неработающем планировщике, что отдельная неисправность.
- Сопутствующее: `EXPECTED_ROLES`/`LOGIN_ROLES` и исключение владения `purge_audit_log` в
  `test_migrations.py`; `test_bootstrap_schema.py` — `app_audit_purge`; `10-roles.sh` и
  `secrets/README.md` — пять ролей; `data-model.md` §4.6 — роль и остаточный путь.

Посадки раунда 2 (по одной, файлы восстановлены после каждой; `pytest test_schema_ops` e2e + unit,
для I — плюс `test_bootstrap_schema`):

| Посадка | Результат |
| :--- | :--- |
| A `interval '13 months'` (посадка ревьюера) | e2e: `assert 1 == 2` — запись за 1 с до границы не удалена |
| B без `GRANT EXECUTE … TO app_rw` (посадка ревьюера) | e2e: `permission denied for function purge_audit_log` |
| C без `REVOKE EXECUTE … FROM PUBLIC` | unit: `app_owner: True` ≠ `False` |
| D без `REVOKE … FROM app_owner` | e2e владельца: слой 1 получает `RaiseException` вместо `InsufficientPrivilege` |
| E триггер пропускает роль очистки на любой операции | e2e владельца: `DID NOT RAISE RaiseException` («UPDATE запрещён») |
| F триггер пропускает любой `DELETE` | e2e владельца: `DID NOT RAISE RaiseException` (слой 2) |
| G `GRANT SELECT` на всю таблицу роли очистки | e2e владельца: `DID NOT RAISE InsufficientPrivilege` (`SELECT entity_id`) |
| H функция создана `app_owner` (без `SET ROLE`) | e2e: `purge_audit_log()` под `app_rw` → `permission denied for table audit_log` |
| I без `REVOKE CREATE ON SCHEMA … FROM app_audit_purge` | bootstrap-unit: `app_audit_purge: {CREATE, USAGE}` ≠ `{USAGE}` |
| J откат без `DROP FUNCTION purge_audit_log` | e2e отката: «функции 090 удалены откатом», `1 == 0`; остаток снесён владельцем схемы |

Инцидент посадок: откат старой версии 090 новым файлом отката (`SET LOCAL ROLE app_audit_purge;
DROP FUNCTION`) упал — «must be owner of function» — функция тогда принадлежала `app_owner`;
откат переписан на удаление владельцем схемы, который снимает объект любого владельца. После
посадок: 78 passed (unit/db + e2e ops + migrations), `make check` → 0 (94 passed); стенд — 7
миграций, строк 0, `purge_audit_log` принадлежит `app_audit_purge`, замок yoyo пуст, ACL
`audit_log` = `{app_owner=arxtm, app_rw=ar, app_backup=r, app_audit_purge=d}`, колонка `ts` —
`{app_audit_purge=r}`.

### Вердикт раунда 2

`sarcasmotron-001-09`: **APPROVED**. Ревьюер повторил свой PoC (подделка текста, в том числе с
переводом строки) под `app_owner`: без самограната — `InsufficientPrivilege`, с ним —
`RaiseException`; `SET ROLE app_audit_purge` недоступен `app_rw`/`app_backup`, доступен
`app_migrate` по объявленной цепочке; ACL таблицы, колонки, схемы, владелец и `proconfig`
функции совпали с заявленными. Посадки ревьюера: `13 months` → красная (`assert 1 == 2`);
`settings` под `app_audit_purge` (снята строка `SET LOCAL ROLE app_owner`) → 5 failed (страж
владения объектов, bootstrap, каталог, права, e2e). `make check` → 0, 94 passed. Возражение
по L-3 принято; стилевое замечание о bootstrap (проверка членства без опций — как у гранта
`app_migrate`) зафиксировано, правок не требует.

## Критерии приёмки

- [x] Все таблицы группы существуют с типами и ограничениями §4.2 — исполняемая сверка
- [x] Триггер `audit_log_immutable` отклоняет `UPDATE` и `DELETE` кроме функции ретенции — под
      `app_owner` с возвращёнными правами и при подделке текста → `RaiseException`, без прав →
      `InsufficientPrivilege`; `purge_audit_log()` под `app_rw` удаляет ровно по границе; посадки
      D–H красные
- [x] Частичный `UNIQUE (idempotency_key) WHERE status IN ('pending','running')` на `jobs` —
      определение индекса в unit; `pending`/`running` под `app_rw` в e2e
- [x] `settings` содержит строку `state_generation` — unit и e2e (`'0'`)
- [x] Откат удаляет только объекты этой миграции — 080 на месте; функции сняты

## Отклонения от описания задачи

- Умолчания, `NOT NULL`, `CHECK`, identity, каскад доставок, индексы по FK, триггер на `TRUNCATE`,
  append-only на привилегиях с ролью `app_audit_purge` (bootstrap, §4.6 модели дополнен),
  `purge_audit_log()` (12 месяцев, UTC, без параметра, владелец `app_audit_purge`), строка
  `state_generation` = `0`, отсутствие FK у ссылок по идентификатору — объявлены поимённо в задаче
  и заголовке миграции.
- Сроки хранения строк непартиционированных таблиц (§4.5) — задача 001.37, зафиксировано в задаче.
- Модуль TC-UNIT-01 — `tests/unit/db/test_schema_ops.py` (в задаче был указан несуществующий
  `test_grants.py`); права — `has_table_privilege`/`has_sequence_privilege`.
- `owner_connection()` вынесен из теста 001.08 в `tests/e2e/_db.py` (используется двумя модулями).
