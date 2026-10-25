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
- `web/packages/i18n/{ru,en}.json` — строки интерфейса; `t(key)`; выбор языка `pickLanguage(profile, navigator.languages)`: профиль → первый поддерживаемый язык из списка `navigator.languages` (из него браузер строит `Accept-Language`; правило сервера 001.65) → `en`
- `web/apps/cabinet/src/routes.tsx` — `/`, `/subscription`, `/traffic`, `/onboarding`, `/profile` — заглушки
- `web/apps/admin/src/routes.tsx` — `/dashboard`, `/users`, `/nodes`, `/groups`, `/plans`, `/codes`, `/settings`, `/audit`, `/approvals` (запросы второго подтверждения, В-34), `/admins` (раздел Administrators) и `/profile` (свой профиль администратора; оба — 001.89, В-33) — заглушки
- `web/apps/cabinet/src/App.test.tsx` — рендер маршрутов
- `web/apps/cabinet/vite.config.ts`, `web/apps/admin/vite.config.ts` — сборка; `build.target` —
  минимальные версии перечня браузеров ниже

### Изменения в существующих файлах

#### Файлы: `web/apps/cabinet/package.json`, `web/apps/admin/package.json`

- `browserslist` по перечню поддерживаемых браузеров (решение заказчика 2026-10-25, В-38; ОВ-14
  закрыт): `last 2 Chrome versions, last 2 Firefox versions, last 2 Safari versions, last 2 Edge
  versions, iOS >= 16, last 2 ChromeAndroid versions` — для обоих приложений. Кабинет
  дополнительно поддерживает встроенные браузеры мессенджеров (Telegram, Instagram) на iOS и
  Android; они работают на системном движке (WKWebView на iOS, Android System WebView), поэтому
  цель сборки кабинета та же, а проверка в них — приёмка 001.63

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
   - Входные данные: профиль `ru`; без профиля с `navigator.languages = ['de']`; без профиля с
     `navigator.languages = ['de', 'ru']`
   - Ожидаемый результат: `ru`; `en`; `ru`

### Регрессионные тесты

- Команда: `cd web && npm run lint && npm run test`
- Полный набор: `make check` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Клиент API собирается из схемы без ручных правок
- [ ] Все ключи `i18n` присутствуют в обоих языках (тест)
- [ ] `browserslist` и `build.target` обоих приложений соответствуют перечню браузеров (В-38)

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.01, 001.10. Приоритет: High. Оценка: 4 ч. Этап: 10 — интерфейсы web.
