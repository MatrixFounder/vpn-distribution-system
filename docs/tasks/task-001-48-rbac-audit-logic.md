# Задача 001.48: RBAC: матрица и политики §5.11; Audit Log append-only

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-09 Управление тарифами, группами и кодами
- UC-13 Поддержка пользователя

Требования RTM: R-35, R-37.

<!-- contract:goal -->

## Цель задачи

Реализовать матрицу «роль × операция» (решение заказчика 2026-10-25, В-39; ОВ-26 закрыт), политики
просмотра subscription URL, режим работы от лица пользователя только для чтения (В-36), второе
подтверждение начисления трафика и продления подписки сверх порогов (В-34; ОВ-17 закрыт) и запись
Audit Log с составом §4.16 источника.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/tests/e2e/test_admin_auth.py` — события таблицы ниже из этой задачи, 001.46, 001.47 и принятого кода попадают в журнал с актором и ролью (AC-15); `UPDATE audit_log` от `app_rw` → ошибка прав (AC-39)
- `control-plane/migrations/NNN_approval_requests.sql` и откат `.rollback.sql` (NNN — следующий
  свободный номер): таблица `approval_requests` (`id`, `operation` — `traffic` | `renew`,
  `user_id`, `payload jsonb`, `requested_by`, `approved_by NULL`, `status` — `pending` | `approved`
  | `expired`, `created_at`, `expires_at`, `decided_at NULL`) и сид ключей `settings`
  `approval.traffic_bytes` и `approval.renew_days` со значением `null` — порог выключен
- `control-plane/app/domain/approvals.py` — `class ApprovalService`: `submit(operation, user_id,
  payload, ctx)` — ниже порога или при выключенном пороге выполняет операцию
  (`SubscriptionService.add_traffic` или `renew`, 001.22), сверх порога создаёт запрос;
  `approve(request_id, ctx)`. Действия `…/traffic` и `…/renew` вызывают `submit` (001.50)

### Изменения в существующих файлах

#### Файл: `control-plane/app/security/rbac.py`

- матрица для четырёх ролей (решение заказчика 2026-10-25, В-39; разделы —
  `docs/architectures/interfaces.md` §5.1):

  | Роль | Разрешения |
  | :--- | :--- |
  | Support | раздел Users: поиск и карточка (`users.read`), действия карточки (`users.write`), `users.view_subscription_url` и `users.impersonate`; дашборд (`dashboard.read`); список и подтверждение запросов второго подтверждения (`approvals.approve`); свой профиль администратора (`admins.self`, 001.89) |
  | Operator | разрешения Support; чтение нод (`nodes.read`, включая доступность по странам `GET /admin/nodes/{id}/availability`), ручные статусы ноды (`nodes.manual_status`; маршрут — 001.30b), команда `restart_xray` (`nodes.restart_xray`; маршрут — 001.31) |
  | Admin | разрешения Operator; остальные операции нод: изменение записи и вывод (`nodes.write`; маршруты — 001.87), изменение и ротации inbound (`nodes.write`; 001.26; чтение inbound — `nodes.read`) и обращение провайдера (`nodes.write`; 001.69), отзыв identity (`nodes.revoke_identity`), `update_agent` (`nodes.update_agent`) и команды `rotate_credentials` и `collect_diagnostics` (`nodes.commands`; маршруты — 001.31); группы (`groups.read`, `groups.write`), тарифы (`plans.read`, `plans.write`), коды (`codes.read`, `codes.write`, `codes.export`), чтение журнала (`audit.read`) |
  | Super Admin | все разрешения, включая настройки и окно обслуживания (`settings.read`, `settings.write`; 001.85) и управление администраторами (`admins.manage`, 001.89) |

- матрица хранится в коде, а не в `settings`; разрешения на восстановление из копии нет — это
  процедура на хосте (§17.12, 001.67)

#### Файл: `control-plane/app/security/audit.py`

- запись всех полей §4.16 включая `impersonated_user_id`, `user_agent`, `result`; `old_value`/`new_value` без PII (маскирование адресов почты); чтение `/admin/audit` регистрируется
- для `actor_type = user` поля `ip` и `user_agent` не заполняются: это персональные данные (idea §16.1), а записи журнала персональных данных не содержат (idea §16.4, TASK UC-14)

#### Файл: `control-plane/app/api/admin/users.py`

- работа от лица пользователя (решение заказчика 2026-10-25, В-36) — расширенная карточка в
  панели с данными кабинета пользователя (`/me`) без входа в кабинет и без сессии пользователя;
  режим только для чтения. `POST /admin/users/{id}/impersonate` начинает режим: отметка в Redis по
  `sid` сессии администратора (TTL — как у сессии), запись журнала с `impersonated_user_id`,
  задача `send_email` вида `impersonation` пользователю (письмо — 001.78).
  `DELETE /admin/users/{id}/impersonate` завершает режим с записью журнала; начало режима для
  другого пользователя завершает прежний с той же записью. Начало без записи завершения означает
  выход или истечение сессии администратора. Данные карточки (`GET …/impersonate`) — 001.50.
  Пользователь с `users.status = 'deleted'` — 409 `user_deleted`
- второе подтверждение (решение заказчика 2026-10-25, В-34): порог начисления трафика —
  `settings.approval.traffic_bytes` (байты за одну операцию), порог продления —
  `settings.approval.renew_days` (длительность продления в днях); по умолчанию `null` — порог
  выключен (при одном операторе, О-6); включение и изменение — `PATCH /admin/settings` с записью
  в Audit Log (001.85). Операция сверх включённого порога не выполняется: создаётся строка
  `approval_requests` со сроком 24 ч, ответ — 202 `approval_required` с идентификатором запроса
- `GET /admin/users/{id}/subscription-url` (заглушка — 001.49): ссылки подписки на обоих доменах из `token_enc` (001.42), ответ с `Cache-Control: no-store`; каждый просмотр — запись `users.view_subscription_url` (§5.11, UC-13 шаг 5)
- `GET …/subscription-url` для пользователя с `users.status = 'deleted'` отвечает 409
  `user_deleted` и токена не выпускает (`ensure_token` 001.42 не вызывается): токены удалённого
  отозваны (001.17), действующего токена нет; тем же кодом отвечают действия раздела Users (001.50)

#### Файл: `control-plane/app/api/admin/approvals.py`

- реализация заглушек 001.49 через `ApprovalService`: `GET /admin/approvals` — запросы в статусе
  `pending`; `POST /admin/approvals/{id}/approve` — подтверждает другая активная учётная запись,
  у которой есть разрешение операции запроса; операция выполняется из `payload` в транзакции
  подтверждения, статус `approved`, `approved_by`, `decided_at`. Отказы: та же учётная запись — 409
  `approval_same_account`; нет разрешения операции — 403; срок `expires_at` прошёл — 409
  `approval_expired`, статус `expired`, операция не выполняется; статус не `pending` — 409
  `approval_not_pending`
- сценарий с числами: порог начисления включён, 100 ГБ. Support A начисляет 150 ГБ → 202, запрос R
  до T + 24 ч. Подтверждение R учётной записью A → 409. Operator B подтверждает R в T + 2 ч → 150 ГБ
  начислены. Повторное подтверждение R → 409 `approval_not_pending`. Начисление 50 ГБ выполняется
  сразу. Запрос R2, подтверждаемый в T + 25 ч, → 409 `approval_expired`, ничего не начислено.
  Порог действует на одну операцию (idea §5.11)

#### Таблица регистрируемых событий

| Событие §4.16 | Место вызова `audit.record` | `actor_type` |
| :--- | :--- | :--- |
| Создание пользователя | `UserService.register` (001.14) | `user` |
| Изменение пользователя | сброс credentials, ручное подтверждение адреса и другие действия раздела Users (001.50) | `admin` |
| Удаление пользователя | `ProfileService.delete_account` (001.17) | `user` |
| Создание, изменение и удаление тарифа | `PlanService.create`, `update`, `archive` (001.19) | `admin` |
| Создание, изменение и удаление группы | методы групп доступа и тарифицируемых групп `GroupService` (001.19) | `admin` |
| Создание промокода и партии кодов | `CodeService.create`, `create_batch` (001.20) | `admin` |
| Экспорт партии кодов | `GET /admin/codes/batch/{batch_id}/export` (вызов — 001.46) | `admin` |
| Создание и изменение ноды | `NodeService.create`, `approve` (001.25); ручной статус — `set_manual_status` (001.30b); изменение записи и вывод — `NodeService.update`, `decommission` (001.87); обращение провайдера — `POST /admin/nodes/{id}/provider-complaint` (001.69) | `admin` |
| Перевод ноды в `suspended` по повторным удержаниям отчётов | `check_held_reports` (001.30c) | `system` |
| Смена тарифа, продление, начисление трафика | `SubscriptionService.change_plan`, `renew`, `add_traffic` (001.22) из действий 001.50 | `admin` |
| Запрос и подтверждение операции сверх порога | `ApprovalService.submit`, `approve` (эта задача) из действий 001.50 и `/admin/approvals` | `admin` |
| Блокировка и разблокировка | действия раздела Users (001.50) | `admin` |
| Ручная корректировка баланса, включая уменьшение | корректировка и уменьшение баланса (001.50) | `admin` |
| Изменение коэффициента трафика | `GroupService.set_group_multiplier`, `set_billing_group` (вызов — 001.46) | `admin` |
| Изменение системных настроек, окно обслуживания | `PATCH /admin/settings`, `/admin/settings/maintenance` (001.85) | `admin` |
| Вход, неудачная попытка входа, выход администратора | `/admin/auth/*` (001.47) | `admin` |
| Сброс второго фактора администратора на хосте | `admin reset-2fa` (001.47) | `system` |
| Действия с учётными записями администраторов и свой профиль | действия `admins.*` раздела `/admin/admins` и своего профиля (001.89); `AdminService.block`, `reset_second_factor` (001.47) | `admin` |
| Просмотр subscription URL | `GET /admin/users/{id}/subscription-url` (эта задача) | `admin` |
| Перевыпуск subscription-токена | `SubscriptionTokenService.reissue` (001.42) из кабинета (001.16) | `user` |
| Начало и завершение работы от лица пользователя | `POST` и `DELETE /admin/users/{id}/impersonate` (эта задача) | `admin` |
| Выдача bootstrap-токена и отзыв identity ноды | `NodeService.issue_bootstrap_token`, `revoke_identity` (001.25) | `admin` |
| Обмен identity ноды | `NodeService.enroll` (001.25) | `system` |
| Чтение журнала | `GET /admin/audit` (эта задача) | `admin` |

#### Изменение принятого кода (отступление от 001.14, 001.19, 001.22, 001.25)

- методы этих задач из таблицы принимают `AuditContext` (001.46) и вызывают `audit.record` на
  подключении своей транзакции; маршруты передают контекст сессии. `UserService.register` пишет
  запись с `actor_type = user`, `NodeService.enroll` — с `actor_type = system`. Открытые задачи
  таблицы (001.16, 001.17, 001.20, 001.30b, 001.30c, 001.42, 001.47, 001.50, 001.69, 001.85,
  001.87, 001.89) вызывают `audit.record` сами.

### Интеграция компонентов

Триггер неизменяемости создан в 001.09; здесь — проверка и запись.

Маршруты `api/admin/users.py` объявляет 001.49 — эта задача от неё зависит. Ссылку для просмотра
строит `token_enc` задачи 001.42: по хешу `token_hash` URL не восстановить. Второе подтверждение к
начислению трафика и продлению подписки подключает 001.50. Маршруты `/admin/approvals` объявляет
001.49. Письмо `impersonation` отправляет обработчик `send_email` (001.78). Ключи `approval.*` и
таблица `approval_requests` описываются в `docs/architectures/data-model.md` §4.2.6.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Просмотр URL регистрируется
   - Входные данные: Support вызывает `GET /admin/users/{id}/subscription-url`
   - Ожидаемый результат: ссылки на обоих доменах; строка `audit_log` с действием
     `users.view_subscription_url` (UC-13)
2. **TC-E2E-02:** Журнал неизменяем
   - Входные данные: `UPDATE audit_log` под `app_rw`
   - Ожидаемый результат: ошибка прав (`InsufficientPrivilegeError`); триггер — вторая линия,
     проверен в 001.09 (`tests/e2e/test_schema_ops.py`) (AC-39)
3. **TC-E2E-03:** Второе подтверждение
   - Входные данные: `ApprovalService.submit` начисления 150 ГБ при выключенном пороге; порог
     включён (100 ГБ), `submit` 150 ГБ учётной записью A; `POST /admin/approvals/{id}/approve`
     учётной записью A, затем B; запрос, срок которого прошёл (фикстура сдвигает `expires_at`)
   - Ожидаемый результат: при выключенном пороге начисление выполнено сразу; при включённом —
     строка `approval_requests` в `pending`, баланс не изменён; подтверждение A — 409
     `approval_same_account`, B — начисление выполнено, статус `approved`; просроченный — 409
     `approval_expired`, баланс не изменён
4. **TC-E2E-04:** Записи источников вне панели
   - Входные данные: экспорт партии кодов; неудачный вход и выход администратора; регистрация
     пользователя; обмен identity ноды; `admin reset-2fa`
   - Ожидаемый результат: строки `audit_log` с действиями по таблице; у регистрации
     `actor_type = user`, `ip` и `user_agent` пусты; у обмена identity и сброса второго фактора
     `actor_type = system`
5. **TC-E2E-05:** Просмотр URL удалённого пользователя
   - Входные данные: пользователь с `users.status = 'deleted'` и отозванным токеном (фикстура
     записывает их в базу; `delete_account` 001.17 в зависимостях задачи нет); Support вызывает
     `GET /admin/users/{id}/subscription-url`
   - Ожидаемый результат: 409 `user_deleted`; строки `subscription_tokens` пользователя с
     `revoked_at IS NULL` нет
6. **TC-E2E-06:** Работа от лица пользователя
   - Входные данные: Support начинает режим для пользователя U (`POST …/impersonate`), затем
     начинает режим для пользователя V, затем завершает его (`DELETE …/impersonate`)
   - Ожидаемый результат: строки `audit_log` начала и завершения для U и V с
     `impersonated_user_id`; задачи `send_email` вида `impersonation` для U и для V; после
     завершения отметки режима в Redis нет
7. **TC-E2E-07:** Матрица ролей
   - Входные данные: сессии `support`, `operator`, `admin`, `super_admin`;
     `POST /admin/nodes/{id}/status`, `POST /admin/nodes/{id}/revoke-identity`,
     `GET /admin/approvals`, `GET /admin/users/{id}`,
     `GET /admin/nodes/{id}/availability`, `POST /admin/nodes/{id}/provider-complaint`,
     `GET /admin/settings`, `POST /admin/settings/maintenance`
   - Ожидаемый результат: ручной статус — 403 у `support`, не 403 у `operator` и `admin`; отзыв
     identity — 403 у `support` и `operator`, не 403 у `admin`; список запросов и карточка
     пользователя — не 403 у всех ролей; доступность — 403 у `support`, не 403 у `operator`;
     обращение провайдера — 403 у `operator`, не 403 у `admin`; настройки и окно обслуживания —
     403 у `admin`, не 403 у `super_admin`

### Модульные тесты

1. **TC-UNIT-01:** Маскирование PII
   - Проверяемая функция: `app/security/audit.py::mask`
   - Входные данные: словарь с `email`
   - Ожидаемый результат: адрес заменён на `user_id`

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_admin_auth.py -k 'audit or rbac'`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] AC-15, AC-39 выполнены
- [ ] События таблицы, источник которых — эта задача, 001.46, 001.47 или принятый код (001.14,
  001.19, 001.22, 001.25), пишут строку `audit_log`; строки остальных источников проверяют их задачи
- [ ] Записи с `actor_type = user` не содержат адреса и `User-Agent`
- [ ] Политики §5.11 реализованы
- [ ] Просмотр subscription URL удалённого пользователя отвечает 409 `user_deleted` без выпуска
  токена
- [ ] Матрица зафиксирована в коде и в `docs/architectures/security.md`: Operator — права Support,
  чтение нод и inbound, ручные статусы и `restart_xray`; отзыв identity, `update_agent`, изменение
  и ротации inbound и обращение провайдера — Admin и выше; настройки и окно обслуживания — Super Admin; у каждого
  маршрута панели — названное разрешение реестра (TC-E2E-07)
- [ ] Пороги второго подтверждения по умолчанию выключены; запрос живёт 24 ч; подтверждает другая
  активная учётная запись с разрешением операции
- [ ] Работа от лица пользователя — расширенная карточка только для чтения; начало и завершение в
  журнале; пользователю ставится письмо

## Примечания

Роли — матрица выше (ОВ-26 закрыт, В-39); пороги второго подтверждения — ключи `approval.*`
(ОВ-17 закрыт, В-34).

Зависимости: 001.46, 001.47, 001.42, 001.49. Приоритет: Critical. Оценка: 5 ч. Этап: 7 —
администрирование, RBAC, аудит, поддержка.
