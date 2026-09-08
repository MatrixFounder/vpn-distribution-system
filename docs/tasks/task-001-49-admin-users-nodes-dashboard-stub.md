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

- `control-plane/app/api/admin/users.py` — `GET /admin/users?q=&filter=`, `GET /admin/users/{id}` (карточка: подписка, обращения к подписке, последнее подключение, потребление с коэффициентом, причина отзыва, `resale_signals`), `POST …/block|unblock|plan|renew|traffic|reset-credentials`
- `control-plane/app/api/admin/dashboard.py` — `GET /admin/dashboard`: счётчики §4.15
- `control-plane/app/api/admin/settings.py` — `GET/PATCH /admin/settings`: режим регистрации, почта, CAPTCHA, домены (≥ 2), обслуживание, версии, стратегия разрыва
- `control-plane/app/api/admin/audit.py` — `GET /admin/audit?filter=`
- `control-plane/tests/e2e/test_admin.py` — заглушки
- `control-plane/app/domain/probes.py` — `class ProbeService`: `record(node_id, country, available)`; интерфейс `ProbeSource` — заглушки
- `control-plane/app/api/admin/probes.py` — `POST /admin/probes/results`, `GET /admin/nodes/{id}/availability` — заглушки

### Интеграция компонентов

Карточка ноды — 001.24 (`GET /admin/nodes/{id}`), дополняется метриками в 001.85.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Карточка пользователя
   - Входные данные: `GET /admin/users/{id}`
   - Ожидаемый результат: схема с полями §17.5 (фиксированные значения)
   - Примечание: заглушка

### Модульные тесты

- Не требуются: задача не содержит логики сверх заглушек или конфигурации.

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_admin.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Маршруты разделов §4.15 в OpenAPI
- [ ] Схема карточки содержит поля §17.5

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.46. Приоритет: High. Оценка: 3 ч. Этап: 7 — администрирование, RBAC, аудит,
поддержка.
