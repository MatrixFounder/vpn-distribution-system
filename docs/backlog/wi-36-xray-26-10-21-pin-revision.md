---
id: WI-36
type: work-item
status: open
opened_at: 2026-10-26
slug: wi-36-xray-26-10-21-pin-revision
effort: S
value: 'задачи 001.26, 001.53 и 001.88 начинаются на закреплённых версиях Xray-core v26.10.21 и sing-box v1.14.4'
source: 'выпуск Xray-core v26.10.21 (2026-10-21); пересмотр по §17.3 источника'
---

# WI-36 — Пересмотр закреплённых версий: Xray-core v26.10.21, sing-box v1.14.4

## Основание

- Xray-core v26.10.21 — стабильный выпуск от 2026-10-21 с пометкой Latest на GitHub.
- Выпуск исправляет уязвимость: из встроенного набора корневых сертификатов удалены три корня.
  Сборка — Go 1.27.3. Выпуски v26.10.10 и v26.10.17 отозваны.
- Примечания к выпуску не содержат списка изменений. Сопровождающие объявили стабильный выпуск со
  списком изменений на конец октября или начало ноября 2026.
- sing-box v1.14.4 — стабильный выпуск от 2026-10-24.
- §17.3 требует пересмотра при выходе стабильного выпуска и при исправлении уязвимости (Н-26).
- При пересмотре перепроверяются допущения §9.1 и имена полей §4.4.

## Метод проверки

- Xray-core: дифф исходников `v26.3.27..v26.10.21` — 335 коммитов, 13 предварительных выпусков
  между тегами.
- REALITY: библиотека `github.com/xtls/reality`, коммит `83f6217` из `go.mod` v26.10.21.
- uTLS Xray-core: `refraction-networking/utls`, коммит `aa6edf4`.
- uTLS sing-box: `metacubex/utls` v1.8.7 — в `go.mod` v1.14.0 и v1.14.4.
- sing-box: сравнение `v1.14.0...v1.14.4` — 174 коммита, файлы `option/`.

## Результаты: Xray-core

### Изменения, требующие правок

1. XHTTP: `sessionPlacement` переименовано в `sessionIDPlacement`, `sessionKey` — в
   `sessionIDKey` (`infra/conf/transport_method.go`, `SplitHTTPConfig`).
   - Декодер JSON пропускает неизвестные ключи (`infra/conf/serial/loader.go`).
   - Прежнее имя → ядро применяет значение по умолчанию `path` → место идентификатора сессии
     расходится с клиентом.
   - Новые необязательные поля: `sessionIDTable`, `sessionIDLength`.
2. REALITY отклоняет ClientHello без ключа X25519MLKEM768 перед X25519.
   - Источник: коммит библиотеки `8cdf7bf` от 2026-09-08, `tls.go`, функция `Server`.
   - Флага отключения в конфигурации нет.
   - Отклонённое соединение уходит на сайт-цель → клиент не подключается, ошибка на сервере не
     пишется.
3. Умолчания `minClientVer` в v26.10.21 нет.
   - Предварительные выпуски v26.7.11 и v26.7.28 по умолчанию отклоняли клиентов ниже v26.3.27
     (коммит `af7eb68`).
   - Умолчание снято в v26.9.9 (коммит `47cfe99`).

### Переименования с сохранёнными псевдонимами

- `settings.clients` → `settings.users` в inbound VLESS и Trojan (коммит `c42deab`). Ядро
  принимает `clients`.
- `streamSettings.network` → `streamSettings.method` (коммит `fb548f5`). Ядро принимает `network`.

### Без изменений

- `realitySettings`: `privateKey`, `shortIds`, `target`, `serverNames`, `minClientVer`,
  `maxClientVer`, `xver`, `limitFallbackUpload`, `limitFallbackDownload`.
- `xhttpSettings`, кроме п. 1. Допустимые значения `mode`, `xPaddingPlacement`,
  `xPaddingMethod`, `uplinkHTTPMethod` те же.
- `api` (`listen`, `services`, `tag`).
- `policy` (`statsUserUplink`, `statsUserDownlink`, `statsUserOnline`, `connIdle`).
- `sockopt.trustedXForwardedFor`.
- Поля правил маршрутизации: `ruleTag`, `source`, `sourceIP`, `ip`, `outboundTag`.
- Outbound `blackhole`: конфигурация `{}` и `response.type` совместимы; добавлен тип `custom`.
- gRPC `HandlerService` и `RoutingService`: сигнатуры прежние. В API маршрутизации исправлена
  гонка данных (коммит `598bde7`).
- gRPC `StatsService`: добавлен метод `GetUsersStats`, прежние методы сохранены.
- `go.mod` ядра: `go 1.27` — совпадает с `node-agent/go.mod`.

### Допущения §9.1

- Д-1, Д-2, Д-3, Д-4, Д-5, Д-7, Д-9, Д-11, Д-12, Д-13, Д-14 подтверждены по исходному коду
  v26.10.21.
