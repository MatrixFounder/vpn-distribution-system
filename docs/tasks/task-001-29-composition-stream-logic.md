# Задача 001.29: Поток состава: `updated_seq`, вентиль по статусу, `resync_required`

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-01 Ввод ноды в эксплуатацию
- UC-05 Исчерпание лимита и отзыв доступа
- UC-10 Восстановление Control Plane из резервной копии
- UC-11 Обновление парка нод
- UC-12 Компрометация ноды

Требования RTM: R-04.

<!-- contract:goal -->

## Цель задачи

Реализовать дельту потока состава по `docs/architectures/interfaces.md` §5.2 и `data-model.md`
§4.2.3: выделение `updated_seq` через `UPDATE nodes … RETURNING`, вентиль по статусу, продвижение
курсора и `resync_required`, расшифровку credentials только для ноды из identity. Перевести
публикацию состава на задачу очереди, поставленную в транзакции бизнес-операции; реализовать
публикацию конфигурации и состава ноды целиком, ротацию credentials пользователя и пользователей
скомпрометированной ноды (Н-32). Полный снапшот и поколение — 001.75b; удержание long-poll —
001.75c; подтверждение курсоров и снятие `resync_required` — 001.75; команды — 001.76.

<!-- contract:changes -->

## Описание изменений

### Изменения в существующих файлах

#### Файл: `control-plane/tests/e2e/test_agent_api.py`

- дополнить: дельта; вентиль для каждого из восьми статусов

#### Файл: `control-plane/app/domain/composition.py`

- постановка публикации: бизнес-операция ставит задачу `composition.publish_user` в своей транзакции (`enqueue(conn, …)`, очередь `critical`, ключ `publish_user:{user_id}:{xid}`); прямой вызов публикации после коммита и из транзакции, держащей строку ноды (приём отчёта), не допускается — только постановка задачи
- `publish_user` (исполнитель задачи): для каждой ноды из пересечения групп доступа — upsert `node_user_state` (состояние, `quota_grant_bytes`, `blocked_ips`) с `updated_seq` из `UPDATE nodes SET desired_users_seq = desired_users_seq + 1 … RETURNING`; создание `node_user_credentials` при отсутствии; для пользователя с `users.status = 'deleted'` — только строки `removed`; `NOTIFY` + `PUBLISH node:{id}` после коммита
- `remove_user(conn, user_id)`: в транзакции вызывающего постановка `composition.publish_user`; исполнитель для `users.status = 'deleted'` пишет только строки `removed` с `updated_seq` через `UPDATE nodes … RETURNING`
- `publish_config(conn, node_id)`: строка `node_config_versions`, `desired_config_version`, `PUBLISH node:{id}` после коммита
- `publish_node(node_id)` — исполнитель задачи `composition.publish_node` (очередь `critical`, ключ `publish_node:{node_id}:{xid}` по образцу `publish_user`; задачу ставит вызывающий в своей транзакции): состав ноды целиком — при первом переходе в `active` (001.30), при смене групп тарифа и групп доступа ноды (изменение записи ноды — 001.87)
- `rotate_user_credentials(conn, user_id)`: в транзакции вызывающего новые `vless_uuid_enc`/`trojan_password_enc` и `version + 1` в `node_user_credentials` на всех нодах пользователя и постановка `composition.publish_user` (AC-35); имя `rotate_credentials` занято командой ротации identity ноды (001.31)
- `rotate_node_credentials(node_id)`: ротация credentials всех пользователей ноды после отзыва её identity по компрометации (UC-12 шаги 3–4; Н-32 — не позднее 4 ч, до 1 000 записей на ноду), задача очереди `critical` с ключом идемпотентности `rotate_node_credentials:{node_id}:{xid}` — по ноде и транзакции отзыва (`interfaces.md` §5.4)
- `state_for` (дельта): вентиль по таблице §5.2 (`pending` 403; `provisioning` только конфигурация; `disabled`/`suspended` только отзывы), продвижение курсора до max `updated_seq` выборки, расшифровка credentials только для `node_id` из identity
- `resync_required`: при выдаче отфильтрованного ответа взводится колонка `nodes.resync_required`, в ответ идёт значение колонки, а не признак, выведенный из статуса (§5.2 требует показать его **при выходе** из фильтрующего статуса; заглушка 001.28 отдаёт `node.resync_required or gate.filters_composition`); снятие — 001.75
- Изменение принятого кода (отступление от сигнатур заглушки 001.28): `publish_config` и `remove_user` принимают `conn` вызывающего и работают в его транзакции

