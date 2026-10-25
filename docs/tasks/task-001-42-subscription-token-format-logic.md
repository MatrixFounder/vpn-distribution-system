# Задача 001.42: Токен подписки, выбор формата, заголовки, состояния, журнал обращений, лимиты

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-03 Получение подписки клиентом
- UC-16 Работа в личном кабинете

Требования RTM: R-10, R-14, R-45.

<!-- contract:goal -->

## Цель задачи

Реализовать токен ≥ 128 бит с хешем для поиска и шифротекстом для показа, первичный выпуск,
перевыпуск с ротацией credentials, выбор формата по конфигурируемому соответствию `User-Agent`,
заголовки `subscription-userinfo`/`profile-update-interval`/`profile-title`/`announce`/`support-url`,
состояния эндпоинта, запись `subscription_access_log` и лимиты частоты.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/migrations/NNN_subscription_token_enc.sql` и откат `.rollback.sql` (NNN — следующий свободный номер) — изменение принятой схемы (отступление от 001.07): колонка `subscription_tokens.token_enc bytea` (FieldCipher, как `codes.code_enc`)
- `control-plane/migrations/NNN_settings_subscription.sql` и откат `.rollback.sql` — миграция-сид ключа `settings.profile_update_interval_hours`, целое число часов, по умолчанию 1 (`ON CONFLICT DO NOTHING`; откат удаляет ровно этот ключ) (ОВ-18 закрыт, решение заказчика 2026-10-25, В-26)

### Изменения в существующих файлах

#### Файл: `control-plane/tests/e2e/test_subscription.py`

- дополнить (файл заводит 001.41): реальные утверждения по состояниям, заголовкам, лимитам, журналу

#### Файл: `control-plane/tests/unit/db/test_schema_subscriptions.py`

- страж дополнить колонкой `token_enc`

#### Файл: `control-plane/app/domain/subscriptions.py`

- Изменение принятого кода (отступление от 001.22): `activate` получает необязательный параметр
  `conn` — при переданном `conn` запись идёт в транзакции вызывающего (его переиспользует погашение
  кода 001.20), без него — в своей транзакции; в той же транзакции
  `activate` вызывает `SubscriptionTokenService.ensure_token(user_id, conn)`; откат активации
  токена не оставляет; погашение кода и действия панели отдельного вызова `ensure_token` не делают
  (TC-E2E-05; наличие токена после погашения проверяет 001.20)

#### Файл: `control-plane/app/subscription/tokens.py`

- `issue`: 32 случайных байта, base64url; запись `token_hash` (поиск) и `token_enc` (показ в кабинете, поддержке и письме); `resolve` ищет по `token_hash`
- `ensure_token(user_id, conn=None) -> str` (сигнатура заглушки 001.41): выпуск при первой активации, идемпотентно (частичный UNIQUE `subscription_tokens_active_user_idx` не даёт второго действующего токена); возвращает действующий токен — прежний (расшифровка `token_enc`) или выпущенный; вызывается в транзакции `SubscriptionService.activate` и из `GET /me/subscription` (001.16), если действующего токена нет
- `reissue(user_id, ctx)` — единственное место перевыпуска: в одной транзакции `revoked_at` прежнего, новый токен, `CompositionService.rotate_user_credentials(conn, user_id)` (001.29) и `audit.record` на подключении этой транзакции (событие «Перевыпуск subscription-токена» таблицы 001.48): `ctx` — `AuditContext` сессии пользователя (001.46), `actor_type = user`, `ip` и `user_agent` не заполняются, значения токенов в `old_value` и `new_value` не пишутся; параметр `ctx` к сигнатуре заглушки 001.41 добавляет эта задача

#### Файл: `control-plane/app/accounting/stats.py`

- `effective_limit(conn, period_id) -> int | None` — действующий лимит периода: `traffic_limit_bytes` плюс сумма записей `bonus` и `adjustment` журнала баланса периода, взятая со знаком минус (решение 001.22); `None` — Unlimited; ту же функцию вызывает кабинет (001.16)

#### Файл: `control-plane/app/subscription/router.py`

- состояние эндпоинта §5.6 по `subscriptions.state` и `users.status`: `active` → `active`; `suspended_quota` → `traffic_exceeded`; `suspended_admin` или `users.status = 'blocked'` → `blocked`, 200, пустой список и причина в `announce` (решение заказчика 2026-10-25, В-40: «Заблокировать» в панели ставит обе отметки, 001.50); `expired` → `expired`; токен отозван, пользователь `deleted` или подписки нет (`none`) → `unknown_token`, 404; `announce` с причиной по состоянию; ноль серверов при `active` → 200, пустой список и `announce`
- счётчик Prometheus `control_plane_subscription_empty_responses_total` без меток — ряд пустых
  ответов `active`: число ответов `GET /s/{token}` с нулём серверов при состоянии эндпоинта
  `active` (200, пустой список); растёт на 1 за каждый такой ответ; пустые ответы состояний
  `traffic_exceeded`, `blocked` и `expired` его не увеличивают; выводится в `/metrics` процесса
  (§5.4); правило алерта «Немедленный» «ноль доступных серверов при активной подписке» по этому
  ряду — 001.68 (решение заказчика 2026-10-25, В-42)
- `subscription-userinfo` (решение заказчика 2026-10-25, В-25): `total` — `effective_limit` текущего периода, Unlimited — `total=0`; `upload + download` = `b` — списанное за период по отчётам: `used_billable_bytes` минус сумма записей `bonus` и `adjustment` периода (равно сумме записей `report` и `late_report`); разбивка пропорциональна сырому трафику: `up` и `down` — суммы `raw_uplink_bytes` и `raw_downlink_bytes` строк `traffic_hourly` пользователя с `hour_start` не раньше `min(period_start, created_at)` текущего периода, `upload = ⌊b × up / (up + down)⌋`, `download = b − upload`, без сырого трафика `upload = 0`; `expire` — `period_end` текущего периода в Unix-времени; остаток в клиенте (`total − upload − download`) равен остатку кабинета `remaining_of(traffic_limit_bytes, used_billable_bytes)` (001.22, 001.16), пока расход не превысил лимит
- `profile-update-interval` — значение `settings.profile_update_interval_hours` (по умолчанию 1 ч); целевого времени восстановления для клиентов `base64` нет, восстановление ручное (Н-35; ОВ-18 закрыт, В-26)
- `subscription_access_log`: время, домен, формат, `User-Agent`, адрес источника, страна (GeoIP, ОВ-A4)
- лимиты `ratelimit.subscription_token(token_hash)` и `ratelimit.subscription_unknown_ip(ip)` (раздельные пороги)

### Интеграция компонентов

Соответствие `User-Agent → формат` — в `settings`; домены подписки — окружение
`SUBSCRIPTION_DOMAINS` (`Settings.subscription_domains`, как в 001.15): панель показывает их только
для чтения, замена домена — процедура развёртывания (решение заказчика 2026-10-25, В-27).
Ротация credentials — `CompositionService.rotate_user_credentials` (001.29, зависимость 001.42 →
001.29); имя `rotate_credentials` занято командой ротации identity ноды (001.31). Запись журнала
перевыпуска — `audit.record` и `AuditContext` (001.46, зависимость 001.42 → 001.46); контекст
передаёт маршрут кабинета (001.16); строку `audit_log` проверяет эта задача (001.48). Страница
подписки — 001.45.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Заголовки
   - Входные данные: подписка `active`, лимит тарифа 10 ГиБ, бонус 2 ГиБ в журнале периода; `traffic_hourly` периода на ноде с коэффициентом 2.0: сырой uplink 1 ГиБ, downlink 3 ГиБ; списано по отчётам 8 ГиБ
   - Ожидаемый результат: `subscription-userinfo`: `upload` = 2 ГиБ, `download` = 6 ГиБ, `total` = 12 ГиБ, `expire` — `period_end` в Unix-времени; `total − upload − download` = 4 ГиБ = `SubscriptionService.remaining` (остаток кабинета); `profile-update-interval: 1`
2. **TC-E2E-02:** Состояние `traffic_exceeded`
   - Входные данные: пользователь `suspended_quota`
   - Ожидаемый результат: 200, пустой список, `announce` с причиной
3. **TC-E2E-03:** Перебор токенов
   - Входные данные: 20 запросов неизвестных токенов с одного адреса
   - Ожидаемый результат: 429 после порога; существующие токены обслуживаются (AC-36)
4. **TC-E2E-04:** Журнал обращений
   - Входные данные: запрос от v2rayNG
   - Ожидаемый результат: строка `subscription_access_log` с форматом, `User-Agent` и адресом источника
5. **TC-E2E-05:** Первичный выпуск
   - Входные данные: первая `SubscriptionService.activate`; повторная активация
   - Ожидаемый результат: после первой действующий токен есть; повторная второго не создаёт
6. **TC-E2E-06:** Состояние `unknown_token`
   - Входные данные: отозванный токен; токен пользователя `deleted`; токен пользователя с подпиской `none`
   - Ожидаемый результат: 404 без тела
7. **TC-E2E-07:** Токен не в журнале
   - Входные данные: ответ 500 на `GET /s/{token}`
   - Ожидаемый результат: строка журнала приложения не содержит токена (Н-25)
8. **TC-E2E-08:** Перевыпуск в журнале аудита
   - Входные данные: `reissue(user_id, ctx)` с контекстом сессии пользователя; для второго пользователя — сбой `audit.record` в транзакции перевыпуска
   - Ожидаемый результат: у первого прежний токен → 404, `node_user_credentials.version` увеличен, одна строка `audit_log` с `actor_type = user`, пустыми `ip` и `user_agent` и без значений токенов; у второго прежний токен действует, credentials и `audit_log` не изменены (AC-35)
9. **TC-E2E-09:** Состояние `blocked`
   - Входные данные: подписка `suspended_admin` при `users.status = 'active'`; подписка `active` при `users.status = 'blocked'`
   - Ожидаемый результат: 200, пустой список, `announce` с причиной блокировки в обоих случаях
10. **TC-E2E-10:** Безлимитный тариф
    - Входные данные: подписка `active` тарифа Unlimited
    - Ожидаемый результат: `subscription-userinfo` с `total=0`
11. **TC-E2E-11:** Ряд пустых ответов `active`
    - Входные данные: подписка `active`, у групп тарифа нет доступных нод; два запроса
      `GET /s/{token}`; затем запрос пользователя `suspended_quota`
    - Ожидаемый результат: два ответа 200 с пустым списком и `announce`;
      `control_plane_subscription_empty_responses_total` вырос на 2; после запроса
      `suspended_quota` значение не изменилось

### Модульные тесты

1. **TC-UNIT-01:** Выбор формата
   - Проверяемая функция: `app/subscription/router.py::pick_format`
   - Входные данные: `User-Agent` из таблицы настроек; неизвестный
   - Ожидаемый результат: формат из таблицы; `base64`
2. **TC-UNIT-02:** Восстановимый токен
   - Проверяемая функция: `app/subscription/tokens.py::issue`
   - Входные данные: выпуск токена
   - Ожидаемый результат: расшифровка `token_enc` равна выданному токену; `resolve` находит пользователя по хешу

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_subscription.py tests/e2e/test_migrations.py tests/unit/subscription tests/unit/db/test_schema_subscriptions.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Н-25: энтропия ≥ 128 бит; путь не в логах (TC-E2E-07)
- [ ] AC-35: перевыпуск аннулирует прежний токен и ротирует credentials
- [ ] Состояния §5.6 выполнены, включая `blocked` (TC-E2E-06, TC-E2E-09)
- [ ] Первичный выпуск идемпотентен; ссылка восстанавливается из `token_enc`
- [ ] Остаток по `subscription-userinfo` совпадает с остатком кабинета (TC-E2E-01, TC-E2E-10)
- [ ] Ответ с нулём серверов при `active` увеличивает
  `control_plane_subscription_empty_responses_total`, пустые ответы других состояний — нет
  (TC-E2E-11)

## Примечания

`tdd-strict` для `tokens.py`.

Зависимости: 001.14, 001.22, 001.41, 001.84, 001.29, 001.46. Приоритет: Critical. Оценка: 4 ч. Этап:
6 — subscription-эндпоинт и логика кабинета.
