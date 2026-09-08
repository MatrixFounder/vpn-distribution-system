# Задача 001.11: Очередь задач в PostgreSQL: интерфейс и каркас исполнителей

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-05 Исчерпание лимита и отзыв доступа
- UC-04 Учёт трафика и списание с коэффициентом

Требования RTM: R-46.

<!-- contract:goal -->

## Цель задачи

Определить интерфейс очереди `jobs` и процессы `worker` и `scheduler` с заглушками обработчиков,
чтобы сквозной тест ставил и забирал задачу.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/jobs/queue.py` — `async def enqueue(conn, queue: str, type: str, payload: dict, idempotency_key: str, run_at=None) -> int`; `async def claim(conn, queue: str, worker_id: str) -> Job | None` (`FOR UPDATE SKIP LOCKED`); `async def complete(conn, job_id) -> None`; `async def fail(conn, job_id, error: str) -> None`
- `control-plane/app/jobs/worker.py` — `async def run(queue: str) -> None`: `LISTEN jobs_<queue>`, страховочный опрос 1 с / 10 с, диспетчер `HANDLERS: dict[str, Handler]`
- `control-plane/app/jobs/scheduler.py` — `async def run() -> None`: `pg_advisory_lock`, расписание из `SCHEDULE: list[Periodic]` (заглушка без задач)
- `control-plane/app/jobs/handlers/__init__.py` — реестр обработчиков; заглушка `noop`
- `control-plane/tests/e2e/test_jobs.py` — постановка и выборка задачи

### Интеграция компонентов

`enqueue` вызывается внутри транзакций доменных операций (outbox). `NOTIFY` выполняется после
коммита обёрткой `transaction()`.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Задача проходит цикл
   - Входные данные: `enqueue(...)` с ключом `k1`; `worker` одной итерации
   - Ожидаемый результат: статус `done`; повторный `enqueue` с `k1` в статусе `pending` отклонён
   - Примечание: заглушка обработчика `noop` возвращает успех
2. **TC-E2E-02:** Две очереди независимы
   - Входные данные: задача в `background` и задача в `critical`
   - Ожидаемый результат: исполнитель `critical` не забирает задачу `background`

### Модульные тесты

1. **TC-UNIT-01:** Частичная уникальность ключа
   - Проверяемая функция: `app/jobs/queue.py::enqueue`
   - Входные данные: ключ существующей задачи в статусе `done`
   - Ожидаемый результат: новая задача создана

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_jobs.py tests/unit/jobs`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] `claim` использует `FOR UPDATE SKIP LOCKED`
- [ ] Обработчики — только `noop`; повторы и `dead` не реализуются (001.74)
- [ ] Метрики глубины очереди экспортируются в `/metrics`

## Примечания

Раскладка бюджета Н-13 — `docs/architectures/interfaces.md` §5.4.

Зависимости: 001.10. Приоритет: Critical. Оценка: 3 ч. Этап: 1 — схема данных и каркас Control
Plane.
