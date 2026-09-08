# Задача 001.28: Node API: состояние (long-poll), подтверждение, heartbeat, команды — заглушки

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-01 Ввод ноды в эксплуатацию
- UC-07 Отказ и возврат ноды
- UC-11 Обновление парка нод

Требования RTM: R-04, R-05, R-07.

<!-- contract:goal -->

## Цель задачи

Объявить операции `/agent/v1` из `docs/architectures/interfaces.md` §5.2 со схемами и заглушками;
сквозной тест агента проходит на фиксированных ответах.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/agent_api/state.py` — `GET /agent/v1/state?config_version=&users_seq=&generation=&full=`; `POST /agent/v1/ack`; схемы `StateResponse{config?: {version, checksum, json}, users: {seq, rows[]}, generation, resync_required, commands[]}`
- `control-plane/app/agent_api/heartbeat.py` — `POST /agent/v1/heartbeat`, `POST /agent/v1/metrics`
- `control-plane/app/agent_api/commands.py` — `POST /agent/v1/commands/{id}/result`
- `control-plane/app/domain/composition.py` — `class CompositionService`: `state_for(node, config_version, users_seq, generation, full) -> StateResponse`; `publish_config(node_id)`; `publish_user(user_id)`; `remove_user(user_id)`; `ack(node_id, cfg, seq)` — заглушки
- `control-plane/app/domain/commands.py` — `class CommandService`: `issue(node_id, type, payload, ttl)`; `pending(node_id)`; `result(command_id, status, error)` — заглушки
- `control-plane/tests/e2e/test_agent_api.py` — контрактные тесты на зафиксированных запросах и ответах (фикстуры `contracts/agent_v1/*.json`)
- `control-plane/app/agent_api/deps.py` — `current_node` — зависимость по отпечатку сертификата и токену identity; заглушка
- `control-plane/app/jobs/handlers/composition.py` — обработчик `composition.publish_user` — заглушка `noop`
- `control-plane/app/domain/statuses.py` — `class StatusService`: `can_transition`, `on_heartbeat`, `on_missed_heartbeat` — заглушки

### Интеграция компонентов

Фикстуры контракта используются и агентом (001.53) — общий каталог `contracts/agent_v1/`.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Состояние на заглушке
   - Входные данные: `GET /agent/v1/state` от ноды `active`
   - Ожидаемый результат: 200 с фиксированным `StateResponse`; `X-Agent-Version` обязателен → без него 426
   - Примечание: заглушка
2. **TC-E2E-02:** Heartbeat
   - Входные данные: `POST /agent/v1/heartbeat`
   - Ожидаемый результат: 204
   - Примечание: заглушка

### Модульные тесты

- Не требуются: задача не содержит логики сверх заглушек или конфигурации.

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_agent_api.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Схема `StateResponse` зафиксирована в `contracts/agent_v1/`
- [ ] Версия API в пути; `426` на неподдерживаемую версию агента

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.24, 001.11. Приоритет: Critical. Оценка: 3 ч. Этап: 4 — парк нод и Node API.