- Д-2: `MemoryValidator.Del` удаляет только запись (`proxy/vless/validator.go`). Замер разрыва —
  PoC-1а (001.88).
- Д-5: сообщения 335 коммитов не называют ограничения числа устройств или соединений.
- Д-6, Д-8, Д-10 не зависят от версии ядра.

### Новые возможности

- `StatsService.GetUsersStats` (коммит `a91a88c`) возвращает адреса и `last_seen` всех
  пользователей онлайн одним вызовом.
  - Трафик в ответе есть только у пользователей с адресами онлайн.
  - `reset` обнуляет счётчики только этих пользователей.
  - Учёт трафика остаётся на `QueryStats`; заметка внесена в 001.56.
- `api.listen` принимает путь Unix-сокета: `/путь` или `@имя` (`app/commander/commander.go`).
  - Доступ к gRPC API ограничивается правами на файл сокета.
  - Это предложение; в спецификацию не внесено.

## Результаты: клиенты и отпечаток `fp`

Отпечаток `fp` выбирает шаблон ClientHello uTLS в ядре клиента. Шаблон под одним именем
различается между ядрами.

| `fp` | Шаблон Xray-core | X25519MLKEM768 | Шаблон sing-box 1.14.x | X25519MLKEM768 |
| :--- | :--- | :--- | :--- | :--- |
| `chrome` | `HelloChrome_133` | есть | `HelloChrome_133` | есть |
| `firefox` | `HelloFirefox_148` | есть | `HelloFirefox_120` | нет |
| `safari` | `HelloSafari_26_3` | есть | `HelloSafari_16_0` | нет |
| `ios` | `HelloIOS_14` | нет | `HelloIOS_14` | нет |
| `edge` | `HelloEdge_85` | нет | `HelloEdge_85` | нет |
| `android` | `HelloAndroid_11_OkHttp` | нет | не проверялось | — |
| `360`, `qq` | `Hello360_7_5`, `HelloQQ_11_1` | нет | не проверялось | — |

- sing-box записывает в идентификатор сессии REALITY версию 1.8.1
  (`common/tls/reality_client.go`). `minClientVer` выше 1.8.1 → клиенты sing-box и Hiddify
  отклоняются.
- Shadowrocket и Streisand используют собственные реализации REALITY. Их ClientHello не
  проверялся; проверка — на ноде (001.88, §17.3).

## Результаты: sing-box v1.14.0 → v1.14.4

- Примечания к выпускам 1.14.1–1.14.4: «Fixes and improvements».
- Схема `option/`: у `server_port`, `VLESSUser.name`, `TrojanUser.name` добавлен `omitempty`.
- Из действий правил маршрутизации удалено `direct`.
- Генератор `singbox` (001.43) использует outbound `vless`, `trojan` и `urltest`. Действие
  `direct` он не использует, правок генератора нет.
- uTLS в обоих выпусках — `metacubex/utls` v1.8.7. Таблица отпечатков действует для обоих.

## Решения заказчика (2026-10-26)

1. Xray-core закрепляется на v26.10.21 сейчас. Отклонено: ожидание выпуска конца октября.
2. `reality_min_client_ver` равно 1.8.1, пока поддерживается формат `singbox`.
3. sing-box закрепляется на v1.14.4.
4. Набор допустимых отпечатков `client_fingerprint` хранится в `settings`; на v26.10.21 — только
   `chrome`. Набор перепроверяется при каждом пересмотре по §17.3. Клиенты §4.2 с `fp=chrome`
   проверяются на ноде в 001.88 (TC-E2E-08). Отклонено: свой набор для каждого формата подписки —
   ссылку `vless://` с `fp=firefox` пользователь может импортировать в Hiddify, где она не
   подключится.

Следствие решения 4: отпечаток, исключённый из набора при пересмотре, заменяется у существующих
inbound первым значением набора (idea §4.4).

## Внесённые правки

- `docs/idea.md`: §4.4 (имена полей XHTTP, `reality_min_client_ver`, требование к
  `client_fingerprint`), §9.1, Р-11, ОВ-20, §17.3.
- `docs/TASK.md`: метаданные, О-8, R-03, R-07, R-15.
- `docs/ARCHITECTURE.md`: редакция, §6.
- `docs/architectures/`: `stack.md`, `system-architecture.md`, `deployment.md`, `data-model.md`.
- Задачи: 001.26, 001.27, 001.41, 001.43, 001.56, 001.61, 001.88. Оценка 001.88 (4 ч) не
  изменена: TC-E2E-08 использует импорт подписки TC-E2E-06.
- Развёртка прежних значений: `docs/sweeps/wi-36.json`.

## Не изменено

- Задача 001.01 принята; `docs/reviews/`, `docs/archive/`, `docs/idea-review.md` — история.
- Фикстуры `contracts/agent_v1/*.json` и тесты: строка `26.9.1` — пример значения
  `xray_version`, а не закреплённая версия.
