# Отчёт о проверке — задача 001.11 «Очередь задач в PostgreSQL: интерфейс и каркас исполнителей»

Дата: 2026-09-09 (раунд 3 после двух ревью: живучесть исполнителя и планировщика, герметичность
тестов на живом стенде, выборка по известным типам, стражи порядка и `db_up 0`, сброс флага
пробуждения и семантика пустого набора типов — см. «Раунд 2», «Раунд 3»).
Стенд: VM (`ssh vm`), Compose `control-plane` — работают все роли: api, worker-critical,
worker-background, scheduler; тесты выполняются **при работающих контейнерах** с рабочей машины
против базы стенда (`PG_DSN` под `app_rw` → VM:15432).

## Регрессия

- `make check` (nvm use 24, `PG_DSN`/`MIGRATE_DSN`/`REDIS_URL` → стенд) при живых контейнерах
  worker-critical, worker-background, scheduler → код 0: ruff, ruff format, mypy strict
  (55 файлов), pytest **124 passed** (прежние 111 + 001.11: e2e 10, unit 3), go, web;
  `shellcheck docker-entrypoint.sh` — чисто. После прогона все три контейнера `Up`; в их
  журналах — предупреждения `UndefinedTableError: relation "jobs" does not exist` и
  `InternalServerError: cache lookup failed for type …` с пометкой «подключения пула будут
  обновлены» (тесты схемы откатывают и повторяют миграцию 090) — процессы пережили это и
  продолжили работу.
- `cd control-plane && pytest tests/e2e/test_jobs.py tests/unit/jobs` при живых контейнерах →
  13 passed (≈7 с).
- Раунд 1 заявлял «122 passed» по прогону, выполненному **до** запуска контейнеров исполнителей
  (они тогда падали на `HOME=/root`, см. «Стенд»); при живых исполнителях тот же набор давал
  4 failed — тесты соревновались с ними за задачи, а тесты схемы убивали исполнителей
  (C-1…C-5 ревью). Исправлено в раунде 2.
- Проба на стенде до тестов (psycopg/asyncpg с рабочей машины): `enqueue` k1 → дубль →
  `DuplicateJobError`, транзакция жива (`select 1`), `depth` показывает `pending 1`,
  `run_once` → 1, строка `done, attempts 1, locked_by NULL`, повторный `enqueue` k1 → новый id,
  уборка → 0 строк. Первый вариант `enqueue` перехватывал `UniqueViolationError` — ошибка
  PostgreSQL переводила транзакцию в aborted, и внешний `COMMIT` молча откатывал постановку
  (проба показала `depth {}` и `processed 0`); заменено на `ON CONFLICT … DO NOTHING`.

## Стенд

`vm-sync`, `up -d --build api worker-critical worker-background scheduler`. Первый запуск ролей
worker/scheduler упал: `asyncpg … ssl.load_cert_chain → PermissionError` — `setpriv` наследовал
`HOME=/root`, и asyncpg искал клиентский сертификат в `/root/.postgresql/` (каталог 0700). В
`docker-entrypoint.sh` перед `setpriv` добавлено `export HOME=/app USER=app`; после пересборки:

```text
api                Up (healthy)          worker-critical    Up   «исполнитель …:1:critical слушает jobs_critical»
worker-background  Up   «… слушает jobs_background»
scheduler          Up   «планировщик — лидер; расписание: 0»
api:8000/metrics (внутри сети): control_plane_db_up 1, control_plane_jobs{queue,status} — 10 рядов с нулями
```

Сквозная проба с рабочей машины: `enqueue("critical", "noop", …)` через базу стенда → исполнитель
в контейнере забрал и завершил задачу за **0,05 с** (по NOTIFY, опрос critical — 1 с); строка
удалена. В раунде 1 после прогона ревьюера оба исполнителя были `Exited (1)` (`LookupError` из
`complete()` при удалённой уборкой строке; `cache lookup failed for type` после пересоздания
перечислений тестами схемы) — в раунде 2 контейнеры пересобраны из исправленного кода и
пережили полный `make check`.

## Сквозные тесты (`control-plane/tests/e2e/test_jobs.py`, 10 тестов) — на живом стенде

