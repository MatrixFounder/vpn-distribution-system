# Задача 001.30: Heartbeat, статусы ноды, пороги Н-15, матрица влияния, метрики

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-07 Отказ и возврат ноды
- UC-01 Ввод ноды в эксплуатацию

Требования RTM: R-05.

<!-- contract:goal -->

## Цель задачи

Реализовать приём heartbeat и метрик, автоматические переходы `offline`/`active`/`degraded` по Н-15
и §4.7, приоритет ручных статусов и матрицу влияния §4.6 источника.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/tests/e2e/test_nodes.py` — UC-07: 90 с без heartbeat → `offline`; два heartbeat → `active`; `maintenance` при живом heartbeat остаётся

### Изменения в существующих файлах

#### Файл: `control-plane/app/agent_api/heartbeat.py`

- `heartbeat`: `last_heartbeat_at`, `ok_heartbeats += 1`, `missed_heartbeats = 0`, версии; `metrics`: запись `node_metrics`

#### Файл: `control-plane/app/domain/statuses.py`

- `class StatusService`: матрица переходов §4.6 с инициатором
- `on_missed_heartbeat` (планировщик раз в 30 с): три пропуска → `offline`
- `on_heartbeat`: два успешных подряд → `active`, только из автоматического статуса
- `degraded`: по порогам ресурсов §4.7 и по `error_state` inbound
- ручные `maintenance`, `disabled`, `suspended`: только администратором, без автовозврата

#### Файл: `control-plane/app/jobs/scheduler.py`

- `nodes.detect_offline` раз в 30 с

### Интеграция компонентов

Смена статуса порождает события «Нода недоступна» / «восстановлена» (001.52) и влияет на выдачу
подписки через фильтр 001.44.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Offline и возврат
   - Входные данные: остановка heartbeat на 90 с, затем два heartbeat
   - Ожидаемый результат: `offline` → `active`; события в `events` (AC-12)
2. **TC-E2E-02:** Однократный пропуск
   - Входные данные: один пропуск
   - Ожидаемый результат: статус не меняется; алерта нет
   - Примечание: UC-07 A3
3. **TC-E2E-03:** Ручной статус не автовозвращается
   - Входные данные: `maintenance` + heartbeat
   - Ожидаемый результат: статус `maintenance`

### Модульные тесты

1. **TC-UNIT-01:** Матрица переходов
   - Проверяемая функция: `app/domain/statuses.py::can_transition`
   - Входные данные: все пары статусов × инициатор
   - Ожидаемый результат: совпадает с таблицей §4.6

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_nodes.py -k status tests/unit/domain/test_statuses.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] AC-12 выполнен полностью, включая инициатора перехода
- [ ] Ноды в ручных статусах не порождают алертов

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.28, 001.29. Приоритет: High. Оценка: 4 ч. Этап: 4 — парк нод и Node API.
