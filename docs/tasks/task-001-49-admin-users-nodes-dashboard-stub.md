# Задача 001.49: API панели: пользователи, ноды, дашборд, настройки, данные поддержки — заглушки

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-13 Поддержка пользователя
- UC-09 Управление тарифами, группами и кодами

Требования RTM: R-06, R-34, R-38.

<!-- contract:goal -->

## Цель задачи

Объявить маршруты разделов §4.15 источника и карточки пользователя §17.5 со схемами и заглушками.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/api/admin/users.py` — `GET /admin/users?q=&filter=`, `GET /admin/users/{id}` (карточка: подписка, обращения к подписке, последнее подключение, потребление с коэффициентом, причина отзыва, пометка недоставляемого адреса `email_undeliverable_at`) — `x-permission: users.read`; `POST …/block|unblock|plan|renew|traffic|reset-credentials|verify-email` (`verify-email` — ручное подтверждение адреса, 001.50) — `x-permission: users.write`; `POST`, `GET` и `DELETE /admin/users/{id}/impersonate` (начало, данные расширенной карточки и завершение режима от лица пользователя — 001.48, 001.50) — `x-permission: users.impersonate`; `GET /admin/users/{id}/subscription-url` (`x-permission: users.view_subscription_url`; ответ — ссылки на обоих доменах подписки, `Cache-Control: no-store`)
- `control-plane/app/api/admin/approvals.py` — `GET /admin/approvals`, `POST /admin/approvals/{id}/approve` (оба — `x-permission: approvals.approve`: кто подтверждает, тот видит список): запросы второго подтверждения — заглушки (логика — 001.48)
- `control-plane/app/api/admin/admins.py` — раздел Administrators (idea §4.15; решение заказчика
  2026-10-25, В-33 в, В-35 в), заглушки без записи в базу (логика — 001.89):
  `GET /admin/admins`, `POST /admin/admins` (адрес, роль), `PATCH /admin/admins/{id}` (роль),
  `POST /admin/admins/{id}/block|unblock|reset-password|reset-2fa` — `x-permission: admins.manage`;
  свой профиль — `POST /admin/admins/me/password`, `POST /admin/admins/me/totp`,
  `POST /admin/admins/me/totp/confirm`, `POST /admin/admins/me/logout-all`,
  `PATCH /admin/admins/me` (язык панели `language`, 001.89) — `x-permission: admins.self`.
  Маршруты `me` объявляются раньше маршрутов `{id}`. Разрешения `admins.manage` и `admins.self`
  объявлены в реестре `PERMISSIONS` (001.46). Маршрут `unblock` и схемы ответов создания и сброса
  пароля — по ответам на вопросы 1 и 2 «решить здесь» 001.89; оба вопроса задаются заказчику до
  начала этой задачи, ответы вносятся в 001.89 и сюда
- `control-plane/app/api/admin/dashboard.py` — `GET /admin/dashboard` (`x-permission: dashboard.read`): счётчики §4.15
- `control-plane/app/api/admin/settings.py` — `GET /admin/settings` (`x-permission: settings.read`) и `PATCH /admin/settings` (`x-permission: settings.write`): режим регистрации, почта, CAPTCHA, пороги второго подтверждения (`approval.traffic_bytes`, `approval.renew_days`, 001.48), версии; ключа стратегии разрыва нет (константа 001.40, data-model §4.2.6); домены подписки — только в ответе `GET`: источник — окружение развёртывания `SUBSCRIPTION_DOMAINS`, замена домена — процедура развёртывания (решение заказчика 2026-10-25, В-27); схема `PATCH` строится по перечню ключей `settings` в `docs/architectures/data-model.md` §4.2.6 (ключ, тип и границы, задача-владелец, кто меняет — панель или миграция); пункт «обслуживание» — `GET /admin/settings/maintenance` (`settings.read`), `POST /admin/settings/maintenance` и `DELETE /admin/settings/maintenance/{silence_id}` (`settings.write`): действующие окна, создание окна (начало, конец, комментарий) и его снятие; ключа в `settings` нет, окно — `silence` в Alertmanager (логика — 001.85; решение заказчика 2026-10-25, В-50)
- `control-plane/app/api/admin/audit.py` — `GET /admin/audit?filter=` (`x-permission: audit.read`)
- `control-plane/tests/e2e/test_admin_users.py` — заглушки раздела Users
- `control-plane/tests/e2e/test_admin_nodes.py` — заглушки карточки ноды, дашборда и настроек
- `control-plane/app/domain/probes.py` — `class ProbeService`: `record(node_id, country, available)`; интерфейс `ProbeSource` — заглушки
- `control-plane/app/api/admin/probes.py` — `GET /admin/nodes/{id}/availability` (`x-permission: nodes.read`) — заглушка
- `control-plane/app/api/hooks/__init__.py` — роутер `hooks` (входящие webhook)
- `control-plane/app/api/hooks/probes.py` — `POST /api/v1/hooks/probes` (входящий webhook проб): вне `/admin`, без сессии и CSRF, проверка подписи HMAC — заглушка

### Изменения в существующих файлах

#### Файл: `control-plane/app/api/router.py`

- подключение роутера `api/hooks/` (входящий webhook проб) к `/api/v1`

#### Файл: `control-plane/app/api/admin/__init__.py` — изменение принятого кода (отступление от 001.18)

- `GET /admin/dashboard` (заглушка 001.18, `dashboard.read`) переносится в `dashboard.py`; подпись
  «(001.44)» в `summary` и в `control-plane/app/api/.AGENTS.md` исправляется на «(001.49/001.85)»

#### Файл: `control-plane/app/api/admin/nodes.py` — изменение принятого кода (001.24)

- `POST /admin/nodes/{id}/provider-complaint` (`x-permission: nodes.write`) — заглушка (логика — 001.69)

### Интеграция компонентов

Карточка ноды — 001.24 (`GET /admin/nodes/{id}`), дополняется метриками в 001.85.

Маршруты этой задачи подключают `require_permission` (001.46) сами; имена разрешений — реестр
001.46, роли — матрица 001.48. Поле `resale_signals` в карточку пользователя добавляет 001.39.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Карточка пользователя
   - Входные данные: `GET /admin/users/{id}`
   - Ожидаемый результат: схема с полями §17.5 (фиксированные значения)
   - Примечание: заглушка
2. **TC-E2E-02:** Разрешения маршрутов
   - Входные данные: `/openapi.json`
   - Ожидаемый результат: у каждого маршрута `/admin/*` этой задачи `x-permission` из реестра
     `PERMISSIONS` (001.46): `users.read` у поиска и карточки, `users.write` у действий карточки,
     `approvals.approve` у обоих маршрутов `/admin/approvals`, `settings.read` и `settings.write` у
     настроек и окна обслуживания, `nodes.read` у доступности, `nodes.write` у обращения
     провайдера

### Модульные тесты

- Не требуются: задача не содержит логики сверх заглушек или конфигурации.

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_admin_users.py tests/e2e/test_admin_nodes.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Маршруты разделов §4.15 в OpenAPI, включая `/admin/approvals`,
  `/admin/settings/maintenance`, `/admin/admins` и режим от лица пользователя
- [ ] Схема карточки содержит поля §17.5 и `email_undeliverable_at`
- [ ] Схема `PATCH /admin/settings` не содержит доменов подписки
- [ ] Входящий webhook проб объявлен вне `/admin`; `GET /admin/dashboard` объявлен один раз
- [ ] Каждый маршрут `/admin/*` этой задачи объявляет `x-permission` с именем из реестра 001.46
  (TC-E2E-02)

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.46. Приоритет: High. Оценка: 3 ч. Этап: 7 — администрирование, RBAC, аудит,
поддержка.
