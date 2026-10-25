# Задача 001.30: Heartbeat и автоматические статусы ноды

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-07 Отказ и возврат ноды
- UC-01 Ввод ноды в эксплуатацию

Требования RTM: R-05.

<!-- contract:goal -->

## Цель задачи

Реализовать приём heartbeat и метрик, автоматические переходы `provisioning`/`active`/`degraded`/
`offline` по §4.6, Н-15 и §4.7, приоритет ручных статусов, выход из `degraded` после неуспешного
`update_agent` и матрицу влияния §4.6 источника. Ручные статусы, их снятие и переходы по событиям
identity — 001.30b; перевод в `suspended` по повторным удержаниям — 001.30c.

<!-- contract:changes -->

## Описание изменений

### Изменения в существующих файлах

#### Файл: `control-plane/tests/e2e/test_nodes.py`

- дополнить: UC-07: возраст heartbeat больше 90 с → `offline`; два heartbeat → `active`; `maintenance` при живом heartbeat остаётся; неуспешное обновление → `degraded`; пересоздание сбрасывает `activated_at` и `update_failed_at` (TC-E2E-11)

#### Файл: `control-plane/app/agent_api/heartbeat.py`

- `heartbeat`: одна транзакция — `last_heartbeat_at`, `ok_heartbeats += 1`, `missed_heartbeats = 0`, версии, правила Н-15 и запись `applied_*` той же функцией проверки и записи курсоров, что и `POST /ack` (001.75); `metrics`: запись `node_metrics`

#### Файл: `control-plane/app/domain/statuses.py`

- `on_missed_heartbeat` (планировщик раз в 10 с): возраст `last_heartbeat_at` по часам Control Plane больше 90 с (Н-15, три интервала) → `offline` (ревью WI-35, группа NODE-36: прежний опрос раз в 30 с с правилом «три пропуска» давал переход через 90–120 с)
- `on_heartbeat(node_id, at, beat)`: `at` — время Control Plane (пороги Н-15 и `nodes.last_heartbeat_at` — измерения сервера), `beat.node_time` — данные для сверки перекоса §5.9. `provisioning` → `active`, когда `applied_config_version ≥ desired_config_version > 0` и `beat.xray_running` (§4.6: конфигурация применена, Xray запущен, heartbeat получен); первый переход в `active` пишет `nodes.activated_at` и ставит задачу `composition.publish_node` (001.29) в транзакции heartbeat. `offline` → `active` — два успешных heartbeat подряд (Н-15), только из автоматического статуса. Нода с `activated_at IS NULL` (после последнего подтверждения ещё не бывшая в `active`) из `offline` по двум heartbeat переходит в `provisioning`, а не в `active` (решение заказчика 2026-10-25, В-13; снятие ручного статуса у такой ноды — 001.30b)
- `degraded` из `active` — по порогам §4.7, включая «Процесс Xray не запущен» (`beat.xray_running = false`) и `error_state` inbound (пишет 001.27); возврат в `active` — пороги в норме два интервала подряд и нет отметки неуспешного обновления. Нода в `provisioning` с нарушенным порогом (inbound в ошибке цели) проходит `provisioning` → `active` по §4.6, затем `active` → `degraded`; перехода `provisioning` → `degraded` нет (решение заказчика 2026-10-25, В-12)
- неуспешное обновление (решение заказчика 2026-10-25, В-15): `mark_update_failed(conn, node_id, at)` пишет `nodes.update_failed_at`; нода `active` переходит в `degraded`; в `provisioning` и `offline` статус не меняется, и нода с отметкой при переходе в `active` сразу получает `degraded`; в ручном статусе отметка не пишется. `clear_update_failed(conn, node_id)` снимает отметку; затем нода возвращается в `active` по порогам §4.7. Отметку снимает следующий успешный `update_agent` (снятие действием администратора — 001.30b). Пороги §4.7 в норме отметку не снимают
- ручные `maintenance`, `disabled`, `suspended` автоматика не меняет: без автовозврата (§4.6 «Приоритет статусов»; ставит и снимает их администратор — 001.30b)
- `on_metrics`: запись `node_metrics`; раздел `targets` → `InboundService.record_target_check` (001.27)
- Изменение принятого кода (отступление от 001.28): `TRANSITIONS` — `offline` → `provisioning` (В-13); `HeartbeatIn` и `NodeMetrics` — поля контракта ниже; страж таблицы — `tests/unit/domain/test_statuses.py`

#### Файл: `control-plane/app/domain/commands.py`

- дополнить (приём результата — 001.76): результат команды `update_agent` в транзакции приёма — `failed` → `StatusService.mark_update_failed`, `applied` → `clear_update_failed` (В-15)

#### Файл: `control-plane/app/domain/nodes.py`

