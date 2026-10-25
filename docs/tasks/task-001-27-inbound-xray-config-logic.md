# Задача 001.27: Inbound: генерация конфигурации Xray, ротация ключей, проверка цели

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-01 Ввод ноды в эксплуатацию
- UC-12 Компрометация ноды

Требования RTM: R-03, R-28.

<!-- contract:goal -->

## Цель задачи

Реализовать генерацию полной конфигурации Xray по `docs/idea.md` §4.4 с `xver = 0` и
рандомизацией `limitFallback*` на ноду, создание inbound при создании ноды, операции панели над
inbound, ротацию `short_ids` с окном перекрытия (Н-33), замену ключевой пары и `short_ids` при
компрометации с уведомлением пользователей ноды и приём результатов проверки `reality_target` от
агента (Н-30, `docs/idea.md` §4.7).

<!-- contract:changes -->

## Описание изменений

### Изменения в существующих файлах

#### Файл: `control-plane/tests/unit/domain/test_xray_config.py`

- дополнить (файл заводит 001.26): golden-файлы для трёх профилей; проверка отсутствия credentials в конфигурации

#### Файл: `control-plane/app/domain/inbounds.py`

- `create_defaults(conn, node_id)`: ключевая пара, `short_ids` (hex, чётная длина ≤ 16), `spider_x`, `min_client_ver` из `settings`, `xver = 0` на всех нодах — сторонний сайт-цель PROXY protocol не принимает, и fallback для зондирующих ломался бы (решение заказчика 2026-10-25, В-17); рандомизируются только `limitFallbackUpload`/`limitFallbackDownload` — объекты `{after_bytes, bytes_per_sec, burst_bytes_per_sec}` на направление в `inbounds.params` (jsonb, ключи `limit_fallback_upload` и `limit_fallback_download`, `data-model.md` §4.2.3): колонки `reality_limit_fb_up`/`reality_limit_fb_down` (`int`, миграция 060) объект из трёх полей не вмещают и не используются
- `rotate_short_ids`: добавление нового значения, удаление прежнего через 24 часа задачей (Н-33)
- `rotate_keypair(reason=compromise)`: новая пара и новые `short_ids` без окна перекрытия (прежние недействительны сразу); в той же транзакции — событие «Адрес или параметры ноды изменены» (`node_address_changed`, `emit` 001.51, `payload={"kind": "params"}`) строкой на каждого пользователя ноды; пользователь ноды — строка `node_user_state` ноды в состоянии, отличном от `removed` (001.29), тот же набор, что у смены адреса (001.87); ключ `dedup_key` — формула 001.51 с версией конфигурации, выпущенной заменой; отдельного типа события нет (решение заказчика 2026-10-25, В-41). Запуск — из обработчика задачи `composition.rotate_node_credentials` (001.29), которую ставит отзыв identity ноды по компрометации (UC-12 шаги 3–4; общий шаг отзыва `_revoke(…, compromise=True)` — 001.87; вывод ноды без компрометации, UC-12 A2, эту задачу не ставит): пары всех inbound ноды заменяются одной версией конфигурации и одним событием на пользователя
- `set_target(inbound_id, target, server_names)`: проверка свойств цели с Control Plane (TLS 1.3 + HTTP/2 + без перенаправления), при отказе цель не меняется; новая версия конфигурации; ключевая пара сохраняется (§4.4)
- `set_enabled`: новая версия конфигурации
- `record_target_check(inbound_id, ok, reason)`: результат проверки цели агентом — запись `error_state`/`error_since` при отказе, очистка при успехе; статус ноды здесь не меняется; признак читает ряд `control_plane_inbounds_error{node_id}`

#### Файл: `control-plane/app/domain/nodes.py`

- Изменение принятого кода (отступление от 001.25): `NodeService.create` вызывает `InboundService.create_defaults(conn, node_id)` в своей транзакции — три строки `inbounds` и `inbound_secrets` с зашифрованным ключом; при пересоздании без компрометации ключевая пара сохраняется (§4.4). Докстринг `create` («Inbound с ключами REALITY (шаг 2) — 001.26») переадресовать на 001.27. Тело создания ноды получает обязательное поле `new_conn_rate_per_ip` (В-10)

#### Файл: `control-plane/app/domain/xray_config.py`

- полная сборка по профилям; `limitFallbackUpload`/`limitFallbackDownload` — объекты из `inbounds.params`; `config_version` увеличивается через `CompositionService.publish_config(conn, node_id)` (001.29) в транзакции изменения

#### Файл: `control-plane/app/domain/composition.py`

- `max_conn_per_ip` и `new_conn_rate_per_ip` раздела `agent` ответа состояния (контракт 001.26) — из одноимённых колонок `nodes`; смена значения выпускает новую версию раздела `agent`, а не новую версию конфигурации Xray. Значения меняет изменение записи ноды (001.87)

