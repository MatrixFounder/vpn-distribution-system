# Задача 001.41: Subscription-эндпоинт `/s/{token}`: маршруты, генераторы, страница — заглушки

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-03 Получение подписки клиентом

Требования RTM: R-14, R-15, R-16, R-17.

<!-- contract:goal -->

## Цель задачи

Объявить эндпоинт, выбор формата, генераторы `base64` и `singbox`, страницу подписки и контракт
состояний по `docs/idea.md` §5.6 с фиксированными ответами.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/subscription/router.py` — `GET /s/{token}`, `GET /s/{token}/base64`, `GET /s/{token}/singbox`; выбор по `User-Agent`; страница для браузера
- `control-plane/app/subscription/tokens.py` — `class SubscriptionTokenService`: `issue(user_id) -> str`; `resolve(token) -> user_id | None`; `reissue(user_id) -> str` — заглушки
- `control-plane/app/subscription/composer.py` — `compose(user_id, country) -> list[Entry]` — отбор пар «нода × профиль» — заглушка
- `control-plane/app/subscription/generators/base.py` — `class Generator(Protocol)`: `applicable(profile) -> bool`; `render(entries, user) -> bytes`
- `control-plane/app/subscription/generators/base64.py` — заглушка: фиксированный список ссылок
- `control-plane/app/subscription/generators/singbox.py` — заглушка: фиксированный JSON по схеме sing-box v1.14.0
- `control-plane/app/subscription/page.py` — Jinja2-страница с инструкциями и кнопками импорта — заглушка
- `control-plane/tests/e2e/test_subscription.py` — состояния, суффиксы, браузер; golden-файлы `tests/golden/subscription/*.{txt,json}`

### Интеграция компонентов

Заголовки ответа §5.6 задаются в `router.py`; путь запроса исключён из журнала приложения (Н-25)
через фильтр логгера.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Формат по User-Agent
   - Входные данные: `User-Agent: sing-box`
   - Ожидаемый результат: `application/json` (заглушка); `v2rayNG` → base64
   - Примечание: заглушка
2. **TC-E2E-02:** Неизвестный токен
   - Входные данные: `GET /s/unknown`
   - Ожидаемый результат: 404 без тела
3. **TC-E2E-03:** Браузер
   - Входные данные: `Accept: text/html`
   - Ожидаемый результат: страница подписки 200

### Модульные тесты

- Не требуются: задача не содержит логики сверх заглушек или конфигурации.

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_subscription.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Три маршрута и страница объявлены
- [ ] Состояния `active|expired|traffic_exceeded|blocked → 200`, `unknown_token → 404` реализованы в маршрутизаторе
- [ ] Golden-файлы заведены

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.10, 001.07. Приоритет: Critical. Оценка: 3 ч. Этап: 6 — subscription-эндпоинт и
логика кабинета.
