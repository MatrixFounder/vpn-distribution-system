# Задача 001.27: Inbound: генерация конфигурации Xray, ротация ключей, проверка цели

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-01 Ввод ноды в эксплуатацию
- UC-12 Компрометация ноды

Требования RTM: R-03.

<!-- contract:goal -->

## Цель задачи

Реализовать генерацию полной конфигурации Xray по `docs/idea.md` §4.4 с рандомизацией `xver` и
`limitFallback*` на ноду, ротацию `short_ids` с окном перекрытия (Н-33), замену ключевой пары при
компрометации и проверку `reality_target` (Н-30).

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/tests/unit/domain/test_xray_config.py` — golden-файлы для трёх профилей; проверка отсутствия credentials в конфигурации

### Изменения в существующих файлах

#### Файл: `control-plane/app/domain/inbounds.py`

- `create_defaults`: ключевая пара, `short_ids` (hex, чётная длина ≤ 16), `spider_x`, `min_client_ver` из `settings`, рандомизация `xver`, `limit_fallback_*`
- `rotate_short_ids`: добавление нового значения, удаление прежнего через 24 часа задачей (Н-33)
- `rotate_keypair(reason=compromise)`: новая пара, событие для уведомления пользователей (001.52)
- `check_target(inbound_id)`: TLS 1.3 + HTTP/2 + без перенаправления; при отказе `error_state`, статус ноды `degraded`, алерт

#### Файл: `control-plane/app/domain/xray_config.py`

- полная сборка по профилям; `config_version` увеличивается через `CompositionService.publish_config(node_id)`

#### Файл: `control-plane/app/jobs/scheduler.py`

- `inbound.check_target` раз в час

### Интеграция компонентов

Перевыпуск конфигурации → новая строка `node_config_versions` и `NOTIFY`/`PUBLISH` для long-poll
(001.29).

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Недоступная цель переводит inbound в ошибку
   - Входные данные: цель без TLS 1.3
   - Ожидаемый результат: `error_state` заполнен не позднее часа; нода `degraded`; алерт «Отложенный» (AC-38)
2. **TC-E2E-02:** Ротация short_id без разрыва
   - Входные данные: `rotate_short_ids`
   - Ожидаемый результат: в конфигурации два значения 24 часа; затем одно

### Модульные тесты

1. **TC-UNIT-01:** Валидация short_id
   - Проверяемая функция: `app/domain/inbounds.py::validate_short_id`
   - Входные данные: `aa1234`, `aaa1234`, 18 символов
   - Ожидаемый результат: успех, ошибка, ошибка
2. **TC-UNIT-02:** Конфигурация без credentials
   - Проверяемая функция: `app/domain/xray_config.py::build`
   - Входные данные: нода с пользователями
   - Ожидаемый результат: в JSON нет `clients` с UUID

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/unit/domain/test_xray_config.py tests/unit/domain/test_inbounds.py tests/e2e/test_nodes.py -k inbound`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Значения `xver` и `limitFallback*` различаются между нодами
- [ ] Компрометация заменяет ключевую пару; пересоздание без компрометации сохраняет её
- [ ] AC-38 выполнен

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.26, 001.29. Приоритет: Critical. Оценка: 4 ч. Этап: 4 — парк нод и Node API.