Все тесты берут пул процесса под окружением стенда (`_jobs.stand_pool`), регистрируют в
`HANDLERS` своего процесса типы `test-noop`, `test-boom`, `test-vanish` и ставят только их —
живые исполнители стенда выбирают лишь известные им типы (`noop`) и тестовые задачи не трогают;
планировщик тестов берёт свой ключ лидерства `TEST_LOCK_KEY`; ключи `test-jobs:*`, уборка до и
после.

1. **TC-E2E-01 + TC-UNIT-01** `test_job_cycle_and_idempotency`: `enqueue` k1 в транзакции,
   дубль → `DuplicateJobError`, транзакция жива; `run_once` → 1, строка ровно `{done, 1, None,
   None, None}`; повтор → 0; после done `enqueue` k1 → новый id (частичная уникальность §4.4);
   `claim` → `running` держит ключ (дубль отклонён); `complete` дважды → `LookupError`.
2. **TC-E2E-02** `test_queues_are_independent`: задачи в background и critical; `claim(critical)`
   возвращает критичную (`running`), второй `claim(critical)` → `None`; `run_once(critical)` → 0,
   `run_once(background)` → 1.
3. `test_failures_unknown_type_and_run_at`: обработчик с исключением → `failed`, `last_error =
   «RuntimeError: сломалось 7»`, блокировка снята, `attempts 1`; обработчик, удаливший свою
   строку (`test-vanish`) — `run_once` не падает (`LookupError` при завершении →
   предупреждение); задача неизвестного типа (`test-unknown`) не выбирается — остаётся
   `pending`; `claim(types=())` → `None`, `claim(types=None)` берёт её (пустой набор — ничего,
   `None` — любые); `run_at` через час → `pending`; после failed ключ свободен.
3а. `test_claim_order_is_run_at_then_id`: пять задач с `run_at` в прошлом в перепутанном порядке
   постановки → выборка возвращает их по `run_at`, при равных `run_at` — по `id`.
4. `test_claim_skips_rows_locked_by_another_session`: `for update skip locked` в `CLAIM_SQL`
   (критерий приёмки); строка, заблокированная чужой транзакцией (`SELECT … FOR UPDATE`), —
   `claim` → `None` за < 5 с (не ждёт); после отката — забирается, `attempts 1`.
5. `test_notify_only_on_commit`: слушатель `jobs_critical`; постановка с откатом → уведомления
   нет за 1 с; постановка в транзакции — до коммита нет за 0,5 с, после коммита приходит id.
6. `test_worker_loop_wakes_on_notify_and_survives_faults`: `worker.run("background", stop, pool)`
   (опрос 10 с) — задача `done` за < 5 с после `enqueue`; `pg_terminate_backend` подключения
   LISTEN (по `application_name = worker:background:listener`) → через 2,5 с новая задача снова
   `done` за < 5 с (подключение восстановлено, иначе ждали бы опроса 10 с); подмена
   `jobs.claim` — первый вызов поднимает `InternalServerError("cache lookup failed for type…")`
   → задача выполнена за < 8 с, цикл жив (`calls ≥ 2`); подмена `connect_listener` на отказ
   (`OSError`) + `pg_terminate_backend` слушателя (флаг пробуждения взведён обрывом) → за 2,5 с
   не более 4 попыток переподключения (пауза 1 с работает), цикл жив; остановка по событию;
   блокировка снята.
7. `test_scheduler_leadership_lock`: `scheduler.run(stop, schedule=[], pool,
   lock_key=TEST_LOCK_KEY)` берёт свой ключ (проба из другой сессии → false),
   `acquire_leadership(probe, TEST_LOCK_KEY)` → False (второй экземпляр не лидер),
   `acquire_leadership(probe, LEADER_LOCK_KEY)` → False (боевой ключ держит планировщик стенда);
   после остановки ключ свободен; `Periodic` с нулевым интервалом и с неизвестной очередью →
   `ValueError`; `tick` с `Periodic(5 мин, test-noop)`: первый вызов → 1, тот же слот → 0,
   следующий слот → 1.
8. `test_metrics_report_queue_depth`: две задачи critical типа `test-noop` (исполнитель стенда
   их не берёт) → `/metrics` содержит `control_plane_db_up 1`,
   `control_plane_jobs{queue="critical",status="pending"} 2` и ряд для каждой из 10 пар.
9. `test_metrics_without_database`: `PG_DSN` на закрытый порт → `metrics.render()` отдаёт
   `control_plane_up 1`, `control_plane_db_up 0`, без рядов очереди и без исключения.