- Изменение принятого кода (отступление от 001.25): `enroll` при обмене нового токена сбрасывает `nodes.activated_at` и `nodes.update_failed_at` — новый агент конфигурацию ещё не применял (В-13)

#### Файл: `contracts/agent_v1/README.md`, фикстуры `contracts/agent_v1/heartbeat.json`, `contracts/agent_v1/metrics.json`

- Изменение контракта agent_v1 (и принятого кода 001.28, `HeartbeatIn`, `NodeMetrics`): в теле heartbeat — `xray_running` (§4.6 «Xray запущен», §4.7 «Состояние процесса Xray»); в теле метрик — `uptime_s` и `agent_uptime_s` (§4.7 «Время работы ноды и агента»), раздел `targets: [{inbound_tag, ok, reason}]` (результат проверки цели агентом, Н-30) и накопительные счётчики агента с момента его запуска: `buffer_dropped_reports`, `counter_decreases`, `clock_offset_ms`, сумма и число `disconnect_seconds`, `report_rejects` по коду. Тела закрыты `extra="forbid"`: агент (001.59) шлёт новые поля только после этой задачи

#### Файлы: миграция `control-plane/migrations/NNN_*.sql` и её откат

- колонки `nodes.activated_at`, `nodes.update_failed_at` (`timestamptz NULL`); у нод в `active` и `degraded` миграция ставит `activated_at = status_changed_at`; колонки `node_metrics` под время работы и счётчики агента

#### Файл: `control-plane/app/jobs/scheduler.py`

- `nodes.detect_offline` раз в 10 с (ревью WI-35, группа NODE-36)

### Интеграция компонентов

Смена статуса порождает события «Нода недоступна» / «восстановлена» через `emit` (001.51,
доставка — 001.52) и влияет на выдачу подписки через фильтр 001.44. Функция проверки и записи
курсоров подтверждения — 001.75; публикация состава ноды — 001.29; запись результата проверки
цели — `InboundService.record_target_check` (001.27). Новые поля heartbeat и метрик отправляет
агент (001.59), правила алертов по ним — 001.68. Событие «Буфер отчётов на ноде заполнен»
строится по данным Control Plane (возраст последнего принятого интервала) — 001.52. Ноды в ручных
статусах алертов не порождают (§17.4). Результат `update_agent` принимает `CommandService.result`
(001.76), нагрузку ставит 001.31. Неизвестный обязательный раздел ответа состояния ноду в
`degraded` не переводит и поля в heartbeat не требует: требование покрывает версия пути
`/agent/v1` (решение заказчика 2026-10-25, В-14). Ручные статусы и переходы по событиям identity —
001.30b; перевод в `suspended` по повторным удержаниям — 001.30c.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Offline и возврат
   - Входные данные: остановка heartbeat, затем два heartbeat
   - Ожидаемый результат: `offline`, когда возраст `last_heartbeat_at` превысил 90 с, не позднее периода опроса (10 с) после порога; затем `active`; события в `events` (AC-12)
2. **TC-E2E-02:** Однократный пропуск
   - Входные данные: один пропуск
   - Ожидаемый результат: статус не меняется; алерта нет
   - Примечание: UC-07 A3
3. **TC-E2E-03:** Ручной статус не автовозвращается
   - Входные данные: `maintenance` + heartbeat
   - Ожидаемый результат: статус `maintenance`
   - Примечание: статус ставит фикстура; маршрут ручного статуса — 001.30b
4. **TC-E2E-04:** Heartbeat — одна транзакция
   - Входные данные: heartbeat с новыми курсорами
   - Ожидаемый результат: `last_heartbeat_at` и `applied_*` записаны одной транзакцией функцией 001.75
5. **TC-E2E-05:** Результат проверки цели из метрик
   - Входные данные: метрики с `targets`, у одного inbound `ok = false`
   - Ожидаемый результат: `error_state` inbound заполнен; нода `active` переходит в `degraded`
6. **TC-E2E-06:** Первый переход в `active` публикует состав
   - Входные данные: нода `provisioning`, heartbeat с применённой конфигурацией и `xray_running = true`
   - Ожидаемый результат: `active`; `activated_at` записан; задача `composition.publish_node` поставлена в транзакции heartbeat; после её исполнения строки состава ноды в дельте
7. **TC-E2E-07:** Ввод ноды с недоступной целью
   - Входные данные: нода `provisioning`; метрики с `targets`, `ok = false`; heartbeat с применённой конфигурацией
   - Ожидаемый результат: `provisioning` → `active` (задача `composition.publish_node` поставлена), затем `degraded`; перехода `provisioning` → `degraded` нет (В-12)
