# Задача 001.42: Токен подписки, выбор формата, заголовки, состояния, журнал обращений, лимиты

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-03 Получение подписки клиентом
- UC-16 Работа в личном кабинете

Требования RTM: R-14.

<!-- contract:goal -->

## Цель задачи

Реализовать токен ≥ 128 бит с хешем, перевыпуск с ротацией credentials, выбор формата по
конфигурируемому соответствию `User-Agent`, заголовки
`subscription-userinfo`/`profile-update-interval`/`profile-title`/`announce`/`support-url`,
состояния эндпоинта, запись `subscription_access_log` и лимиты частоты.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/tests/e2e/test_subscription.py` — реальные утверждения по состояниям, заголовкам, лимитам, журналу

### Изменения в существующих файлах

#### Файл: `control-plane/app/subscription/tokens.py`

- `issue`: 32 случайных байта, base64url, `token_hash`; `reissue`: `revoked_at` прежнего, новый токен, `CompositionService.rotate_credentials(user_id)`

#### Файл: `control-plane/app/subscription/router.py`

- состояния по `subscriptions.state`; `announce` с причиной; ноль серверов при `active` → 200 пустой список + алерт
- `subscription_access_log`: время, домен, формат, `User-Agent`, страна (GeoIP, ОВ-A4)
- лимиты `rl:sub:token:{hash}` и `rl:sub:unknown:ip:{ip}` (раздельные пороги)
- страница для браузера с обоими доменами

### Интеграция компонентов

Соответствие `User-Agent → формат` — в `settings`; домены — `settings.subscription_domains` (два).

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Заголовки
   - Входные данные: подписка `active`
   - Ожидаемый результат: `subscription-userinfo` с `upload/download/total/expire` в байтах и Unix-времени; `profile-update-interval`
2. **TC-E2E-02:** Состояние `traffic_exceeded`
   - Входные данные: пользователь `suspended_quota`
   - Ожидаемый результат: 200, пустой список, `announce` с причиной
3. **TC-E2E-03:** Перебор токенов
   - Входные данные: 20 запросов неизвестных токенов с одного адреса
   - Ожидаемый результат: 429 после порога; существующие токены обслуживаются (AC-36)
4. **TC-E2E-04:** Журнал обращений
   - Входные данные: запрос от v2rayNG
   - Ожидаемый результат: строка `subscription_access_log` с форматом и `User-Agent`

### Модульные тесты

1. **TC-UNIT-01:** Выбор формата
   - Проверяемая функция: `app/subscription/router.py::pick_format`
   - Входные данные: `User-Agent` из таблицы настроек; неизвестный
   - Ожидаемый результат: формат из таблицы; `base64`

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_subscription.py tests/unit/subscription`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Н-25: энтропия ≥ 128 бит; путь не в логах
- [ ] AC-35: перевыпуск аннулирует прежний токен и ротирует credentials
- [ ] Состояния §5.6 выполнены

## Примечания

`tdd-strict` для `tokens.py`.

Зависимости: 001.14, 001.22, 001.41, 001.84. Приоритет: Critical. Оценка: 4 ч. Этап: 6 —
subscription-эндпоинт и логика кабинета.
