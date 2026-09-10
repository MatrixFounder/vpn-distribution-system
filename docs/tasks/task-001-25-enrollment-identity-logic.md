# Задача 001.25: Enrollment и identity ноды: внутренний CA, bootstrap-токен, подтверждение, отзыв

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-01 Ввод ноды в эксплуатацию
- UC-12 Компрометация ноды

Требования RTM: R-02, R-44.

<!-- contract:goal -->

## Цель задачи

Реализовать выдачу одноразового bootstrap-токена (Н-24), обмен на клиентский сертификат внутреннего
CA, подтверждение администратором и отзыв identity (Н-31) по `docs/architectures/security.md` §7.1.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/tests/e2e/test_nodes.py` — A1 просроченный токен, A2 отклонение, повторное использование токена, запрос с отозванной identity → 401

### Изменения в существующих файлах

#### Файл: `control-plane/app/security/ca.py`

- генерация CA при первом старте, ключ в `FieldCipher`; `sign_csr` на 90 дней; `fingerprint` SHA-256

#### Файл: `control-plane/app/domain/nodes.py`

- `issue_bootstrap_token`: 256 бит, хеш в `bootstrap_tokens`, `expires_at = now() + 60 мин`, аннулирование прежнего
- `enroll`: проверка хеша и срока, одноразовость (`used_at`), подпись CSR, запись `node_identities` (`generation`), статус `pending`
- `approve`: сверка признаков (адрес источника, отпечаток, версии) → `provisioning`; `NodeService.on_active` при первом heartbeat после применения конфигурации → `active` и публикация состава (001.29)
- `revoke_identity`: `revoked_at`, статус `disabled`, событие для ротации (001.29)

#### Файл: `control-plane/app/agent_api/deps.py`

- зависимость `current_node`: отпечаток из заголовка прокси → `node_identities` (не отозвана) + токен identity; `node_id` берётся только из identity

### Интеграция компонентов

Заголовок отпечатка выставляет nginx (001.02, правило границы 1). Ротация identity с окном
перекрытия (Н-28) — 001.31.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Повторное использование токена отклонено
   - Входные данные: `enroll` дважды одним токеном
   - Ожидаемый результат: второй — 401 `token_used` (AC-13)
2. **TC-E2E-02:** Токен старше 60 минут отклонён
   - Входные данные: токен с `expires_at` в прошлом
   - Ожидаемый результат: 401 `token_expired`
3. **TC-E2E-03:** Отозванная identity
   - Входные данные: `revoke-identity`, затем запрос агента
   - Ожидаемый результат: 401; статус `disabled`
   - Примечание: AC-14

### Модульные тесты

1. **TC-UNIT-01:** Отпечаток сертификата
   - Проверяемая функция: `app/security/ca.py::fingerprint`
   - Входные данные: тестовый сертификат
   - Ожидаемый результат: SHA-256 в нижнем регистре без разделителей

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_nodes.py tests/unit/security/test_ca.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Bootstrap-токен одноразовый со сроком Н-24
- [ ] В `pending` запрос состояния отклоняется 403
- [ ] Отзыв identity выполняется не позднее Н-31 (операция синхронная)

## Примечания

`tdd-strict` для `enroll` и `current_node`.

Найдено при 001.24 (заглушки), решить здесь:

- отпечаток: nginx отдаёт `$ssl_client_fingerprint` — это SHA-1, а `node_identities.cert_fingerprint`
  и `InternalCA.fingerprint` — SHA-256 от DER; варианты — передавать `$ssl_client_escaped_cert`
  заголовком и считать SHA-256 в C-01 (сохраняет модель) или хранить SHA-1 (меняет
  data-model.md §4.2.3 и security.md §7.1); выбранный вариант закрепить в `deploy/nginx/nginx.conf`
  и `tests/unit/test_proxy_contract.py`;
- адрес источника enrollment, который администратор сверяет на шаге 6 UC-01, в модели не
  хранится — нужна колонка (например, `node_identities.enrolled_from inet`) или запись в
  `audit_log` с чтением в `GET /admin/nodes/{id}/state`;
- в заглушке 001.24 отказ по токену объявлен как 401 (UC-01 A1): неизвестный, использованный и
  просроченный токен отвечают одинаково.

Зависимости: 001.24. Приоритет: Critical. Оценка: 4 ч. Этап: 4 — парк нод и Node API.
