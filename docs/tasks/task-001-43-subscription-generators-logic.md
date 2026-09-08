# Задача 001.43: Генераторы `base64` и `singbox`: матрица «профиль × формат», golden-файлы

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-03 Получение подписки клиентом

Требования RTM: R-15.

<!-- contract:goal -->

## Цель задачи

Реализовать генерацию ссылок `vless://` и `trojan://` с параметрами REALITY и XHTTP и JSON по схеме
sing-box v1.14.0 с автоселектором `urltest`; исключение неприменимых пар по матрице §5.6 источника.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/tests/golden/subscription/` — эталоны: base64 три профиля; singbox два профиля + urltest

### Изменения в существующих файлах

#### Файл: `control-plane/app/subscription/generators/base64.py`

- `vless://uuid@host:port?security=reality&sni=&fp=&pbk=&sid=&spx=&flow=&type=raw|xhttp&path=&mode=#name`; `trojan://password@host:port?security=reality&…#name`; имя = код ноды + профиль + коэффициент при ≠ 1.0

#### Файл: `control-plane/app/subscription/generators/singbox.py`

- outbounds `vless` (reality, utls) и `trojan`; `applicable(vless_xhttp) = False`; `urltest` над всеми серверами; поля только из схемы v1.14.0

#### Файл: `control-plane/app/subscription/composer.py`

- по одной записи на включённый inbound доступной ноды; исключение неприменимых пар генератором

### Интеграция компонентов

Credentials пользователя для ноды — из `node_user_credentials` (расшифровка `FieldCipher`).

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Sing-box без XHTTP
   - Входные данные: пользователь с нодой из трёх inbound; формат `singbox`
   - Ожидаемый результат: две записи + `urltest`; профиля `vless-xhttp` нет (AC-28)
2. **TC-E2E-02:** Base64 по включённым inbound
   - Входные данные: один inbound выключен
   - Ожидаемый результат: две записи на ноду (AC-28)
3. **TC-E2E-03:** Побайтное совпадение
   - Входные данные: фикстура
   - Ожидаемый результат: ответ равен golden-файлу (AC-03)

### Модульные тесты

1. **TC-UNIT-01:** Ссылка VLESS REALITY
   - Проверяемая функция: `generators/base64.py::vless_link`
   - Входные данные: inbound `vless-raw-vision`
   - Ожидаемый результат: параметры `security=reality`, `flow=xtls-rprx-vision`, `pbk`, `sid`, `fp`, `spx` присутствуют

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_subscription.py -k generator tests/unit/subscription/test_generators.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] AC-03, AC-28 выполнены
- [ ] Имя записи содержит коэффициент при ≠ 1.0 (§5.6)

## Примечания

Проверка реальными клиентами §4.2 — на стенде, 001.73.

Зависимости: 001.41, 001.27. Приоритет: Critical. Оценка: 4 ч. Этап: 6 — subscription-эндпоинт и
логика кабинета.