#### Файл: `control-plane/app/jobs/handlers/composition.py`

- обработчики `composition.publish_user` (вместо заглушки, завершающей задачу успехом), `composition.publish_node` и `composition.rotate_node_credentials` в очереди `critical`

#### Файл: `control-plane/app/jobs/scheduler.py`

- уплотнение: строки `removed` удаляются после того, как `applied_users_seq` ноды достиг их `updated_seq` (`data-model.md` §4.2.3)

#### Файл: `control-plane/app/domain/subscriptions.py`

- Изменение принятого кода (отступление от 001.22): шесть вызовов `CompositionService.publish_user` после коммита заменяются постановкой задачи `composition.publish_user` в транзакции перехода (`interfaces.md` §5.4: `INSERT INTO jobs` в транзакции бизнес-операции; при крахе между коммитом и прямым вызовом отзыв терялся бы — Н-14). Довод докстринга модуля — ключ `publish:{node_id}:{users_seq}` при постановке неизвестен — снимается ключом `publish_user:{user_id}:{xid}`; докстринги модуля и обработчика привести к этому

#### Файл: `control-plane/app/domain/nodes.py`

- Изменение принятого кода (отступление от 001.25): `approve` вызывает `publish_config(conn, node_id)` — первая версия конфигурации (иначе `desired_config_version` остаётся 0 и нода в `provisioning` конфигурацию не получает); `revoke_identity` в своей транзакции ставит задачу `composition.rotate_node_credentials` (отзыв по компрометации; 001.87 выносит тело отзыва в общий шаг `_revoke(conn, node_id, at, *, compromise)`: постановка — при `compromise=True`, вывод ноды без компрометации, UC-12 A2, задачу не ставит); `enroll` при обмене нового токена (§4.5 «Пересоздание») сбрасывает `applied_config_version`, `applied_users_seq`, `applied_at`, `missed_heartbeats`, `ok_heartbeats` — новый агент начинает с нуля, и правило «курсор ноды впереди сервера» (409) не срабатывает

#### Файл: `control-plane/app/domain/plans.py`

- Изменение принятого кода (отступление от 001.19): `PlanService._set_composition` ставит задачу `composition.publish_node` нод групп, вошедших в состав тарифа или вышедших из него

### Интеграция компонентов

Задачу `composition.publish_user` ставят `SubscriptionService` (001.22), `LimitsService` (001.35),
`DeviceLimitService` (001.38) и перевыпуск токена (001.42). `NodeService.approve` → `publish_config`
(первая версия конфигурации); `StatusService` (001.30) при первом `active`, `PlanService`
(001.19) при смене групп тарифа и изменение записи ноды (001.87) при смене её групп доступа
ставят задачу `composition.publish_node`. `rotate_user_credentials` (`reissue` 001.42, панель
001.50) и `remove_user` (`delete_account` 001.17) вызываются с `conn` вызывающего и ставят
`composition.publish_user` в его транзакции. Пробуждение long-poll — Redis pub/sub.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Публикация доставляет строку дельтой
   - Входные данные: перевод пользователя в `suspended_quota`
   - Ожидаемый результат: задача `composition.publish_user` стоит в `jobs` в транзакции перехода; после её исполнения дельта содержит строку со состоянием, `users.seq` продвинут, `PUBLISH node:{id}` отправлен
   - Примечание: замер «ответ удержанному запросу не позднее 5 с» (Н-13) — 001.75c
2. **TC-E2E-02:** В `provisioning` состав пуст
   - Входные данные: нода `provisioning`
   - Ожидаемый результат: `users.rows == []`; ни одного credential (AC-13)
3. **TC-E2E-03:** `suspended` получает только отзывы
   - Входные данные: нода `suspended`; в потоке отзыв и выдача
   - Ожидаемый результат: в ответе только отзыв; `resync_required = true`
4. **TC-E2E-04:** Подтверждение ноды публикует конфигурацию
   - Входные данные: `NodeService.approve`
   - Ожидаемый результат: строка `node_config_versions` с первой версией; нода в `provisioning` получает конфигурацию
