# Задача 001.15: API кабинета `/api/v1/me`: маршруты и сквозные тесты на заглушках

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-16 Работа в личном кабинете
- UC-14 Удаление аккаунта

Требования RTM: R-10, R-11, R-12, R-13, R-52.

<!-- contract:goal -->

## Цель задачи

Объявить маршруты профиля, подписки, статистики, онбординга и удаления аккаунта со схемами ответов
по `docs/idea.md` §4.2, §4.12, §17.6.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/api/me.py` — `GET /me`, `PATCH /me` (язык, часовой пояс, согласия), `DELETE /me`, `GET /me/subscription`, `POST /me/subscription/redeem`, `POST /me/subscription/reissue`, `GET /me/traffic?period=`, `GET /me/onboarding`
- `control-plane/app/domain/profile.py` — `class ProfileService`: `get`, `update`, `delete_account` — заглушки
- `control-plane/app/accounting/stats.py` — `async def user_traffic(conn, user_id, period_id) -> TrafficStats` — заглушка с фиксированными числами
- `control-plane/tests/e2e/test_me.py` — сценарий UC-16 на заглушках

### Интеграция компонентов

Схема `TrafficStats` содержит `raw_uplink`, `raw_downlink`, `billable`, `remaining`, `by_node[]` с
`multiplier`, `by_country[]`, `active_ips`, `device_limit` (§4.12).

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Кабинет отдаёт состав §4.12
   - Входные данные: `GET /me/traffic`
   - Ожидаемый результат: 200 и все поля схемы с фиксированными значениями
   - Примечание: заглушка
2. **TC-E2E-02:** Перевыпуск требует подтверждения
   - Входные данные: `POST /me/subscription/reissue` без `confirm=true`
   - Ожидаемый результат: 409 с предупреждением о разрыве устройств

### Модульные тесты

- Не требуются: задача не содержит логики сверх заглушек или конфигурации.

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_me.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Восемь маршрутов в OpenAPI
- [ ] Схема `TrafficStats` соответствует §4.12
- [ ] Тесты проходят на заглушках

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.13. Приоритет: High. Оценка: 3 ч. Этап: 2 — пользователи и кабинет.
