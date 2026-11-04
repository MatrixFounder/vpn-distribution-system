# Задача 001.29d: Ротация credentials пользователя и пользователей скомпрометированной ноды, сброс курсоров при пересоздании

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-01 Ввод ноды в эксплуатацию
- UC-12 Компрометация ноды

Требования RTM: R-04.

<!-- contract:goal -->

## Цель задачи

Реализовать ротацию credentials пользователя (AC-35) и пользователей скомпрометированной ноды
(Н-32, `interfaces.md` §5.4) и сброс курсоров ноды при обмене нового токена (§4.5
«Пересоздание»). Постановка публикации в транзакции — 001.29b; ключевая пара REALITY при
компрометации — 001.27.

<!-- contract:changes -->

## Описание изменений

### Изменения в существующих файлах

#### Файл: `control-plane/app/domain/composition.py`

- `rotate_user_credentials(conn, user_id)`: в транзакции вызывающего новые `vless_uuid_enc`/`trojan_password_enc` и `version + 1` в `node_user_credentials` на всех нодах пользователя и постановка `composition.publish_user` (AC-35); имя `rotate_credentials` занято командой ротации identity ноды (001.31)
- `rotate_node_credentials(node_id)`: ротация credentials всех пользователей ноды после отзыва её identity по компрометации (UC-12 шаги 3–4; Н-32 — не позднее 4 ч, до 1 000 записей на ноду), задача очереди `critical` с ключом идемпотентности `rotate_node_credentials:{node_id}:{xid}` — по ноде и транзакции отзыва (`interfaces.md` §5.4)

#### Файл: `control-plane/app/jobs/handlers/composition.py`

- обработчик `composition.rotate_node_credentials` в очереди `critical` (замену ключевой пары в нём добавляет 001.27)

#### Файл: `control-plane/app/domain/nodes.py`

- Изменение принятого кода (отступление от 001.25): `revoke_identity` в своей транзакции ставит задачу `composition.rotate_node_credentials` (отзыв по компрометации; 001.87 выносит тело отзыва в общий шаг `_revoke(conn, node_id, at, *, compromise)`: постановка — при `compromise=True`, вывод ноды без компрометации, UC-12 A2, задачу не ставит); `enroll` при обмене нового токена (§4.5 «Пересоздание») сбрасывает `applied_config_version`, `applied_users_seq`, `applied_at`, `missed_heartbeats`, `ok_heartbeats` — новый агент начинает с нуля, и правило «курсор ноды впереди сервера» (409) не срабатывает

- сериализация с `publish_user` (001.29, роаст раунд 3): `rotate_node_credentials` и `rotate_user_credentials` пишут credentials пар и публикуют строки — либо под блокировкой пользователя (`PUBLISH_LOCK_CLASS`, до блокировки строки ноды, ключи по возрастанию), либо постановкой `composition.publish_user`; правило — в 001.29b. Семантика `version`: после снятия пары запись credentials удаляется, и повторная выдача начинает с `version` 1 (README контракта, «Ключи в потоке состава»: нода применяет ключи по значению); сохранить `version` монотонной на пару — решить здесь

### Интеграция компонентов

`rotate_user_credentials` вызывают перевыпуск токена (`reissue` 001.42) и панель (001.50) с `conn`
вызывающего. `revoke_identity` (001.25; общий шаг отзыва — 001.87) ставит
`composition.rotate_node_credentials`; 001.27 добавляет в её обработчик `rotate_keypair(reason=compromise)`.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Пересоздание сбрасывает курсоры
   - Входные данные: нода `active` с подтверждёнными курсорами; обмен нового токена
   - Ожидаемый результат: `applied_*`, `missed_heartbeats`, `ok_heartbeats` сброшены; условие выдачи `applied_users_seq ≥ updated_seq` (001.44) не выполняется, пока новый агент не подтвердил `users_seq`; запрос нового агента с нулевыми курсорами не получает 409
2. **TC-E2E-02:** Ротация credentials после компрометации
   - Входные данные: 1 000 пользователей на ноде; `revoke_identity`
   - Ожидаемый результат: задача `composition.rotate_node_credentials` поставлена в транзакции отзыва; у всех строк `node_user_credentials` ноды `version + 1`, строки опубликованы; время исполнения записано и не больше 4 ч (Н-32, AC-14)
3. **TC-E2E-03:** Перевыпуск credentials пользователя
   - Входные данные: `rotate_user_credentials(conn, user_id)`
   - Ожидаемый результат: новая версия credentials на всех нодах пользователя; строки в дельте (AC-35)

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_composition.py tests/e2e/test_agent_api.py tests/e2e/test_nodes.py tests/unit/domain/test_composition.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Отзыв identity запускает ротацию credentials пользователей ноды в пределах Н-32
- [ ] Перевыпуск credentials пользователя даёт новую версию на всех его нодах (AC-35)
- [ ] Обмен нового токена сбрасывает курсоры `applied_*` и счётчики heartbeat

## Примечания

`tdd-strict`: контракт `/agent/v1` — ядро системы.

Выделена из 001.29 2026-11-04 по правилу 4 «Цикла задачи» (решение заказчика В-47 а).

Зависимости: 001.29b. Приоритет: Critical. Оценка: 2 ч. Этап: 4 — парк нод и Node API.