5. **TC-E2E-05:** Пересоздание сбрасывает курсоры
   - Входные данные: нода `active` с подтверждёнными курсорами; обмен нового токена
   - Ожидаемый результат: `applied_*`, `missed_heartbeats`, `ok_heartbeats` сброшены; условие выдачи `applied_users_seq ≥ updated_seq` (001.44) не выполняется, пока новый агент не подтвердил `users_seq`; запрос нового агента с нулевыми курсорами не получает 409
6. **TC-E2E-06:** Ротация credentials после компрометации
   - Входные данные: 1 000 пользователей на ноде; `revoke_identity`
   - Ожидаемый результат: задача `composition.rotate_node_credentials` поставлена в транзакции отзыва; у всех строк `node_user_credentials` ноды `version + 1`, строки опубликованы; время исполнения записано и не больше 4 ч (Н-32, AC-14)
7. **TC-E2E-07:** Перевыпуск credentials пользователя
   - Входные данные: `rotate_user_credentials(conn, user_id)`
   - Ожидаемый результат: новая версия credentials на всех нодах пользователя; строки в дельте (AC-35)
8. **TC-E2E-08:** Удалённый пользователь не получает доступ
   - Входные данные: `users.status = 'deleted'`; `publish_user`
   - Ожидаемый результат: на всех нодах только строки `removed`
9. **TC-E2E-09:** Уплотнение строк `removed`
   - Входные данные: строка `removed`; подтверждение `applied_users_seq` ниже, затем не ниже её `updated_seq`
   - Ожидаемый результат: строка сохраняется до подтверждения и удаляется после него

### Модульные тесты

1. **TC-UNIT-01:** Выделение `updated_seq` монотонно
   - Проверяемая функция: `app/domain/composition.py::next_users_seq`
   - Входные данные: две конкурентные транзакции
   - Ожидаемый результат: коммиты упорядочены; агент не пропускает строк (тест с задержкой коммита)

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_agent_api.py tests/unit/domain/test_composition.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Вентиль совпадает с матрицей §4.6 построчно
- [ ] `updated_seq` выделяется только через `UPDATE nodes … RETURNING`
- [ ] Credentials расшифровываются только для ноды из identity
- [ ] Публикация состава — задача очереди `critical` в транзакции бизнес-операции; прямых вызовов `publish_user` после коммита в `SubscriptionService` не осталось
- [ ] Отзыв identity запускает ротацию credentials пользователей ноды в пределах Н-32
- [ ] Обмен нового токена сбрасывает курсоры `applied_*` и счётчики heartbeat

## Примечания

`tdd-strict`: контракт `/agent/v1` — ядро системы.

Добавлена зависимость от 001.25 (найдено при 001.28): критерий приёмки «credentials
расшифровываются только для ноды из identity» невыполним, пока `current_node` — заглушка,
отдающая фиксированную карточку. Без этого ребра настоящие credentials поехали бы поверх
аутентификации, принимающей любой токен нужной формы, а отзыв identity (Н-31) не применялся бы
нигде.

Найдено при 001.28 (заглушки), решить здесь:

- вентиль по статусу §4.6 уже в коде как данные (`app/domain/statuses.py::STREAM_GATE`) и сверен
  с матрицей влияния построчно в `tests/unit/domain/test_statuses.py`, а поведение выдачи — по
  всем восьми статусам в обеих формах запроса (`tests/e2e/test_agent_api.py`). Критерий приёмки
  «вентиль совпадает с матрицей §4.6 построчно» этой частью закрыт: здесь остаётся привязать его
  к настоящим строкам `node_user_state`, а не к фиксированным;
- курсор продвигается по выборке **до** фильтра по статусу — иначе нода застревает всякий раз,
  когда самое свежее изменение вентиль скрыл; свойство закреплено фиксированными данными, где
  самое свежее изменение доступ не снимает;
- `users.full` в ответе — разрешение ноде удалять отсутствующих: любой урезанный ответ (вентиль,
  будущий предел размера выборки) обязан идти с `false`.

Зависимости: 001.28, 001.22, 001.25, 001.26. Приоритет: Critical. Оценка: 4 ч. Этап: 4 — парк нод и Node
API.
