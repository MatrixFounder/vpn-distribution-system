# Задача 001.46: Аутентификация администраторов, RBAC и аудит — интерфейсы и заглушки

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-09 Управление тарифами, группами и кодами
- UC-13 Поддержка пользователя

Требования RTM: R-23, R-35, R-36, R-37.

<!-- contract:goal -->

## Цель задачи

Объявить `/api/v1/admin/auth/*`, реестр разрешений, зависимость `require_permission` и
`audit.record` с заглушками, чтобы административные маршруты собирались с проверкой прав до
реализации.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/api/admin/auth.py` — `POST /admin/auth/login`, `POST /admin/auth/totp`, `POST /admin/auth/recovery`, `POST /admin/auth/logout`, `GET /admin/auth/session` (роль и список разрешений текущего администратора — по нему панель скрывает недоступные действия, 001.64; язык панели `language` из `admin_users.language` — выбор языка в своём профиле, 001.89)
- `control-plane/app/security/rbac.py` — `PERMISSIONS: dict[str, set[Role]]` — полный перечень
  разрешений (роли — матрица 001.48): имена `x-permission` принятых маршрутов (`nodes.read`,
  `nodes.write` и др.) и новые разрешения:
  - раздел Users и поддержка: `users.read` (поиск и карточка), `users.write` (действия карточки:
    блокировка и разблокировка, тариф, продление, начисление и корректировка баланса, сброс
    credentials, ручное подтверждение адреса), `users.view_subscription_url`, `users.impersonate` (начало, данные и
    завершение режима), `approvals.approve` (список запросов `GET /admin/approvals` и их
    подтверждение: кто подтверждает, тот видит список) (001.48, 001.49);
  - настройки: `settings.read` (`GET /admin/settings`, действующие окна обслуживания) и
    `settings.write` (`PATCH /admin/settings`, создание и снятие окна обслуживания) — 001.49,
    001.85;
  - коды и журнал: `codes.export`, `audit.read`;
  - ноды: `nodes.revoke_identity`, `nodes.manual_status` (ручные статусы, 001.30b),
    `nodes.update_agent`, `nodes.restart_xray`, `nodes.commands` (команды `rotate_credentials` и
    `collect_diagnostics`, 001.31); изменение записи и вывод ноды (`PATCH` и
    `DELETE /admin/nodes/{id}`, 001.87) остаются под `nodes.write`; доступность ноды по странам
    (`GET /admin/nodes/{id}/availability`, 001.49) — `nodes.read`; обращение провайдера
    (`POST /admin/nodes/{id}/provider-complaint`, 001.69) и изменение и ротации inbound (001.26) —
    `nodes.write`; чтение inbound (`GET /admin/nodes/{id}/inbounds`, 001.26) — `nodes.read`;
  - учётные записи администраторов: `admins.manage` (раздел `/admin/admins`) и `admins.self`
    (свой профиль) — маршруты 001.49, логика 001.89.

  Заглушка — все разрешения у `super_admin`. Разрешения на восстановление из копии нет: это
  процедура на хосте (§17.12, 001.67), а не операция API
- `control-plane/app/security/totp.py` — `class Totp`: `provision(admin_id) -> uri`; `verify(admin_id, code) -> bool`; `recovery_codes(admin_id) -> list[str]` — заглушки
- `control-plane/app/security/audit.py` — `AuditContext` (актор, роль, сессия, адрес, `User-Agent`, `impersonated`); `async def record(conn, ctx, action, entity, old, new, result)` — заглушка без записи
- `control-plane/tests/e2e/test_admin_auth.py` — вход, второй фактор, отказ без разрешения — на заглушках

### Изменения в существующих файлах

#### Файл: `control-plane/app/security/deps.py`

- `require_permission(name)` — на месте заглушки (сейчас `NotImplementedError`): проверка роли сессии по `PERMISSIONS`; `rbac.py` держит только реестр

#### Файл: `control-plane/app/domain/groups.py` — изменение принятого кода (отступление от 001.19)

- `GroupService.set_group_multiplier` (интервал коэффициента группы) и
  `GroupService.set_billing_group` (тарифицируемая группа ноды и её переопределение) принимают
  `AuditContext` и вызывают `audit.record` с прежним и новым значением коэффициента (§4.16, UC-09
  шаг 7, R-23). Прежние значения берутся из закрытой строки истории (001.23), у `Multiplier` —
  поле `previous_multiplier`.
- Маршрут, вызывающий `set_billing_group` (изменение записи ноды), — задача 001.87.

#### Изменение принятого кода (отступление от 001.18 и 001.24)

- `control-plane/app/api/admin/codes.py`: у экспорта партии кодов `x-permission` `codes.read` →
  `codes.export`; экспорт вызывает `audit.record` (строку `audit_log` проверяет 001.48);
- `control-plane/app/api/admin/nodes.py`: у отзыва identity `x-permission` `nodes.write` →
  `nodes.revoke_identity`; у ручного статуса ноды (`POST /admin/nodes/{id}/status`)
  `nodes.write` → `nodes.manual_status`: ручные статусы выполняет и Operator, остальные операции
  записи нод — Admin и выше (решение заказчика 2026-10-25, В-39).

### Интеграция компонентов

Все маршруты `/admin/*` из 001.18 и 001.24 используют `require_permission`; 001.49 подключает свои
маршруты сама.

Механизм записи журнала: доменный метод, меняющий данные, принимает `ctx: AuditContext` и
вызывает `audit.record` на подключении своей транзакции — изменение и его след в журнале атомарны.
Транзакцию открывает сам метод (`async with transaction(self._pool)`), поэтому маршрут после
коммита записать журнал в той же транзакции не может. Отказ по разрешению (`result = denied`)
пишет `require_permission` отдельной короткой транзакцией. Перечень доменных методов, получающих
`AuditContext`, — 001.48.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Без второго фактора вход не завершён
   - Входные данные: `login` без `totp`
   - Ожидаемый результат: `202 totp_required`
   - Примечание: заглушка
2. **TC-E2E-02:** Отказ по разрешению
   - Входные данные: роль `support` вызывает `DELETE /admin/plans/{id}`
   - Ожидаемый результат: 403; заглушка `audit.record` вызвана с `denied`
3. **TC-E2E-03:** Сессия администратора
   - Входные данные: `GET /admin/auth/session` с сессией `support`
   - Ожидаемый результат: роль, список разрешений и язык панели сессии
   - Примечание: заглушка
4. **TC-E2E-04:** Разрешения принятых маршрутов
   - Входные данные: `/openapi.json`; смена коэффициента группы
   - Ожидаемый результат: у экспорта партии кодов `x-permission: codes.export`, у отзыва identity —
     `nodes.revoke_identity`, у ручного статуса ноды — `nodes.manual_status`;
     `set_group_multiplier` вызывает `audit.record` с прежним и новым значением

### Модульные тесты

- Не требуются: задача не содержит логики сверх заглушек или конфигурации.

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_admin_auth.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Реестр разрешений объявлен, включая `users.read`, `users.write`, `settings.read` и
  `settings.write`; каждый маршрут `/admin/*` декларирует одно разрешение
- [ ] Экспорт кодов объявляет `codes.export`, отзыв identity — `nodes.revoke_identity`, ручной
  статус ноды — `nodes.manual_status`
- [ ] `set_group_multiplier` и `set_billing_group` вызывают `audit.record` в своей транзакции
- [ ] Заглушки не пишут в базу

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.12, 001.09. Приоритет: Critical. Оценка: 3 ч. Этап: 7 — администрирование, RBAC,
аудит, поддержка.
