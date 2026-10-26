# Задача 001.26: Inbound и генератор конфигурации Xray: модель, ключи REALITY, заглушки

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-01 Ввод ноды в эксплуатацию
- UC-03 Получение подписки клиентом

Требования RTM: R-03.

<!-- contract:goal -->

## Цель задачи

Объявить `InboundService` и `XrayConfigBuilder` с сигнатурами и заглушками, заглушки маршрутов
панели над inbound и раздел параметров агента в ответе состояния, чтобы поток конфигурации,
генераторы подписки и агент могли собираться до реализации.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/domain/inbounds.py` — `class InboundService`: `create_defaults(conn, node_id) -> list[Inbound]` (три профиля §4.4, порты 443 / из набора XHTTP / отдельный), `rotate_short_ids(inbound_id)`, `rotate_keypair(inbound_id, reason)`, `set_enabled(inbound_id, bool)`, `set_target(inbound_id, target, server_names)` — заглушки; `def gen_x25519() -> (priv, pub)` (реализуется: библиотека)
- `control-plane/app/domain/xray_config.py` — `class XrayConfigBuilder`: `build(node: Node, inbounds: list[Inbound]) -> dict` — конфигурация без credentials: `api.listen 127.0.0.1:10085`, `policy` с `statsUser*`, три inbound с полями `realitySettings` и `xhttpSettings` в именах v26.10.21, outbound `blackhole` для правил блокировки — заглушка возвращает фиксированный словарь
  - состав inbound — `users`, транспорт — `streamSettings.method` (WI-36); прежние имена `clients` и `network` ядро v26.10.21 принимает как псевдонимы
- `control-plane/tests/unit/domain/test_xray_config.py` — эталонный JSON конфигурации (golden file)

### Изменения в существующих файлах

#### Файл: `control-plane/app/api/admin/nodes.py`

- заглушки маршрутов над inbound: `GET /admin/nodes/{id}/inbounds`, `PATCH /admin/inbounds/{id}` (`target`, `server_names`, `enabled`), `POST /admin/inbounds/{id}/rotate-short-ids`, `POST /admin/inbounds/{id}/rotate-keypair`; разрешения (`x-permission`): чтение `GET /admin/nodes/{id}/inbounds` — `nodes.read` (Operator и выше), изменение и ротации — `nodes.write` (Admin и выше; реестр разрешений — 001.46, матрица — 001.48, `security.md` §7.1)

#### Файл: `control-plane/app/domain/composition.py`

- Изменение принятого кода (отступление от 001.28) и контракта agent_v1: в `StateResponse` раздел `agent` рядом с `config` — `version`, `max_conn_per_ip`, `new_conn_rate_per_ip` (предел частоты новых входящих соединений с одного адреса клиента на порты inbound, соединений в секунду; решение заказчика 2026-10-25, В-10), `multiplier_milli`, `disconnect_strategy` (перечисление без `restart_inbound`, В-9; значение, выбранное PoC-1а, — 001.40). Раздел лежит вне `config.json` и не входит в `config.checksum`; версия раздела — свой курсор, независимый от `config_version`. Смена раздела применяется агентом без перезапуска Xray (перезагрузка набора nftables, запись в Store); перезапуск Xray — только при смене `config.checksum`. Применённую версию раздела агент передаёт в запросе состояния рядом с `config_version` (имя параметра — в README); отставание по ней — повод ответить, как по `config_version` (решение long-poll — 001.75c). Заглушка отдаёт фиксированные значения

#### Файл: `contracts/agent_v1/README.md`, фикстуры `contracts/agent_v1/state-*.json`

- Изменение контракта agent_v1: раздел `agent` в фикстурах ответа состояния; в README — состав раздела, его курсор, параметр запроса с применённой версией раздела и правило применения без перезапуска Xray

#### Файл: `control-plane/app/agent_api/state.py`

- Изменение принятого кода (отступление от 001.28): параметр запроса `GET /agent/v1/state` с применённой версией раздела `agent`

#### Файл: `control-plane/tests/e2e/test_agent_api.py`

- дополнить: раздел `agent` в ответе состояния, сверка с фикстурами (TC-E2E-02)

### Интеграция компонентов

Результат `build` сохраняется в `node_config_versions` (001.29) и отдаётся агенту потоком
конфигурации. Значения раздела `agent` заполняют логические задачи: `max_conn_per_ip` и
`new_conn_rate_per_ip` — 001.27, `multiplier_milli` — 001.36, `disconnect_strategy` — 001.40.
Раздел принимает агент (001.55) и применяет без перезапуска Xray (001.80); читают его 001.57,
001.60 и 001.86.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Ввод ноды создаёт три inbound
   - Входные данные: `create_defaults`
   - Ожидаемый результат: три `Inbound` с разными портами (фиксированные значения), без записи в базу
   - Примечание: запись строк `inbounds` и `inbound_secrets` при создании ноды — 001.27
2. **TC-E2E-02:** Раздел `agent` вне контрольной суммы конфигурации
   - Входные данные: ответ состояния заглушки; фикстуры `state-*.json`
   - Ожидаемый результат: раздел `agent` рядом с `config`; `config.checksum` считается только по `config.json`

### Модульные тесты

1. **TC-UNIT-01:** Пара x25519
   - Проверяемая функция: `app/domain/inbounds.py::gen_x25519`
   - Входные данные: —
   - Ожидаемый результат: 32-байтные ключи; публичный выводится из приватного

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/unit/domain/test_xray_config.py tests/unit/domain/test_inbounds.py tests/e2e/test_agent_api.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Сигнатуры и заглушки маршрутов над inbound объявлены; у каждого маршрута одно разрешение:
  чтение — `nodes.read`, изменение и ротации — `nodes.write`
- [ ] Серверная конфигурация (golden-файл) содержит поля §4.4 источника, включая `minClientVer`, `xver` (значение 0, В-17), `limitFallbackUpload`/`limitFallbackDownload` — объекты `{afterBytes, bytesPerSec, burstBytesPerSec}`; `spiderX` и `fingerprint` — только в клиентских ссылках (001.43)
- [ ] Числа в документе конфигурации — только целые в пределах ±2⁵³; строки без U+2028, U+2029 и управляющих символов (README, «Контрольная сумма конфигурации»)
- [ ] Имена полей XHTTP — `sessionIDPlacement`, `sessionIDKey` (v26.10.21, WI-36); прежних имён `sessionPlacement` и `sessionKey` в конфигурации нет: ядро игнорирует их без ошибки
- [ ] Раздел `agent` объявлен в контракте, фикстурах и README вне `config.json` и его контрольной суммы

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.24. Приоритет: Critical. Оценка: 3 ч. Этап: 4 — парк нод и Node API.
