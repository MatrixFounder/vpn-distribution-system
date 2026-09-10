# Отчёт о проверке — задача 001.24 «API нод и enrollment: маршруты, bootstrap-токен, заглушки»

Дата: 2026-09-10 (раунд 2 после ревью — см. «Раунд 2»; разделы ниже описывают текущее состояние
кода). Стенд: VM (`ssh vm`), Compose `control-plane` пересобран после правок — все роли `Up`,
`api` healthy. Тесты — с рабочей машины через `ASGITransport` против живых базы и Redis стенда;
сессия администратора создаётся тестами прямо в Redis (вход администратора с TOTP — 001.47).

## Регрессия

- `make check` (nvm use 24, `PG_DSN`/`MIGRATE_DSN`/`REDIS_URL` → стенд) → код 0: ruff, ruff
  format, mypy strict (99 файлов), pytest **311 passed**, go, web, `lint-plan`.
- Прирост против 264 на `HEAD` — 47 случаев: `tests/e2e/test_nodes.py` 8,
  `tests/unit/security/test_ca.py` 17, `tests/unit/domain/test_nodes.py` 5,
  `tests/unit/test_proxy_contract.py` +1, параметризованные тесты панели +17
  (`test_catalog.py` 36 → 53), `test_skeleton.py` −1 (enroll больше не заглушка 501).

## Стенд (через nginx, порты стенда: enrollment 9444, агентский 9443, публичный 443)

```text
POST 9444 /agent/v1/enroll, CSR с окончаниями CRLF   → 200, ключи ответа
    ['ca_pem', 'client_cert_pem', 'identity_token', 'node_id']
POST 9444 /agent/v1/enroll, PEM-рамка без тела        → 422 validation_error
POST 9444 /agent/v1/enroll, csr_pem "nope"            → 422, msg «ожидается PEM-блок
    CERTIFICATE REQUEST»
GET  9444 /api/v1/admin/nodes                         → 404 (server отдаёт только enroll)
POST 9443 /agent/v1/enroll с dev-сертификатом ноды    → 404 (enroll не на агентском server)
POST 9443 /agent/v1/enroll без клиентского сертификата → 400 (mTLS обязателен)
GET  9443 /agent/v1/state с dev-сертификатом          → 501 not_implemented (заглушка 001.28)
POST  443 /agent/v1/enroll                             → 404 (публичный server не знает /agent)
GET   443 /api/v1/admin/nodes без сессии               → 401 unauthenticated
/openapi.json: 28 операций /api/v1/admin, из них 10 по /nodes; /agent/v1/enroll — один путь
```

## Сквозные тесты (`control-plane/tests/e2e/test_nodes.py`, 8 случаев)

1. `test_node_and_enroll_contracts_in_openapi` — **критерии приёмки**: `POST /agent/v1/enroll`
   в схеме, тег `agent`, без `x-permission`; путей с `enroll` ровно один (второй монтаж роутера
   под `/api/v1` виден тесту, а не только стенду); тело — ровно четыре поля с
   `additionalProperties: false`, все обязательны; ответ — ровно `client_cert_pem`, `ca_pem`,
   `identity_token`, `node_id`, все обязательны; объявлены 401 (UC-01 A1) и 422. Наборы полей
   **равенством**: `NodeIn` — двадцать полей §4.2.3 с девятью обязательными (NOT NULL без
   умолчаний), `NodePatch` — те же поля без `required`, `Node` — `NodeIn` плюс десять полей
   состояния, `NodeState` — одиннадцать, `Cursors` — пять, `Heartbeat` — три, `Identity` — пять,
   `BootstrapToken` — три, `ManualStatusIn` — два. `Node.status` — перечисление `node_status`
   §4.6 в порядке модели; `ManualStatusIn.status` — только три ручных статуса и `null`, поле
   обязательно (снятие — явный `null`).
2. **TC-E2E-01** `test_uc01_node_onboarding_on_stubs` — UC-01 на фиксированных ответах:
   шаг 1 создание → 201 `pending`, версии и heartbeat пусты; шаги 2–3 токен → 201, ровно 43
   символа алфавита base64url, `expires_at` внутри `[before + 60 мин, after + 60 мин]` (Н-24,
   без допусков), повторная выдача даёт другой токен; шаг 5 обмен с другого адреса, без cookie и
   CSRF → 200; шаг 6 `GET …/state` показывает отпечаток **того же** сертификата
   (`InternalCA.fingerprint` от ответа enroll), версии агента и Xray, погашенный токен, и
   статус, совпадающий с карточкой той же ноды; шаг 7 approve → `provisioning`; список → одна
   нода. Далее: три ручных статуса с причиной → 200 с тем же статусом, снятие `null` →
   `active`, отказ 422 на `active`/`pending`/пустом теле/регистре; `revoke-identity` →
   `revoked_at` не пуст при том же отпечатке; `PATCH` меняет имя и не трогает код; `DELETE` →
   204 без тела.