#### Файлы: миграция `control-plane/migrations/NNN_*.sql` и её откат

- колонка `nodes.new_conn_rate_per_ip int CHECK (> 0)` — предел частоты новых входящих соединений с одного адреса клиента на порты inbound, соединений в секунду (nftables meter, правило — 001.86); значения по умолчанию нет, поле обязательное (решение заказчика 2026-10-25, В-10)

#### Файл: `control-plane/app/api/admin/nodes.py`

- логика маршрутов над inbound (заглушки 001.26): `GET /admin/nodes/{id}/inbounds` → список inbound ноды с `error_state`/`error_since`, `PATCH /admin/inbounds/{id}` → `set_target`/`set_enabled`, `rotate-short-ids` → `rotate_short_ids`, `rotate-keypair` → `rotate_keypair`

#### Файл: `control-plane/app/metrics.py`

- ряд `control_plane_inbounds_error{node_id}` — число inbound ноды с заполненным `error_state`; источник правила алерта «Цель `reality_target` недоступна», приоритет «Отложенный» (§17.4; правило — 001.68)

#### Файл: `control-plane/app/jobs/handlers/composition.py`

- дополнить (обработчик заводит 001.29): `composition.rotate_node_credentials` вызывает `rotate_keypair(reason=compromise)` для всех inbound ноды в одной транзакции: одна версия конфигурации, одно событие на пользователя (В-41)

### Интеграция компонентов

Перевыпуск конфигурации → новая строка `node_config_versions` и `NOTIFY`/`PUBLISH` для long-poll
(001.29). Доступность цели проверяет агент раз в час и при вводе ноды (`docs/idea.md` §4.7,
001.59) и присылает результат в теле метрик (раздел `targets`, контракт и приём — 001.30, который
вызывает `record_target_check`). Статус ноды по `error_state` выбирает `StatusService` (001.30):
нода в `provisioning` с inbound в ошибке становится `active` по §4.6, затем `degraded`. Inbound в
`error_state` в выдачу подписки не попадает, остальные профили ноды выдаются (фильтр 001.44;
решение заказчика 2026-10-25, В-12). Правило алерта «Цель `reality_target` недоступна» — 001.68.
Событие «Адрес или параметры ноды изменены» при смене адреса ноды порождает изменение записи
ноды (001.87), при замене ключевой пары — эта задача; доставка — 001.52.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Недоступная цель переводит inbound в ошибку
   - Входные данные: результат проверки агента `ok = false` (`record_target_check`), затем `ok = true`
   - Ожидаемый результат: `error_state` и `error_since` заполнены, ряд `control_plane_inbounds_error{node_id}` в `/metrics` равен 1; после успешной проверки признак снят, ряд равен 0; статус ноды задачей не меняется (AC-38)
2. **TC-E2E-02:** Ротация short_id без разрыва
   - Входные данные: `rotate_short_ids`
   - Ожидаемый результат: в конфигурации два значения 24 часа; затем одно
3. **TC-E2E-03:** Создание ноды создаёт три inbound
   - Входные данные: `NodeService.create`
   - Ожидаемый результат: в той же транзакции три строки `inbounds` с разными портами и `inbound_secrets` с зашифрованным ключом; у всех `reality_xver = 0`; `limitFallback*` двух созданных нод различаются (В-17)
4. **TC-E2E-04:** Компрометация заменяет пару и `short_ids` без окна
   - Входные данные: `rotate_keypair(reason=compromise)` у ноды с тремя строками `node_user_state` (две `active`, одна `removed`)
   - Ожидаемый результат: новая ключевая пара; прежних `short_ids` нет в следующей версии конфигурации; в `events` две строки `node_address_changed` с `payload.kind = params` — по одной на строку не `removed`; повтор операции новых строк не добавляет (В-41)
5. **TC-E2E-05:** Смена цели администратором
   - Входные данные: `PATCH /admin/inbounds/{id}` с новой целью
   - Ожидаемый результат: новая версия конфигурации; ключевая пара прежняя; цель без TLS 1.3 — отказ, цель не изменена

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

- [ ] `xver = 0` на всех нодах; значения `limitFallback*` различаются между нодами (В-17)
- [ ] Компрометация заменяет ключевую пару и `short_ids` и порождает событие «Адрес или параметры ноды изменены» для пользователей ноды (В-41); пересоздание без компрометации сохраняет пару
- [ ] Создание ноды создаёт три inbound в своей транзакции
- [ ] AC-38: результат проверки цели от агента переводит inbound в ошибку; периодичность Н-30 обеспечивает агент (001.59)

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.26, 001.29, 001.51. Приоритет: Critical. Оценка: 4 ч. Этап: 4 — парк нод и Node
API.
