# Задача 001.62: Web: клиент API из OpenAPI, пакет локализации, каркасы кабинета и панели

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-16 Работа в личном кабинете
- UC-13 Поддержка пользователя

Требования RTM: R-51, R-53.

<!-- contract:goal -->

## Цель задачи

Собрать два приложения React 19 + Vite с общим клиентом API, сгенерированным из `/openapi.json`,
пакетом `i18n` (RU, EN) и маршрутизацией по экранам §4.2 и §4.15; экраны — заглушки.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `web/packages/api-client/` — генерация из OpenAPI (`openapi-typescript`); `npm run gen`
- `web/packages/i18n/{ru,en}.json` — строки интерфейса; `t(key)`; выбор языка: профиль → `navigator.language` → `en`
- `web/apps/cabinet/src/routes.tsx` — `/`, `/subscription`, `/traffic`, `/onboarding`, `/profile` — заглушки
- `web/apps/admin/src/routes.tsx` — `/dashboard`, `/users`, `/nodes`, `/groups`, `/plans`, `/codes`, `/settings`, `/audit` — заглушки
- `web/apps/cabinet/src/App.test.tsx` — рендер маршрутов

### Интеграция компонентов

Клиент API регенерируется в CI из схемы Control Plane (стадия `contract`).

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Приложения собираются
   - Входные данные: `npm run build`
   - Ожидаемый результат: статика в `dist/` для обоих приложений; `tsc` без ошибок

### Модульные тесты

1. **TC-UNIT-01:** Выбор языка
   - Проверяемая функция: `packages/i18n::pickLanguage`
   - Входные данные: профиль `ru`; без профиля с `navigator.language = de`
   - Ожидаемый результат: `ru`; `en`

### Регрессионные тесты

- Команда: `cd web && npm run lint && npm run test`
- Полный набор: `make check` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Клиент API собирается из схемы без ручных правок
- [ ] Все ключи `i18n` присутствуют в обоих языках (тест)

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.01, 001.10. Приоритет: High. Оценка: 4 ч. Этап: 10 — интерфейсы web.