3. `test_operations_answer_about_the_node_from_the_path` — заглушка отдаёт фиксированные
   значения, но идентификатор берёт из пути: карточка, состояние, approve, ручной статус, отзыв
   identity, токен и `PATCH` другой ноды возвращают её `node_id`, а не `STUB_NODE_ID`.
4. `test_node_record_validation` — 14 некорректных тел → 422 (страна строчными и из трёх букв,
   не-IPv4 и IPv6 в `public_ipv4`, IPv4 в `public_ipv6`, нулевая полоса, отрицательный лимит
   соединений, пробелы в коде, пробел в fqdn, валюта строчными, отрицательная цена, не-объект в
   `legal_profile`, пропуск `billing_group_id` и `bandwidth_mbps`); `null` для пяти NOT NULL
   полей в `PATCH` → 422, для необязательных — 200; не-UUID в пути → 422.
5. `test_enroll_validates_body_and_needs_no_session` — валидное тело без cookie и CSRF → 200 с
   `STUB_CLIENT_CERT_PEM`; одиннадцать некорректных тел → 422 единого формата (пустое тело,
   короткий токен, «не CSR», **сертификат вместо CSR**, битый base64 внутри рамки, текст перед
   рамкой, **рамка без тела**, **валидный base64 длиннее `CSR_MAX_CHARS`**, пустая версия,
   лишнее поле, пропуск версии Xray); сообщение об ошибке называет ожидаемую метку PEM; CSR с
   окончаниями строк CRLF (RFC 7468 §3) принимается.
6. `test_csr_rejected_by_the_ca_is_a_request_error_not_a_server_error` — служба, подменённая
   через `dependency_overrides`, бросает `ValueError`: ответ 422 `invalid_csr`, текст исключения
   наружу не выносится (ветка обработки достижима и покрыта, а не мёртвая).
7. `test_state_and_card_agree_on_the_same_node` ×2 — карточка и состояние одной ноды не
   противоречат друг другу по статусу, версиям и `resync_required`.

Расширены существующие: `_admin.py::ADMIN_OPERATIONS` — 10 операций нод (всего 28) с картой
`PATH_PARAMS`/`openapi_path`, поэтому `test_operations_require_an_admin_session` (401 без
сессии, с чужой и с сессией пользователя), `test_every_mutation_requires_csrf` (403, 19 мутаций)
и `test_security_fail_closed` (503 без Redis) покрывают их без нового кода;
`test_admin_operations_in_openapi_with_one_permission_each` сверяет множество операций панели с
перечнем и закрепляет их число (28); `test_skeleton.py::test_stubs_return_501` больше не ждёт
501 от enroll.

## Модульные тесты

- `control-plane/tests/unit/security/test_ca.py`, 17 случаев.
  `test_fingerprint_is_sha256_of_der_regardless_of_line_wrapping`: отпечаток равен SHA-256 от DER
  при любой ширине строк PEM, с краевыми пробелами и без завершающего перевода строки; 64 символа
  в нижнем регистре. `test_crlf_line_endings_are_accepted_as_rfc_7468_requires`: CRLF даёт тот же
  DER и тот же отпечаток, подпись принимает такой CSR.
  `test_fingerprint_rejects_anything_but_one_certificate_block` ×13: пустая строка, не-PEM,
  `CERTIFICATE REQUEST` вместо `CERTIFICATE`, `PRIVATE KEY`, несогласованные BEGIN/END, битый
  base64, обрезанная длина, мусор до и после рамки, два блока подряд, **рамка без содержимого**,
  **пустая строка внутри тела**, **валидное тело длиннее `MAX_PEM_CHARS`** → `ValueError`.
  `test_pem_der_names_the_reason_it_refused`: сообщение отличает чужую метку, рамку без тела,
  не-base64, несогласованные BEGIN/END, не-строку и превышение длины.
  `test_stub_sign_csr_requires_a_csr_and_returns_the_fixed_certificate`: подпись принимает только
  CSR и возвращает `STUB_CLIENT_CERT_PEM`, отпечатки сертификата ноды и CA различны,
  `CERT_DAYS = 90`, сертификат вместо CSR и `days = 0` → `ValueError`.
