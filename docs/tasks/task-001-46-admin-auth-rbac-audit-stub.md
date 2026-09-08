# Задача 001.46: Аутентификация администраторов, RBAC и аудит — интерфейсы и заглушки

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-09 Управление тарифами, группами и кодами
- UC-13 Поддержка пользователя

Требования RTM: R-35, R-36, R-37.

<!-- contract:goal -->

## Цель задачи

Объявить `/api/v1/admin/auth/*`, реестр разрешений, зависимость `require_permission` и
`audit.record` с заглушками, чтобы административные маршруты собирались с проверкой прав до
реализации.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/api/admin/auth.py` — `POST /admin/auth/login`, `POST /admin/auth/totp`, `POST /admin/auth/recovery`, `POST /admin/auth/logout`
- `control-plane/app/security/rbac.py` — `PERMISSIONS: dict[str, set[Role]]` — заглушка со всеми разрешениями у `super_admin`; `require_permission(name)`
- `control-plane/app/security/totp.py` — `class Totp`: `provision(admin_id) -> uri`; `verify(admin_id, code) -> bool`; `recovery_codes(admin_id) -> list[str]` — заглушки
- `control-plane/app/security/audit.py` — `async def record(conn, actor, action, entity, old, new, result, ip, ua, impersonated=None)` — заглушка без записи
- `control-plane/tests/e2e/test_admin_auth.py` — вход, второй фактор, отказ без разрешения — на заглушках

### Интеграция компонентов

Все маршруты `/admin/*` из 001.18, 001.24, 001.49 используют `require_permission`.

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

### Модульные тесты

- Не требуются: задача не содержит логики сверх заглушек или конфигурации.

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_admin_auth.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Реестр разрешений объявлен; каждый маршрут `/admin/*` декларирует одно разрешение
- [ ] Заглушки не пишут в базу

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.12, 001.09. Приоритет: Critical. Оценка: 3 ч. Этап: 7 — администрирование, RBAC,
аудит, поддержка.
