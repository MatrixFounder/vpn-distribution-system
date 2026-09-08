# Задача 001.24: API нод и enrollment: маршруты, bootstrap-токен, заглушки

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-01 Ввод ноды в эксплуатацию
- UC-12 Компрометация ноды

Требования RTM: R-02.

<!-- contract:goal -->

## Цель задачи

Объявить `/api/v1/admin/nodes/*` и `POST /agent/v1/enroll` со схемами и заглушками; сквозной тест
проходит сценарий UC-01 на фиксированных ответах.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/api/admin/nodes.py` — CRUD ноды; `POST /admin/nodes/{id}/bootstrap-token`; `POST /admin/nodes/{id}/approve`; `POST /admin/nodes/{id}/status` (ручные статусы); `POST /admin/nodes/{id}/revoke-identity`; `GET /admin/nodes/{id}/state`
- `control-plane/app/agent_api/enroll.py` — `POST /agent/v1/enroll`: `{bootstrap_token, csr_pem, agent_version, xray_version}` → `{client_cert_pem, ca_pem, identity_token, node_id}`
- `control-plane/app/domain/nodes.py` — `class NodeService`: `create`, `issue_bootstrap_token(node_id) -> str`, `enroll(token, csr) -> Identity`, `approve(node_id, admin)`, `set_manual_status`, `revoke_identity(node_id)` — заглушки
- `control-plane/app/security/ca.py` — `class InternalCA`: `sign_csr(csr_pem, days=90) -> cert_pem`; `fingerprint(cert_pem) -> str` — заглушка
- `control-plane/tests/e2e/test_nodes.py` — UC-01 на заглушках

### Интеграция компонентов

Enrollment обслуживается отдельным `server` nginx (001.02); маршрут не требует клиентского
сертификата.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Ввод ноды на заглушках
   - Входные данные: создание ноды, токен, enroll, approve
   - Ожидаемый результат: статусы `pending` → `provisioning` → `active` в фиксированных ответах
   - Примечание: заглушка

### Модульные тесты

- Не требуются: задача не содержит логики сверх заглушек или конфигурации.

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_nodes.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Маршруты в OpenAPI
- [ ] Схема ответа enroll содержит сертификат, CA и токен identity

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.06, 001.12. Приоритет: Critical. Оценка: 3 ч. Этап: 4 — парк нод и Node API.