- `control-plane/tests/unit/domain/test_nodes.py`, 5 случаев.
  `test_bootstrap_token_comes_from_the_os_source_not_the_module_generator`: выдача идёт через
  `secrets.token_urlsafe` ровно с `BOOTSTRAP_TOKEN_BYTES` (256 бит), генератор Мерсенна не
  участвует (его `getrandbits` подменён на ошибку — боевая выдача жива), 64 токена без повторов,
  каждый 43 символа из алфавита base64url. `test_stub_values_do_not_contradict_the_model`:
  группа доступа ≠ тарифицируемая, карточка и состояние согласованы, `pending` без версий.
  `test_token_expiry_is_measured_from_the_moment_of_issue`: `expires_at = now + TTL` (Н-24).
  `test_identity_fingerprint_belongs_to_the_issued_certificate`: отпечаток identity считается от
  выданного сертификата, отзыв проставляет `revoked_at`.
  `test_manual_status_reason_reaches_the_domain`: маршрут передаёт причину ручного статуса в
  домен (запись в `audit_log` — 001.48), а не выбрасывает её.
- `control-plane/tests/unit/test_proxy_contract.py` — добавлен
  `test_enrollment_is_served_only_by_its_own_server`: статический разбор `deploy/nginx/nginx.conf`
  — enrollment проксируется единственным `server` (8444, без `ssl_verify_client on`) и ровно
  одним точным `location`; агентский `server` (8443, mTLS) отдаёт на этот путь 404 и не
  проксирует его; публичный не знает `/agent`. Граница §7.1 больше не держится только на ручном
  curl.

## Посадки стражей (раунд 1: до ревью; раунд 2: после правок; по одной, файлы восстановлены, `cmp` совпадает)

| Посадка | Результат |
| :--- | :--- |
| 1 `fingerprint` считает SHA-256 от текста PEM, не от DER | unit: 1 failed |
| 2 `sign_csr` принимает любой PEM | unit: 1 failed |
| 3 валидатор `csr_pem` в теле enroll снят | e2e: 1 failed |
| 4 `EnrollIn` без `extra="forbid"` | e2e: 1 failed |
| 5 `BOOTSTRAP_TOKEN_TTL` = 61 минута | e2e: 1 failed (Н-24) |
| 6 ручной статус допускает `active` | e2e: 1 failed |
| 7 отпечаток identity не от выданного сертификата | e2e: 1 failed |
| 8 `approve` без `require_csrf` | e2e: 1 failed |
| 9 разрешение `revoke-identity` — `nodes.read` | e2e: 1 failed |
| 10 `GET …/state` без `Admin` | e2e: 1 failed |
| 11 `null` для `public_ipv4` в `PATCH` принимается | e2e: 1 failed |
| 12 маршрут `/enroll` переименован | e2e: 1 failed |
| 13 разбор PEM снова допускает рамку без тела | unit: 1 failed |
| 14 нормализация CRLF снята | unit: 1 failed |
| 15 `MAX_PEM_CHARS` поднят | unit: 1 failed |
| 16 enroll подключён вторым деревом под `/api/v1` | e2e: 1 failed |
| 17 `decommissioned_at` убрано из `Node` | e2e: 1 failed |
| 18 `bootstrap_token_expires_at` убрано из `NodeState` | e2e: 1 failed |
| 19 `node_id` из пути игнорируется | e2e: 1 failed |
| 20 `CSR_MAX_CHARS` поднят | e2e: 1 failed |
| 21 токен из `random` вместо `secrets` | unit: 1 failed |
| 22 состояние и карточка снова расходятся по статусу | e2e: 1 failed |
| 23 `reason` снова выбрасывается маршрутом | unit: 1 failed |
| 24 ошибка CA становится 500 вместо 422 | e2e: 1 failed |
| 25 группа доступа снова равна тарифицируемой | unit: 1 failed |
| 26 nginx: агентский `server` проксирует enroll | unit: 1 failed |
| 27 nginx: enrollment-`server` раздаёт весь `/agent/v1` | unit: 1 failed |

Посадка 26 наблюдалась дважды: намеренно и случайно — один из агентов ревью изменил
`deploy/nginx/nginx.conf` в рабочем дереве и не восстановил его; новый статический страж
покраснел на этой правке раньше, чем она попала бы в коммит (см. «Раунд 2», процесс).

## Раунд 2 — правки по ревью и их проверка

Ревью раунда 1 (`sarcasmotron-001-24`: шесть независимых линз — контракты, логика,
безопасность, тесты, слоп, документация; каждая находка проверялась тремя опровергателями,
выжившей считалась находка с большинством голосов «не опровергнуто»). Выжило 12 записей — пять
различных дефектов, найденных разными линзами.

