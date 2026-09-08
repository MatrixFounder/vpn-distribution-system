# Задача 001.26: Inbound и генератор конфигурации Xray: модель, ключи REALITY, заглушки

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-01 Ввод ноды в эксплуатацию
- UC-03 Получение подписки клиентом

Требования RTM: R-03.

<!-- contract:goal -->

## Цель задачи

Объявить `InboundService` и `XrayConfigBuilder` с сигнатурами и заглушками, чтобы поток конфигурации
и генераторы подписки могли собираться до реализации.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/domain/inbounds.py` — `class InboundService`: `create_defaults(node_id) -> list[Inbound]` (три профиля §4.4, порты 443 / из набора XHTTP / отдельный), `rotate_short_ids(inbound_id)`, `rotate_keypair(inbound_id, reason)`, `set_enabled(inbound_id, bool)` — заглушки; `def gen_x25519() -> (priv, pub)` (реализуется: библиотека)
- `control-plane/app/domain/xray_config.py` — `class XrayConfigBuilder`: `build(node: Node, inbounds: list[Inbound]) -> dict` — конфигурация без credentials: `api.listen 127.0.0.1:10085`, `policy` с `statsUser*`, три inbound с полями `realitySettings` и `xhttpSettings` в именах v26.3.27, outbound `blackhole` для правил блокировки — заглушка возвращает фиксированный словарь
- `control-plane/tests/unit/domain/test_xray_config.py` — эталонный JSON конфигурации (golden file)

### Интеграция компонентов

Результат `build` сохраняется в `node_config_versions` (001.29) и отдаётся агенту потоком
конфигурации.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Ввод ноды создаёт три inbound
   - Входные данные: `create_defaults`
   - Ожидаемый результат: три строки `inbounds` с разными портами; `inbound_secrets` с зашифрованным ключом
   - Примечание: заглушка возвращает фиксированные значения

### Модульные тесты

1. **TC-UNIT-01:** Пара x25519
   - Проверяемая функция: `app/domain/inbounds.py::gen_x25519`
   - Входные данные: —
   - Ожидаемый результат: 32-байтные ключи; публичный выводится из приватного

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/unit/domain/test_xray_config.py tests/unit/domain/test_inbounds.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Сигнатуры объявлены
- [ ] Golden-файл конфигурации содержит поля §4.4 источника, включая `minClientVer`, `xver`, `limitFallback*`, `spiderX`
- [ ] Имена полей XHTTP — `sessionPlacement`, `sessionKey` (v26.3.27)

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.24. Приоритет: Critical. Оценка: 3 ч. Этап: 4 — парк нод и Node API.