## Модульные тесты (`control-plane/tests/unit/jobs/test_queue.py`, 3 теста, без базы)

- `CLAIM_SQL`: `for update skip locked`, `status = 'pending'`, `run_at <= now()`, `attempts =
  attempts + 1`, без форматирования строк.
- Каналы: `jobs_critical`, `jobs_background`.
- `Periodic`: ключ одинаков внутри часового слота, различается между слотами, начинается с имени.

## Посадки стражей (до ревью, по одной; файлы восстановлены после каждой, `diff` пуст)

| Посадка | Результат |
| :--- | :--- |
| `FOR UPDATE` без `SKIP LOCKED` | unit/e2e: «критерий приёмки 001.11» |
| выборка без фильтра по очереди | e2e: `claim(critical)` вернул background (`77 == 78`) |
| выборка без `run_at <= now()` | e2e: `assert 3 == 2` (задача из будущего выполнена) |
| `enqueue` без NOTIFY | e2e: `TimeoutError` — уведомление не пришло |
| дубль ключа перезаписывает (`DO UPDATE`) | e2e: `DID NOT RAISE DuplicateJobError` |
| упавший обработчик → `complete` | e2e: строка `done` ≠ `failed` |
| завершение не снимает блокировку | e2e: `locked_by = 'w1'` ≠ `None` |
| планировщик без блокировки лидера | e2e: «планировщик не взял блокировку лидера» |
| метрики без нулевых пар | e2e: нет ряда `critical/running` |

После посадок: 11 passed, строк в `jobs` на стенде 0, `make check` → 0 (122 passed).

## Раунд 2 — правки по ревью и их проверка

Ревью раунда 1 (`sarcasmotron-001-11`, REJECTED): C-1 `make check` красный при живых
исполнителях (4 failed); C-2 страж лидерства красный при живом планировщике стенда (общий
ключ); C-3 исполнитель умирал от `LookupError` из `complete()`; C-4 — от `cache lookup failed
for type` после пересоздания перечислений; C-5 e2e соревновались с живыми исполнителями за
задачи; L-1 порядок выборки без стража (LIFO зелёный); L-2 `db_up 0` без теста; L-3 обрыв LISTEN
не обнаруживался; L-4 завершение на подключении, испорченном обработчиком; L-5 `Periodic` без
проверки интервала; L-6 `count(*)` по всей таблице на каждый опрос; L-7 расщепление лидерства
не названо.

- **C-3, C-4, L-3, L-4 → живучесть.** `worker.run`: выборка и завершение в `try/except
  RECOVERABLE` (ошибки PostgreSQL, сети, таймауты) → предупреждение, `pool.expire_connections()`
  (устаревшие кэши типов, разорванные сессии), пауза 1 с, продолжение; `LookupError` при
  завершении → предупреждение; обработчик и запись статуса на разных подключениях; LISTEN —
  `add_termination_listener` + `is_closed()` с переподключением. То же в `scheduler.run`
  (захват лидерства и `tick`). Стенд после полного `make check` (тесты схемы роняют и
  пересоздают `jobs` и перечисления): все три контейнера `Up`, в журналах предупреждения
  «подключения пула будут обновлены».
- **C-5, C-2 → герметичность.** `claim(…, types=)` — исполнитель берёт только типы из
  `HANDLERS` (и это полезное свойство поэтапного выпуска §17.2); тесты ставят задачи только
  своих типов, живые исполнители их не видят; `scheduler.run(lock_key=)` — тесты берут свой
  ключ, боевой ключ проверяется как занятый.
- **C-1 → регрессия честная:** `make check` выполнен при живых контейнерах → 124 passed, все
  контейнеры пережили прогон (см. «Регрессия»).
- **L-1, L-2, L-5** — стражи: порядок `run_at, id`; `db_up 0` без базы; `ValueError` на нулевой
  интервал и неизвестную очередь. **L-6, L-7** — решения записаны в задаче (срок хранения
  `done` 7 суток бережёт таблицу; расщепление лидерства обезврежено ключом слота).

Посадки раунда 2 (по одной, при живых контейнерах; файлы восстановлены, `diff` пуст):