- **C-1 (все четыре линзы, тавтология).** `assert "/agent/v1/enroll" not in {p … if
  p.startswith("/api/")}` истинен по построению: множество содержит только пути с `/api/`.
  Заменён на равенство множеств `{p … if "enroll" in p} == {"/agent/v1/enroll"}`. Посадка 16
  (второй монтаж роутера под `/api/v1`) — красная.
- **S-1 (тесты, слоп).** Наборы полей `Node`, `NodeState`, `Cursors`, `Heartbeat` не
  проверялись (для `NodeState` стояло `⊇`), поэтому выпавшее поле контракта проходило молча:
  pydantic игнорирует лишний аргумент конструктора заглушки. Все наборы сверяются равенством.
  Посадки 17 и 18 — красные.
- **S-2 (тесты, безопасность).** Предел размера CSR объявлен задачей, но не охранялся, а имя
  `CSR_MAX_BYTES` описывало байты при ограничении в символах. Константа переименована в
  `CSR_MAX_CHARS` с пояснением про `client_max_body_size 64k` на enrollment-сервере; добавлен
  случай с валидным base64 длиннее предела. Посадка 20 — красная.
- **D-1 (три линзы).** Отчёт и задача считали 11 операций нод и 29 операций панели; в коде их 10
  и 28. Числа исправлены и **закреплены кодом**: `assert len(ADMIN_OPERATIONS) == 28` рядом со
  сверкой множества операций.
- Разбивка «296 = 264 + 32» заменена точной: 47 новых случаев, включая −1 в `test_skeleton`.

Опровергнутые голосованием находки, которые оказались верными при прямой проверке, тоже
исправлены — вердикт большинства не заменяет запуск кода:

- **рамка PEM без тела принималась** (`pem_der` возвращал `b""`, enroll отдавал 200 на «CSR» из
  двух строк рамки): класс тела содержал `\n`. Разбор переписан — тело состоит из непустых строк
  base64, разделитель вне класса (заодно исчезла неоднозначность разбора на длинном входе).
  Посадка 13 — красная.
- **CRLF отвергался** вопреки RFC 7468 §3: добавлена нормализация окончаний строк и предел
  `MAX_PEM_CHARS`. Посадки 14 и 15 — красные.
- **`node_id` из пути игнорировался**: карточка, состояние и действия отвечали про
  `STUB_NODE_ID`. Заглушка эхом возвращает идентификатор из пути. Посадка 19 — красная.
- **состояние противоречило карточке** (`pending` против `active` у одной ноды): статус
  состояния по умолчанию согласован с карточкой, переходы показывают `create` и `approve`.
  Посадка 22 — красная.
- **`ManualStatusIn.reason` принимался и выбрасывался маршрутом**: причина доходит до домена
  (`set_manual_status(..., reason=)`), запись в `audit_log` — 001.48. Посадка 23 — красная.
- **источник случайности токена не охранялся** (тот же класс, что C-1 задачи 001.18): добавлен
  страж `secrets.token_urlsafe` с подменой источника и запретом генератора Мерсенна. Посадка
  21 — красная.
- **граница enrollment держалась только ручным curl**: добавлен статический разбор
  `nginx.conf`. Посадки 26 и 27 — красные.
- **группа доступа = тарифицируемая группа** в фиксированных значениях: разведены. Посадка 25 —
  красная.
- **ветка `invalid_csr` была недостижимой**: сохранена как отображение отказа CA в 422 (001.25
  разбирает CSR по-настоящему), но теперь достижима через подменённую службу, покрыта тестом,
  объявлена в OpenAPI и не выносит текст исключения наружу. Посадка 24 — красная.

Отклонено: `legal_profile` не сделан обязательным (в базе `NOT NULL DEFAULT '{}'::jsonb` —
правило «обязательны NOT NULL без умолчаний» соблюдено).

Найдено в самих правках раунда 2 собственными посадками: два стража длины (PEM и CSR) сначала
меряли не длину, а испорченный base64, а после первой правки вычисляли размер входа из той же
константы, которую проверяли, — и оставались зелёными при снятом пределе. Размеры заданы
литералами, значения констант закреплены отдельными ассертами.

Процесс: агенты ревью работали инструментами записи и один из них оставил в рабочем дереве
подсаженную регрессию (`deploy/nginx/nginx.conf`) и временный файл теста. Дерево проверено
`git status`, посторонние правки сняты, конфигурация восстановлена из `HEAD` и стенд пересобран
уже на восстановленной конфигурации; сигнал записан в ретро.

После правок: `make check` → 0, **311 passed**; стенд пересобран, `api` healthy, дымовые
проверки выше выполнены заново.
