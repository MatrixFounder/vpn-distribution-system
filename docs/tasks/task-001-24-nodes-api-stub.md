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

- [x] Маршруты в OpenAPI
- [x] Схема ответа enroll содержит сертификат, CA и токен identity

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Уточнения при реализации:

- маршруты панели (`api/admin/nodes.py`, разрешения `nodes.read|write`, сессия администратора,
  CSRF на мутациях — как у 001.18): `GET/POST /admin/nodes`, `GET/PATCH/DELETE
  /admin/nodes/{node_id}` (удаление = вывод из эксплуатации по §4.6 «Удаление», 204),
  `POST …/bootstrap-token` (201), `POST …/approve`, `POST …/status`, `POST …/revoke-identity`,
  `GET …/state` — десять операций (всего в разделе `/admin` — 28, число закреплено тестом); `POST /agent/v1/enroll` (`agent_api/enroll.py`) без
  сессии, CSRF и `X-Client-Fingerprint` — его обслуживает отдельный `server` nginx (порт
  `ENROLL_PORT`, 001.02; на стенде VM — 9444, агентский — 9443); на агентском `server` и на
  публичном путь отвечает 404 (проверено через nginx);
- схемы — по колонкам `data-model.md` §4.2.3: `NodeIn` (обязательны NOT NULL без умолчаний в
  базе — `code`, `name`, `country` ISO alpha-2, `city`, `provider`, `public_ipv4`,
  `billing_group_id` R-18, `bandwidth_mbps` > 0 §5.9, `max_conn_per_ip` > 0 Н-29 —
  умолчания для двух последних постановка не задаёт, поэтому они не выдуманы), `NodePatch`
  (семантика `null` как у тарифов 001.18), `Node` (+ статус, версии агента и Xray, heartbeat,
  `resync_required`, `decommissioned_at`), `BootstrapToken(node_id, token, expires_at)`,
  `ManualStatusIn(status: maintenance|disabled|suspended|null, reason?)` — `null` снимает ручной
  статус (§4.6: далее автоматика), автоматические статусы через этот маршрут не ставятся (422),
  `NodeState` (статус, `Cursors` обоих потоков §5.2, `Heartbeat`, версии, `Identity`, срок и
  погашение последнего токена), `EnrollIn` (`extra="forbid"`, CSR ≤ 16 КБ — PEM-блок
  `CERTIFICATE REQUEST`, иначе 422 с причиной) → `EnrollOut(client_cert_pem, ca_pem,
  identity_token, node_id)` — ровно четыре поля контракта §5.2; отказ по токену объявлен как
  401 (UC-01 A1, логика 001.25);
- что в заглушке настоящее: bootstrap-токен — 256 бит `secrets.token_urlsafe` со сроком
  `BOOTSTRAP_TOKEN_TTL` = 60 мин (Н-24; тест сверяет `expires_at` с границами момента выдачи
  без допусков, а источник случайности — подменой `secrets` и запретом генератора Мерсенна),
  повторная выдача даёт новый токен; `InternalCA.fingerprint` — SHA-256 от DER сертификата (как
  хранит `node_identities.cert_fingerprint`); `pem_der` принимает ровно один PEM-блок с
  ожидаемой меткой и непустым телом из строк base64, окончания строк LF и CRLF (RFC 7468 §3),
  длина до `MAX_PEM_CHARS` = 64 КБ; identity в `GET …/state` несёт отпечаток именно того
  сертификата, который вернул enroll (сверка UC-01 шаг 6 работает уже на заглушке);
  `sign_csr` — заглушка (фиксированный сертификат), ключ CA из `CA_KEY_FILE` — 001.25;
- границы заглушки объявлены явно: состояния между вызовами она не хранит, но идентификатор
  берёт из пути (карточка, состояние и действия отвечают про запрошенную ноду), статус карточки
  и состояния одной ноды совпадает, переходы показывают `create` (`pending`) и `approve`
  (`provisioning`); `ManualStatusIn.reason` доходит до домена (запись в `audit_log` — 001.48);
  отказ CA при синтаксически верном CSR отображается в 422 `invalid_csr` без текста исключения;
- команда bootstrap (UC-01 шаг 3) в ответ токена не включена: адрес enrollment-порта в
  настройках отсутствует (`.env` знает только порты), команду собирает 001.61 вместе со
  скриптом bootstrap и переменной адреса;
- найдено для 001.25 (записано в её примечаниях): nginx передаёт `$ssl_client_fingerprint` —
  это SHA-1, а модель хранит SHA-256; адрес источника enrollment (UC-01 шаг 6) в
  `node_identities` не хранится;
- тесты: `tests/e2e/test_nodes.py` (контракт OpenAPI с равенством наборов полей, TC-E2E-01,
  идентификатор из пути, валидация записи, enroll без сессии, отказ CA, согласованность карточки
  и состояния), `tests/unit/security/test_ca.py` (разбор PEM и отпечаток),
  `tests/unit/domain/test_nodes.py` (источник токена, срок Н-24, фиксированные значения,
  передача причины), `tests/unit/test_proxy_contract.py` (статическая граница enrollment в
  `nginx.conf`: агентский и публичный `server` не обслуживают `/agent/v1/enroll`); операции нод
  добавлены в `ADMIN_OPERATIONS` (`tests/e2e/_admin.py`, с картой `PATH_PARAMS`/`openapi_path`),
  поэтому сессия, CSRF и fail-closed без Redis для них проверяются существующими
  параметризованными тестами, а их число закреплено ассертом; `test_skeleton.py` больше не ждёт
  501 от enroll.

Зависимости: 001.06, 001.12. Приоритет: Critical. Оценка: 3 ч. Этап: 4 — парк нод и Node API.
