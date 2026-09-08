# Задача 001.18: API администратора: тарифы, группы, коды — маршруты и заглушки

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-09 Управление тарифами, группами и кодами

Требования RTM: R-18, R-30, R-32, R-33.

<!-- contract:goal -->

## Цель задачи

Объявить CRUD-маршруты `/api/v1/admin/plans`, `/admin/groups`, `/admin/codes` со схемами и
заглушками, проходящими сквозные тесты на фиксированных ответах.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/api/admin/plans.py` — `GET/POST /admin/plans`, `PATCH/DELETE /admin/plans/{id}`; схема `PlanIn`: `name`, `price_amount?`, `price_currency?`, `duration_days`, `traffic_limit_bytes?`, `device_limit?`, `access_group_ids[]`, `profiles[]`
- `control-plane/app/api/admin/groups.py` — `/admin/groups/access` и `/admin/groups/billing` CRUD; `POST /admin/groups/billing/{id}/multiplier` (новый интервал)
- `control-plane/app/api/admin/codes.py` — `POST /admin/codes` (один), `POST /admin/codes/batch`, `GET /admin/codes/batch/{id}/export` (CSV), `GET /admin/codes/{id}/redemptions`
- `control-plane/app/domain/plans.py` — `class PlanService` — заглушки
- `control-plane/app/domain/groups.py` — `class GroupService` — заглушки
- `control-plane/app/domain/codes.py` — `class CodeService`: `create`, `create_batch(n, spec) -> batch_id`, `export(batch_id) -> Iterable[str]`, `redeem(user_id, code) -> Redemption` — заглушки; `def checksum_ok(code: str) -> bool` реализуется сразу: чистая функция без внешних зависимостей, тест в этой задаче
- `control-plane/tests/e2e/test_catalog.py` — сценарий UC-09 на заглушках

### Интеграция компонентов

Маршруты защищены `require_permission` (заглушка 001.12). Схемы совпадают с полями
`docs/architectures/data-model.md` §4.2.2 и §4.2.4.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** CRUD тарифа на заглушках
   - Входные данные: создание, чтение, изменение, архивирование
   - Ожидаемый результат: фиксированные ответы 201/200/200/204
   - Примечание: заглушка
2. **TC-E2E-02:** Экспорт партии
   - Входные данные: `GET /admin/codes/batch/{id}/export`
   - Ожидаемый результат: `text/csv` с заголовком `code,expires_at,plan`
   - Примечание: заглушка

### Модульные тесты

1. **TC-UNIT-01:** Контрольная сумма кода
   - Проверяемая функция: `app/domain/codes.py::checksum_ok`
   - Входные данные: `VPN-ABCD-EFGH` с верной и неверной суммой
   - Ожидаемый результат: истина / ложь без обращения к базе

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_catalog.py tests/unit/domain/test_codes.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Маршруты в OpenAPI со схемами
- [ ] `checksum_ok` реализована (чистая функция, §16.8)
- [ ] Сквозные тесты проходят на заглушках

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.12, 001.05. Приоритет: High. Оценка: 3 ч. Этап: 3 — тарифы, подписки, коды.
