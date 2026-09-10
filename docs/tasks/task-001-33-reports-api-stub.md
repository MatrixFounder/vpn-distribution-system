# Задача 001.33: Node API отчётов, модули учёта и лимитов — сигнатуры и заглушки

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-04 Учёт трафика и списание с коэффициентом

Требования RTM: R-19, R-21, R-22, R-23, R-24, R-26, R-27, R-29, R-48.

<!-- contract:goal -->

## Цель задачи

Объявить `POST /agent/v1/reports` и `POST /agent/v1/quota/request` со схемами по
`docs/architectures/interfaces.md` §5.2, а также сигнатуры всех модулей учёта (`AccountingService`,
`multiplier`, `LimitsService`, `QuotaService`, сверки, агрегация, лимит адресов, признаки
перепродажи) с заглушками, чтобы логические задачи этапа 5 изменяли существующие файлы.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/agent_api/reports.py` — `POST /agent/v1/reports`: `{node_id?, counter_epoch, report_seq, period_start, period_end, lines[{user_id, uplink, downlink}], online_ips[{user_id, ip, last_seen}], node_rx, node_tx}` → `{last_accepted_seq, duplicate}`; `POST /agent/v1/quota/request`
- `control-plane/app/accounting/service.py` — `class AccountingService`: `accept_report(node, report) -> Accept`; `close_hour(node_id, at)`; заглушки
- `control-plane/tests/e2e/test_reports.py` — приём отчёта на заглушке; фикстуры контракта
- `control-plane/app/domain/multiplier.py` — `async def resolve(conn, node_id, at) -> Resolved(multiplier_milli, billing_group_id, source)`; `def billable(raw: int, milli: int) -> int` — заглушки с фиксированными значениями
- `control-plane/app/accounting/limits.py` — `class LimitsService`: `check(user_id)` — заглушка
- `control-plane/app/accounting/quota.py` — `class QuotaService`: `grant(user_id, node_id) -> int`; `on_report(user_id, node_id, billable)` — заглушки
- `control-plane/app/accounting/reconcile.py` — `arithmetic(day)`, `cross_source(node_id, day, tolerance_pct)`, `continuity(node_id)` — заглушки
- `control-plane/app/accounting/aggregate.py` — `aggregate_day(day)` — заглушка
- `control-plane/app/accounting/device_limit.py` — `class DeviceLimitService`: `evaluate(user_id)` — заглушка
- `control-plane/app/accounting/signals.py` — `resale_signals(user_id, days=7) -> Signals` — заглушка
- `control-plane/app/jobs/handlers/limits.py` — обработчик `limits.check` — заглушка `noop`
- `control-plane/app/jobs/handlers/maintenance.py` — `partitions.ensure`, `partitions.drop_expired`, `retention.purge` — заглушки

### Интеграция компонентов

Схема отчёта — часть контракта `contracts/agent_v1/`.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Отчёт принят
   - Входные данные: валидный отчёт
   - Ожидаемый результат: 200 `{last_accepted_seq: 1, duplicate: false}`
   - Примечание: заглушка

### Модульные тесты

- Не требуются: задача не содержит логики сверх заглушек или конфигурации.

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_reports.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Схемы зафиксированы в контракте `contracts/agent_v1/`
- [ ] Сигнатуры всех модулей учёта объявлены; каждая возвращает фиксированное значение
- [ ] Тест проходит на заглушках

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.


Найдено при 001.28 (заглушки), решить здесь:

- предел тела агентского `server` nginx снижен до 64 КБ по операциям 001.28 (тело читается до
  отказа по версии агента и identity, поэтому предел — защита от оплаты чужого мусора). Отчёты о
  трафике со строками по пользователям больше: поднимать предел надо **своим** `location =
  /agent/v1/reports`, а не на весь `server`. Страж `tests/unit/test_proxy_contract.py` требует,
  чтобы на уровне `server` остался ровно один предел, и покраснеет на попытке поднять его там.

Зависимости: 001.28, 001.08. Приоритет: Critical. Оценка: 4 ч. Этап: 5 — учёт трафика и лимиты.
