# Задача 001.29: Поток состава: `updated_seq`, вентиль по статусу, `resync_required`

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-01 Ввод ноды в эксплуатацию
- UC-05 Исчерпание лимита и отзыв доступа
- UC-10 Восстановление Control Plane из резервной копии
- UC-11 Обновление парка нод

Требования RTM: R-04.

<!-- contract:goal -->

## Цель задачи

Реализовать дельту потока состава по `docs/architectures/interfaces.md` §5.2 и `data-model.md`
§4.2.3: выделение `updated_seq` через `UPDATE nodes … RETURNING`, вентиль по статусу, продвижение
курсора и `resync_required`, расшифровку credentials только для ноды из identity. Полный снапшот,
поколение и long-poll — 001.75; команды — 001.76.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/tests/e2e/test_agent_api.py` — дельта; вентиль для каждого из восьми статусов

### Изменения в существующих файлах

#### Файл: `control-plane/app/domain/composition.py`

- `publish_user`: для каждой ноды из пересечения групп доступа — upsert `node_user_state` (состояние, `quota_grant_bytes`, `blocked_ips`) с `updated_seq` из `UPDATE nodes SET desired_users_seq = desired_users_seq + 1 … RETURNING`; создание `node_user_credentials` при отсутствии; `NOTIFY` + `PUBLISH node:{id}` после коммита
- `state_for` (дельта): вентиль по таблице §5.2 (`pending` 403; `provisioning` только конфигурация; `disabled`/`suspended` только отзывы), продвижение курсора до max `updated_seq` выборки, `resync_required`, расшифровка credentials только для `node_id` из identity

#### Файл: `control-plane/app/jobs/handlers/composition.py`

- обработчик `composition.publish_user` в очереди `critical`

### Интеграция компонентов

Вызывается из `SubscriptionService` (001.22), `LimitsService` (001.35), `DeviceLimitService`
(001.38), `NodeService` (001.25). Пробуждение long-poll — Redis pub/sub.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Дельта доставляется за бюджет
   - Входные данные: перевод пользователя в `suspended_quota`; агент удерживает long-poll
   - Ожидаемый результат: ответ не позднее 5 с; строка со состоянием; `users_seq` продвинут (Н-13)
2. **TC-E2E-02:** В `provisioning` состав пуст
   - Входные данные: нода `provisioning`
   - Ожидаемый результат: `users.rows == []`; ни одного credential (AC-13)
3. **TC-E2E-03:** `suspended` получает только отзывы
   - Входные данные: нода `suspended`; в потоке отзыв и выдача
   - Ожидаемый результат: в ответе только отзыв; `resync_required = true`

### Модульные тесты

1. **TC-UNIT-01:** Выделение `updated_seq` монотонно
   - Проверяемая функция: `app/domain/composition.py::next_users_seq`
   - Входные данные: две конкурентные транзакции
   - Ожидаемый результат: коммиты упорядочены; агент не пропускает строк (тест с задержкой коммита)

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_agent_api.py tests/unit/domain/test_composition.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Вентиль совпадает с матрицей §4.6 построчно
- [ ] `updated_seq` выделяется только через `UPDATE nodes … RETURNING`
- [ ] Credentials расшифровываются только для ноды из identity

## Примечания

`tdd-strict`: контракт `/agent/v1` — ядро системы.

Зависимости: 001.28, 001.22, 001.26. Приоритет: Critical. Оценка: 4 ч. Этап: 4 — парк нод и Node
API.
