# Задача 001.31: Закреплённые версии, команда `update_agent`, ротация identity с перекрытием

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-11 Обновление парка нод
- UC-12 Компрометация ноды
- UC-10 Восстановление Control Plane из резервной копии

Требования RTM: R-07, R-44.

<!-- contract:goal -->

## Цель задачи

Реализовать постановку команд администратором и полезные нагрузки `update_agent`, `restart_xray`,
`rotate_credentials`, `collect_diagnostics`, проверку `update_agent` по закреплённым версиям в
`settings`, результат `collect_diagnostics` в поле `output`, автоматическую ротацию identity ноды
(постановка `rotate_credentials` и операция Node API подачи CSR) с окном перекрытия Н-28 и
продлением окна после смены `state_generation` (§5.3). Выдача команд ноде, TTL и статусы — 001.76.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/agent_api/identity.py` — `POST /agent/v1/identity/rotate`: операция Node API
  подачи CSR при ротации (решение заказчика 2026-10-25, В-16). Запрос — по mTLS с токеном
  действующей identity, тело `{csr_pem}` (модель — как `csr_pem` обмена токена 001.25,
  `extra="forbid"`); ответ 200 `{client_cert_pem, identity_token, generation}`; отказ проверки
  identity — 401, как у раздела; отказ CSR (`CsrRejectedError`) — 422

### Изменения в существующих файлах

#### Файл: `control-plane/tests/e2e/test_nodes.py`

- дополнить: постановка `update_agent` с проверкой закреплённой версии; разрешения маршрутов команд по ролям (В-39); чтение команд ноды и результата с `output` (TC-E2E-08)

#### Файл: `control-plane/tests/e2e/test_agent_api.py`

- дополнить: операция ротации; окно перекрытия; продление окна; результат с `output`

#### Файл: `control-plane/app/domain/commands.py`

- полезная нагрузка `update_agent`: `{component: agent | xray, version, url, sha256}` — обновляется агент или Xray-core (UC-11, Н-26); `validate_update_payload` проверяет компонент, контрольную сумму и совпадение версии с закреплённой для компонента в `settings`
- нагрузки `rotate_credentials` и `collect_diagnostics` пусты (`{}`): по `rotate_credentials` агент создаёт ключ P-256 и CSR и вызывает `POST /agent/v1/identity/rotate`; собранное по `collect_diagnostics` агент возвращает в `output` результата
- Изменение принятого кода (отступление от 001.28) и контракта agent_v1: `CommandResultIn` получает поле `output` — результат `collect_diagnostics`: версии агента и Xray, статус служб, хвост журнала агента без адресов пользователей; не больше 65 536 байт в UTF-8 (64 КиБ), длиннее — 422; принимается только в результате `collect_diagnostics`; управляющие символы, кроме перевода строки и табуляции, вычищаются на границе, как у текста ошибки (§11.3); значение хранится в `commands.result` (решение заказчика 2026-10-25, В-18)

#### Файл: `control-plane/app/domain/nodes.py`

- `rotate_identity(conn, node_id, presented, csr_pem, source_ip)` — исполнитель операции ротации:
  - сначала `FOR NO KEY UPDATE` строки ноды, затем запись identity (порядок блокировок — докстринг `app/domain/nodes.py`, страж — `tests/unit/domain/test_nodes.py`): иначе встречный обмен или отзыв поймают взаимную блокировку, а момент, взятый до блокировки, поставит ротацию «раньше» отзыва, которого она ждала;
  - под блокировкой заново проверить предъявившую identity: не отозвана и не истекла, нода не выведена и не `disabled`, иначе 401. Запрос опознан `current_node` в начале и мог ждать блокировку, которую держит отзыв (`revoke_identity`): без перепроверки скомпрометированная нода получила бы поколение N+1 без `revoked_at` после UC-12 шаг 1 (роаст 001.25);
  - лист — `InternalCA.sign_csr(csr_pem, *, node_id, now, days=90)` → `IssuedCert(pem, fingerprint, serial, not_before, not_after)`; `now` — `statement_timestamp()` оператора после блокировки строки ноды, а не `now()` начала транзакции (роаст 001.25, раунд 1); один источник времени у сертификата и строки identity; `serial` — в `node_identities.cert_serial` (форма `$ssl_client_serial`, карта отказа 001.66c);
  - новая строка `node_identities`: `generation` = max + 1, новый токен identity, `enrolled_from` — адрес запроса ротации;
  - прежние identity не отзываются (обмен токена по §4.5 «Пересоздание» отзывает их, ротация — нет): у каждой неотозванной прежней `expires_at` := min(`expires_at`, момент операции + 7 суток) — окно перекрытия Н-28 (В-16). Поиск ноды (`NodeService.by_identity`) принимает любую неотозванную и неистёкшую identity, поэтому окно держится только сроком
- `extend_overlap_windows(conn, at)`: при смене `state_generation` у неотозванных прежних поколений с открытым окном (`expires_at` > `at`) `expires_at` := `at` + 7 суток (В-16). Вызывает `check_generation_on_start` (001.67) в транзакции смены поколения. Отозванные identity не продлеваются: отзывы, сделанные после точки копии, `restore.sh` повторяет по внешнему журналу отзывов до открытия агентского порта (решение заказчика 2026-10-25, В-20; 001.67)

#### Файл: `control-plane/app/jobs/scheduler.py`

- `nodes.schedule_identity_rotation` раз в час: ноде, у которой до `expires_at` последнего поколения identity осталось 30 суток и меньше, ставится `rotate_credentials`, если у неё нет такой команды в статусах `issued` и `delivered`; ноды `pending`, выведенные и с закрытым каналом команд (§4.6: `disabled`, `suspended`) пропускаются; просроченная команда ставится заново следующим прогоном (В-16)

#### Файл: `control-plane/app/api/admin/nodes.py`

- `POST /admin/nodes/{id}/update-agent` — разрешение `nodes.update_agent`, Admin и выше; `POST /admin/nodes/{id}/restart-xray` — разрешение `nodes.restart_xray`, Operator и выше; `POST /admin/nodes/{id}/commands` с типом и полезной нагрузкой для `rotate_credentials` и `collect_diagnostics` — разрешение `nodes.commands`, Admin и выше: из команд Operator получает только `restart_xray` (решение заказчика 2026-10-25, В-39). Каждая операция декларирует одно разрешение (`interfaces.md` §5.1)
- чтение команд ноды и их результатов (В-18 б: собранное возвращается в `output`; без маршрута чтения администратор его не видит): `GET /admin/nodes/{id}/commands` — команды ноды по убыванию `issued_at`: `command_id`, тип, статус, `issued_at`, `expires_at`, без `commands.result`; `GET /admin/nodes/{id}/commands/{command_id}` — команда с нагрузкой, статусом и `commands.result` (текст ошибки, `output` результата `collect_diagnostics`); команда другой ноды — 404. Разрешение обоих маршрутов — `nodes.read`, Operator и выше (`security.md` §7.1: «команды и их результаты»; `interfaces.md` §5.1). Экран результата в карточке ноды — 001.64

#### Файл: `contracts/agent_v1/README.md`, фикстуры `contracts/agent_v1/`

- Изменение контракта agent_v1: в фикстурах ответа состояния — команды `update_agent` с нагрузкой, `rotate_credentials` и `collect_diagnostics`; новая операция `POST /agent/v1/identity/rotate` — фикстура `identity-rotate.json` и раздел README (условия 401, окно перекрытия 7 суток, продление на 7 суток при смене `state_generation`); поле `output` результата — фикстура `command-result-diagnostics.json` и предел 64 КиБ в README

#### Файл: `deploy/nginx/nginx.conf`

- `POST /agent/v1/commands/{id}/result` — собственный `location` с `client_max_body_size` по самой широкой форме тела с `output` 64 КиБ с учётом экранирования JSON: общий предел агентского `server` (64 КиБ) такое тело не пропускает; порог сверяет гейт `tests/unit/test_proxy_contract.py`

### Интеграция компонентов

Канареечная процедура и проверка клиентов — операторская инструкция (001.71); здесь — механизм.
Выдачу команд в ответе long-poll, TTL и статусы реализует `CommandService` (001.76), поэтому
постановка команд опирается на него. Исполнение нагрузок на ноде — 001.81: обновление Xray — с
финальным чтением счётчиков; `rotate_credentials` — ключ, CSR, вызов операции ротации, хранение
прежнего поколения до конца окна; `collect_diagnostics` — сбор в `output`. Повторное
подтверждение прежним поколением при 401 — агент, 001.54. Результат `update_agent` переводит ноду
в `degraded` и выводит из него — 001.30 (решение заказчика 2026-10-25, В-15). Матрица разрешений
`nodes.restart_xray`, `nodes.commands`, `nodes.update_agent` — 001.48.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Постановка `update_agent`
   - Входные данные: `POST /admin/nodes/{id}/update-agent` для `component = xray` с закреплённой версией; затем с версией, не закреплённой в `settings`
   - Ожидаемый результат: команда поставлена с нагрузкой `{component, version, url, sha256}`; вторая — отказ, команда не создана
   - Примечание: просроченная команда и путь `expired` — 001.76
2. **TC-E2E-02:** Окно перекрытия identity
   - Входные данные: `POST /agent/v1/identity/rotate`; запросы со старым и новым сертификатом в течение 7 суток и после
   - Ожидаемый результат: оба 200 в окне; старый — 401 после окна; нода не переходит в `offline` (Н-28)
3. **TC-E2E-03:** Автоматическая постановка ротации
   - Входные данные: до конца срока identity 31 сутки, затем 30; повторный прогон при команде в `issued`; нода `disabled`
   - Ожидаемый результат: при 31 сутках команды нет; при 30 — одна `rotate_credentials`; повторный прогон второй команды не ставит; нода `disabled` пропущена (В-16)
4. **TC-E2E-04:** Ротация, ждавшая отзыв
   - Входные данные: отзыв identity держит блокировку ноды; запрос ротации той же identity
   - Ожидаемый результат: 401; нового поколения нет
5. **TC-E2E-05:** Продление окна при смене поколения
   - Входные данные: прежнее поколение в окне, прежнее с закрытым окном, отозванное; `extend_overlap_windows`
   - Ожидаемый результат: у первого `expires_at` = момент смены + 7 суток; два других без изменений
6. **TC-E2E-06:** Результат `collect_diagnostics`
   - Входные данные: результат с `output` 65 536 байт; с `output` 65 537 байт; `output` в результате `restart_xray`; тело с самой широкой формой `output` через прокси
   - Ожидаемый результат: первый — 204, `output` в `commands.result`; второй и третий — 422; через прокси — не 413 (В-18)
7. **TC-E2E-07:** Разрешения команд
   - Входные данные: Operator вызывает `restart-xray`, `update-agent` и `commands`
   - Ожидаемый результат: первый принят; два других — 403 (В-39)
8. **TC-E2E-08:** Чтение команд и результата
   - Входные данные: у ноды A — `collect_diagnostics` с результатом `output` и `restart_xray` без результата; запросы Operator к `GET /admin/nodes/{A}/commands` и к `GET /admin/nodes/{A}/commands/{command_id}`; запрос команды ноды A по пути ноды B; запрос Support
   - Ожидаемый результат: список — две команды по убыванию `issued_at`, без `output`; команда — статус `applied` и `output`, совпадающий с принятым; путь ноды B — 404; Support — 403 (В-18, В-39)

### Модульные тесты

1. **TC-UNIT-01:** Проверка полезной нагрузки `update_agent`
   - Проверяемая функция: `app/domain/commands.py::validate_update_payload`
   - Входные данные: верная/неверная сумма; неизвестный компонент; версия, не совпадающая с закреплённой
   - Ожидаемый результат: успех/ошибка; ошибка; ошибка

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_nodes.py tests/e2e/test_agent_api.py tests/unit/domain/test_nodes.py tests/unit/test_proxy_contract.py -k 'command or identity or rotat or diagnostics or proxy'`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Ротация identity не переводит ноду в `offline` (Н-28)
- [ ] `rotate_credentials` ставится автоматически, когда до конца срока identity 30 суток и меньше; окно перекрытия — 7 суток; смена `state_generation` продлевает открытое окно на 7 суток (В-16)
- [ ] Операция `POST /agent/v1/identity/rotate` объявлена в контракте, фикстуре и README
- [ ] Результат `collect_diagnostics` несёт `output` не больше 64 КиБ; прокси пропускает самую широкую форму тела (В-18)
- [ ] Команды ноды и их результаты, включая `output`, читаются маршрутами
  `GET /admin/nodes/{id}/commands` и `GET /admin/nodes/{id}/commands/{command_id}` с разрешением
  `nodes.read` (В-18)
- [ ] Версии Xray-core и sing-box закреплены в `settings` с датой проверки (О-8)
- [ ] Нагрузка `update_agent` указывает компонент (`agent` или `xray`); версия сверяется с закреплённой; маршрут `update-agent` декларирует одно разрешение
- [ ] Operator ставит из команд только `restart_xray` (В-39)

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.29, 001.25, 001.76. Приоритет: Medium. Оценка: 4 ч. Этап: 4 — парк нод и Node API.