8. **TC-E2E-08:** Нода, после последнего подтверждения ещё не бывшая в `active`
   - Входные данные: нода `provisioning` без применённой конфигурации; пропуск heartbeat дольше 90 с, затем два heartbeat; для сравнения — нода, бывшая `active`
   - Ожидаемый результат: `offline` → `provisioning`; нода, бывшая `active`, из `offline` — `active` (В-13)
9. **TC-E2E-09:** Xray не запущен
   - Входные данные: heartbeat с `xray_running = false` у ноды `active`; у ноды `provisioning` с применённой конфигурацией
   - Ожидаемый результат: первая — `degraded`; вторая остаётся `provisioning`
10. **TC-E2E-10:** Выход из `degraded` после неуспешного обновления
    - Входные данные: результат `update_agent` `failed`; пороги в норме два интервала; результат следующего `update_agent` `applied`
    - Ожидаемый результат: `degraded`; после порогов — `degraded`; после `applied` — `active` (В-15)
11. **TC-E2E-11:** Пересоздание сбрасывает отметки
    - Входные данные: нода, бывшая `active`, с `activated_at` и отметкой `update_failed_at` (в `degraded`); обмен нового токена; подтверждение; heartbeat нового агента с применённой конфигурацией и `xray_running = true`
    - Ожидаемый результат: после обмена `activated_at` и `update_failed_at` — NULL; после heartbeat — `active`, а не `degraded`; `activated_at` записан заново; задача `composition.publish_node` поставлена в транзакции heartbeat (В-13, В-15)

### Модульные тесты

1. **TC-UNIT-01:** Матрица переходов автоматики
   - Проверяемая функция: `app/domain/statuses.py::can_transition`
   - Входные данные: пары статусов автоматических строк §4.6
   - Ожидаемый результат: совпадает с таблицей §4.6; переход `offline` → `provisioning` допустим (В-13)
2. **TC-UNIT-02:** Тела heartbeat и метрик
   - Проверяемая функция: `app/domain/statuses.py::HeartbeatIn`, `NodeMetrics`
   - Входные данные: фикстуры `heartbeat.json` с `xray_running` и `metrics.json` с `targets`, временем работы и счётчиками агента; тело с неизвестным полем
   - Ожидаемый результат: первые приняты; последнее — 422

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/unit/domain/test_statuses.py && pytest tests/e2e/test_nodes.py tests/e2e/test_agent_api.py -k 'status or heartbeat or metrics or enroll'`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] AC-12 в части автоматики: `offline` и возврат по Н-15, ручные статусы без автовозврата
  (инициатор перехода — 001.30b, перевод по удержаниям — 001.30c)
- [ ] Ноды в ручных статусах не порождают алертов
- [ ] `offline` — по возрасту `last_heartbeat_at` больше 90 с по часам Control Plane, опрос раз в 10 с
- [ ] `provisioning` → `active` требует применённой конфигурации и `xray_running`; нода, после
  последнего подтверждения ещё не бывшая в `active`, из `offline` переходит в `provisioning` (В-13)
- [ ] Нода в `provisioning` с inbound в ошибке цели проходит `active`, затем `degraded` (В-12)
- [ ] После неуспешного `update_agent` нода выходит из `degraded` только после успешного
  `update_agent` (действие администратора — 001.30b) (В-15)
- [ ] Heartbeat записывает отметку и курсоры одной транзакцией
- [ ] Тела heartbeat и метрик с новыми полями закреплены в моделях, фикстурах и README

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Задача разделена 2026-10-25 по правилу 4 «Цикла задачи» (решение заказчика В-47 а): ручные
статусы и переходы identity — 001.30b, перевод в `suspended` по повторным удержаниям — 001.30c.


Найдено при 001.28 (заглушки), решить здесь:

- `can_transition` (`app/domain/statuses.py`) пока никем не вызывается: подключить её к
  автоматике (к ручному статусу и инициатор перехода — 001.30b);
- `on_metrics(node_id, metrics)` объявлена сверх перечисленных задачей методов: без неё маршрут
  телеметрии проверял бы тело и выбрасывал его, а 204 этого не показывает.


Найдено при 001.25, раунд 2 роаста (решить здесь):

- identity сверяется в начале запроса (`deps.current_node`): отзыв, зафиксированный, пока запрос
  heartbeat ждёт своей транзакции, не остановит его запись. Запись по heartbeat — под блокировкой
  ноды и с перепроверкой, что identity запроса не отозвана (или одним оператором с условием по
  `node_identities.revoked_at is null`); иначе отозванная нода сдвинет статус и счётчики после
  отзыва.

Зависимости: 001.28, 001.29, 001.27, 001.51, 001.75, 001.76. Приоритет: High. Оценка: 4 ч. Этап: 4 —
парк нод и Node API.