| Посадка | Результат |
| :--- | :--- |
| ошибка выборки роняет цикл (`raise`) | e2e: «после ошибки базы цикл не восстановился» |
| обрыв LISTEN не восстанавливается (без `is_closed()`) | e2e: «после обрыва LISTEN не восстановился» |
| выборка без фильтра типов | e2e: `test-unknown` выбран, `3 == 2` |
| порядок LIFO (посадка ревьюера) | e2e: `[92, 94, 93, 96, 95] == [95, 96, 93, 94, 92]` |
| `LookupError` при завершении роняет исполнителя (C-3) | e2e: `LookupError: задача 105 не выполняется` |
| метрики падают без базы | e2e: `ConnectionRefusedError` |
| планировщик игнорирует `lock_key` | e2e: «планировщик не взял блокировку лидера» (боевой ключ у стенда) |
| `Periodic` без проверки интервала | e2e: `DID NOT RAISE ValueError` |

После посадок: 13 passed, контейнеры `Up`, строк в `jobs` 0.

## Раунд 3 — правки по ревью раунда 2 и их проверка

Ревью раунда 2 (`sarcasmotron-001-11`, REJECTED): L2-1 (MAJOR) — при обрыве LISTEN и недоступной
базе `wakeup`, взведённый обрывом, не сбрасывался до паузы переподключения → холостая прокрутка
(48 219 попыток за 2 с у ревьюера); L2-2 (MAJOR) — `claim(types=())` трактовался как «любые типы»;
L2-3 — транзиентные и постоянные ошибки не различаются (принято, записано в задаче); L2-4 —
ссылка на §17.2 была экстраполяцией (формулировка исправлена).

- **L2-1:** `wakeup.clear()` перенесён в начало витка (до блока переподключения); страж — в
  тесте цикла: `connect_listener` подменён на `OSError`, слушатель оборван
  `pg_terminate_backend`, за 2,5 с ≤ 4 попыток. Посадка ревьюера (сброс после блока) →
  «переподключение крутится вхолостую: 27065 за 2,5 с».
- **L2-2:** `known = None if types is None else list(types)`; страж — `claim(types=())` → `None`
  при ожидающей задаче, `claim(types=None)` берёт её. Посадка ревьюера (`if types else None`) →
  красная (взята задача `test-unknown`).

После правок: `make check` при живых контейнерах → 0, 124 passed; контейнеры пересобраны и `Up`.

### Вердикт раунда 3

`sarcasmotron-001-11`: **APPROVED**. Ревьюер замерил переподключение LISTEN на стенде сам: раунд 2 —
48 219 попыток за 2,5 с, раунд 3 — 3 (пауза 1 с), с его посадкой — 28 286; три посадки красные
(сброс флага после блока переподключения; `if types else None`; сброс флага после `run_once` —
потеря пробуждения: страж двусторонний). `make check` → 0, 124 passed; контейнеры `Up`, база
чистая. Единственная придирка — устаревшая ссылка на §17.2 в комментарии `queue.py` — исправлена
после вердикта (комментарий приведён к формулировке задачи).

## Критерии приёмки

- [x] `claim` использует `FOR UPDATE SKIP LOCKED` — текст `CLAIM_SQL` (unit и e2e) и поведение при
      чужой блокировке строки
- [x] Обработчики — только `noop`; повторы и `dead` не реализуются — `HANDLERS = {"noop"}`,
      `fail` терминален, `attempts`/`max_attempts` только хранятся (001.74); тестовые типы
      живут только в реестре процесса тестов
- [x] Метрики глубины очереди экспортируются в `/metrics` — `control_plane_jobs{queue,status}`,
      проверено тестом и внутри сети стенда

## Отклонения от описания задачи

- `NOTIFY` без обёртки `transaction()` — доставка при коммите обеспечивается PostgreSQL;
  дубль ключа — `ON CONFLICT DO NOTHING` (транзакция не портится); `claim(types=)` — выборка
  только известных типов; `run(queue, stop=, pool=)`, `run_once()`, `scheduler.run(lock_key=)`
  сверх контракта; живучесть циклов при ошибках базы и обрыве LISTEN; `attempts` растёт при
  выборке; `app/metrics.py` вынесен из `main.py`; `HOME=/app` в `docker-entrypoint.sh`. Всё
  объявлено в «Уточнениях при реализации» задачи; карты `app/.AGENTS.md`,
  `control-plane/.AGENTS.md`.
- Идентификаторы задач на стенде начинаются заново после каждого прогона `test_schema_ops`
  (откат/повтор 090 пересоздаёт identity-последовательность) — свойство тестов схемы, не очереди.
