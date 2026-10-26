# Задача 001.43: Генераторы `base64` и `singbox`: матрица «профиль × формат», golden-файлы

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-03 Получение подписки клиентом

Требования RTM: R-15.

<!-- contract:goal -->

## Цель задачи

Реализовать генерацию ссылок `vless://` и `trojan://` с параметрами REALITY и XHTTP и JSON по схеме
sing-box v1.14.4 с автоселектором `urltest`; исключение неприменимых пар по матрице §5.6 источника.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/tests/unit/subscription/test_generators.py` — ссылка VLESS REALITY (TC-UNIT-01)
- `control-plane/tests/golden/subscription/` — эталоны: base64 три профиля; singbox два профиля + urltest

### Изменения в существующих файлах

#### Файл: `control-plane/app/subscription/generators/base64.py`

- `vless://uuid@host:port?security=reality&sni=&fp=&pbk=&sid=&spx=&flow=&type=raw|xhttp&path=&mode=#name`; `trojan://password@host:port?security=reality&…#name`; имя = код ноды + профиль + коэффициент при ≠ 1.0
- `fp` — `client_fingerprint` inbound; на v26.10.21 — `chrome` (WI-36)

#### Файл: `control-plane/app/subscription/generators/singbox.py`

- outbounds `vless` (reality, utls) и `trojan`; `applicable(vless_xhttp) = False`; `urltest` над всеми серверами; поля только из схемы v1.14.4
- `tls.utls.fingerprint` — `client_fingerprint` inbound, то же значение, что `fp` ссылки (WI-36)

#### Файл: `control-plane/app/subscription/composer.py`

- одна запись на inbound из фикстуры для golden-файлов; исключение неприменимых пар генератором; фильтры выдачи (статус ноды, `inbounds.enabled`, исправность inbound и остальные условия R-16) — 001.44

### Интеграция компонентов

Credentials пользователя для ноды — из `node_user_credentials` (расшифровка `FieldCipher`).

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Sing-box без XHTTP
   - Входные данные: пользователь с нодой из трёх inbound; формат `singbox`
   - Ожидаемый результат: две записи + `urltest`; профиля `vless-xhttp` нет (AC-28)
2. **TC-E2E-02:** Base64 по записи на inbound
   - Входные данные: пользователь с нодой из трёх inbound; формат `base64`
   - Ожидаемый результат: три записи, включая `vless-xhttp` (AC-28; выключенный inbound и inbound с ошибкой цели исключает 001.44)
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

- [ ] AC-03 выполнен; AC-28 — в части генераторов (запись на включённый inbound — 001.44)
- [ ] Имя записи содержит коэффициент при ≠ 1.0 (§5.6)
- [ ] `fp` ссылки и `tls.utls.fingerprint` JSON `singbox` равны `client_fingerprint` inbound (WI-36)

## Примечания

Проверка реальными клиентами §4.2 — на стенде, 001.73b.

Зависимости: 001.41, 001.27. Приоритет: Critical. Оценка: 4 ч. Этап: 6 — subscription-эндпоинт и
логика кабинета.
