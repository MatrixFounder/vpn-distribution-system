# Задача 001.66d: Офлайн-CA серверных сертификатов агентских портов

Тип задачи: `[LOGIC IMPLEMENTATION]`.

## Связь со сценариями
- UC-01 Ввод ноды в эксплуатацию

Требования RTM: R-44, R-02.

<!-- contract:goal -->

## Цель задачи

Снизить принятый риск кражи ключа онлайн-CA через уязвимость `api` (решение заказчика 2026-09-25,
security.md §7.3): серверные сертификаты агентских портов (8443, 8444) подписывает отдельный
офлайн-CA, его сертификат — якорь bootstrap-команды; онлайн-CA в `api` подписывает только листы нод
и остаётся `ssl_client_certificate` nginx. Задача выполняет сторону C-01 (настройка и файл якоря,
`BootstrapToken.ca_pem`), меняет смысл `EnrollOut.ca_pem` в контракте `agent_v1`, профиль CA и
рецепты секретов. Агент 001.54 и bootstrap 001.61 зависят от этой задачи и строятся сразу на
раздельных якоре и CA листов (OPS-06, PLAN-15, `docs/reviews/wi-35-spec-review.md`).

<!-- contract:changes -->

## Описание изменений

### Изменения в существующих файлах

#### Файл: `control-plane/app/config.py`

- настройка `server_ca_cert_path` (`SERVER_CA_CERT_FILE`) — сертификат офлайн-CA серверных
  сертификатов, только роль `api` (как `CA_CERT_FILE`)

#### Файл: `control-plane/app/security/ca.py`

- `get_server_ca_pem()` — сертификат серверного CA процесса: загружается на старте роли `api`
  (lifespan, `app.main`) и запоминается, как `get_ca`; нет переменной или файла — `SecretError`
  (отказ процесса, не 500 у панели); при загрузке — те же стражи профиля, что у CA листов
  (самоподписанный корень, `CA:TRUE`, `keyCertSign`, срок), кроме сверки пары с ключом: ключа
  серверного CA у процесса нет

#### Файл: `control-plane/app/api/admin/nodes.py`

- Изменение принятого кода (отступление от 001.25): `get_node_service` получает якорь из
  `get_server_ca_pem()`, а не из `get_ca_pem()`: `BootstrapToken.ca_pem` ответа
  `POST /admin/nodes/{id}/bootstrap-token` — сертификат серверного CA

#### Файл: `control-plane/app/domain/nodes.py`, `control-plane/app/agent_api/enroll.py`

- Изменение принятого кода (001.25): описания полей — `BootstrapToken.ca_pem` — серверный CA
  (якорь агента для 8443 и 8444), `EnrollOut.ca_pem` (ответ обмена, `NodeService.enroll`) — CA
  листов, с якорем не совпадает; поведение `enroll` не меняется

#### Файл: `contracts/agent_v1/README.md`, `contracts/agent_v1/enroll.json`

- Изменение контракта agent_v1: `ca_pem` ответа обмена — CA листов (им агент проверяет
  выданный лист), а не якорь серверов; якорь — сертификат серверного CA из bootstrap-команды; в
  фикстуре — якорь в `given`, отличный от `ca_pem` ответа; сверку агента меняет 001.54

#### Файл: `deploy/compose/docker-compose.yml`

- `api` — файл сертификата серверного CA и `SERVER_CA_CERT_FILE` в базовом файле (страж
  запрещает оверлею секреты `api`); `tls/agent.crt` nginx — лист серверного CA;
  `ssl_client_certificate` — прежний `tls/ca.crt` (CA листов)

#### Файл: `deploy/compose/secrets/README.md`

- рецепт офлайн-CA: ключ создаётся и хранится вне хоста, на хост попадают только его сертификат
  и подписанный им `tls/agent.crt`; онлайн-CA (`ca_key`, `tls/ca.crt`) — только листы нод и
  `ssl_client_certificate`; профиль обоих CA — как в рецепте 001.25 (`pathlen:0`,
  `keyCertSign`, `cRLSign`)

#### Файл: `deploy/scripts/dev-secrets.sh`

- два CA стенда: серверный подписывает `tls/agent.crt` и `tls/public.crt`, CA листов —
  `dev/dev-node.crt`; ключ серверного CA стенда — файл `server_ca_key` в корне каталога секретов
  стенда, вне `tls/` (каталог `./secrets/tls` монтирует nginx) и вне `dev/`; секретом Compose не
  объявлен и ни одной службе не монтируется (страж — TC-UNIT-04); профиль CA — по примечанию
  «профиль сертификата CA»

#### Файл: `control-plane/tests/_pki.py`

- `make_ca` по умолчанию — профиль `basicConstraints=critical,CA:TRUE,pathlen:0` и
  `keyUsage=critical,keyCertSign,cRLSign` (параметры негодных профилей для стражей старта
  остаются)

#### Файлы тестов

- `control-plane/tests/unit/security/test_ca.py` — дополнить: TC-UNIT-01, TC-UNIT-03
- `control-plane/tests/unit/domain/test_nodes.py` — дополнить: TC-UNIT-02
- `control-plane/tests/unit/test_proxy_contract.py` — дополнить: TC-UNIT-04
- `control-plane/tests/e2e/test_nodes.py` — дополнить: TC-E2E-01
- `control-plane/tests/contract/test_agent_v1.py` — дополнить: фикстура `enroll.json` (TC-E2E-02)
- `control-plane/tests/stand/enrollment_probe.py` — дополнить: TC-E2E-03

### Интеграция компонентов

Сверка `ca_pem` с выданным листом вместо сравнения с якорем — агент 001.54; перенос
`BootstrapToken.ca_pem` в `server-ca.pem` — bootstrap 001.61. Резервная копия каталога `tls/` —
001.67 (ключ офлайн-CA в копию хоста не входит). Отказ отозванному листу на прокси — 001.66c.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Выдача токена отдаёт серверный CA
   - Входные данные: `POST /admin/nodes/{id}/bootstrap-token` при двух разных CA
   - Ожидаемый результат: `ca_pem` — сертификат серверного CA, не `tls/ca.crt`
2. **TC-E2E-02:** Контракт обмена
   - Входные данные: фикстура `contracts/agent_v1/enroll.json`
   - Ожидаемый результат: `ca_pem` ответа — CA листов, выданный лист им проверяется; якорь из `given` от `ca_pem` отличается
3. **TC-E2E-03:** Якорь проверяет агентские порты стенда (на VM через `ssh vm`, скилл `vm-deploy`)
   - Входные данные: секреты `dev-secrets.sh`; клиент Python со строгой проверкой (`VERIFY_X509_STRICT`) и якорем серверного CA, затем с CA листов; лист, выпущенный обменом
   - Ожидаемый результат: с якорем — 8444 и 8443 приняты по имени; с CA листов — отказ проверки сервера; лист обмена принят 8443

### Модульные тесты

1. **TC-UNIT-01:** Загрузка серверного CA
   - Проверяемая функция: `app.security.ca.get_server_ca_pem`
   - Входные данные: нет `SERVER_CA_CERT_FILE`; нет файла; `CA:FALSE`; без `keyCertSign`; промежуточный CA; годный корень
   - Ожидаемый результат: `SecretError` в пяти случаях; PEM годного корня
2. **TC-UNIT-02:** Раздельные якорь и CA листов
   - Проверяемая функция: `NodeService.issue_bootstrap_token`, `NodeService.enroll`
   - Входные данные: служба нод с якорем серверного CA и CA листов
   - Ожидаемый результат: `BootstrapToken.ca_pem` — серверный CA; `EnrollOut.ca_pem` — CA листов; выданный лист проверяется CA листов и не проверяется якорем
3. **TC-UNIT-03:** Профиль CA
   - Проверяемая функция: `tests/_pki.py::make_ca` по умолчанию
   - Входные данные: CA по умолчанию; серверный лист, подписанный им
   - Ожидаемый результат: `keyUsage` критическое с `keyCertSign` и `cRLSign`, `basicConstraints` `CA:TRUE, pathlen:0`; клиент со строгой проверкой принимает цепочку
4. **TC-UNIT-04:** Ключ серверного CA вне смонтированных каталогов
   - Проверяемая функция: `deploy/scripts/dev-secrets.sh --dir <временный каталог>` и файлы Compose (основа и оверлеи)
   - Входные данные: созданные секреты стенда; пути хоста каждой службы (`_host_sources`, как в `test_only_the_api_holds_the_node_ca_key`)
   - Ожидаемый результат: ключ, чей открытый ключ совпадает с ключом издателя `tls/agent.crt`, лежит только в `server_ca_key` в корне каталога, не в `tls/` и `dev/`; файл не объявлен секретом или `configs` Compose и не входит в пути хоста ни одной службы; подмена — ключ в `tls/` или том nginx на корень `./secrets` — даёт отказ

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/unit/security/test_ca.py tests/unit/domain/test_nodes.py tests/unit/test_proxy_contract.py tests/e2e/test_nodes.py tests/contract/test_agent_v1.py`; проба стенда — `enrollment_probe.py` на VM через `ssh vm`
- Полный набор: `make check` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] `BootstrapToken.ca_pem` — сертификат серверного CA; `EnrollOut.ca_pem` — CA листов; README и
  фикстура `enroll.json` контракта `agent_v1` описывают раздельный смысл
- [ ] Серверные сертификаты 8443 и 8444 подписаны серверным CA; онлайн-CA в `api` подписывает
  только листы нод; ключ серверного CA лежит вне `secrets/tls` (каталог монтирует nginx), не
  объявлен секретом Compose и не смонтирован ни одной службе (TC-UNIT-04)
- [ ] Профиль CA (`keyCertSign`, `cRLSign`, `pathlen:0`) — в `dev-secrets.sh`, рецепте
  `deploy/compose/secrets/README.md` и `tests/_pki.py::make_ca`; строгий клиент Python принимает
  цепочку агентского порта
- [ ] `api` не стартует без сертификата серверного CA или с негодным профилем

## Примечания

`tdd-strict`: enrollment и якорь доверия агента.

Решить здесь (вопросы заказчику до кода, правило 1 «Цикла задачи»):

- держатель ключа офлайн-CA: а) оператор вне хоста, копия у второго держателя доступов (§17.12,
  Н-12), как ключ шифрования копий; б) только оператор. Рекомендация: а — без копии потеря ключа
  требует нового якоря всему парку;
- совпадение CA в промышленном контуре: а) при `APP_ENV=prod` `api` не стартует, если сертификат
  серверного CA совпадает с CA листов; б) не проверять. Рекомендация: а — иначе разделение не
  действует, а ошибка рецепта не видна.

Найдено при 001.25 (enrollment и identity), решить здесь:

- **профиль сертификата CA.** Рецепт `openssl req -x509 -new -key ca_key …` (`dev-secrets.sh`,
  `secrets/README.md`) даёт CA без `keyUsage`: RFC 5280 требует `keyCertSign` (и `cRLSign` для
  CRL выше) у сертификата, которым проверяют подписи листов, и строгие клиенты его отвергают —
  найдено живой пробой 001.25: Python 3.13+ по умолчанию (`VERIFY_X509_STRICT`) не принимает
  цепочку серверного сертификата агентского порта («CA cert does not include key usage
  extension»). nginx при проверке клиента и Go-агент этого не требуют, поэтому стенд работает;
  промышленный CA выпускать с `-addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext
  "keyUsage=critical,keyCertSign,cRLSign"` (`pathlen:0` — промежуточных нет, как и
  `ssl_verify_depth 0`: в OpenSSL глубина считает промежуточных CA), тестовый
  `tests/_pki.py::make_ca` — привести к тому же профилю; рецепт `deploy/compose/secrets/README.md`
  исправлен в 001.25 (раунд 2), `dev-secrets.sh` — ещё нет;
- пара `ca_key`/`tls/ca.crt` читается `api` один раз на процесс (`security.ca.get_ca`): смена CA
  — перезапуск `api` и перечитывание nginx вместе, иначе листы новым ключом прокси не примет.

Найдено при 001.25, раунд 2 роаста (решить здесь):

- серверные сертификаты enrollment-server и агентского server выпускаются **внутренним CA** —
  агент проверяет их якорем из bootstrap-команды (security.md §7.1, 001.54): профиль
  `basicConstraints=CA:FALSE`, `extendedKeyUsage=serverAuth`, SAN — адреса Control Plane
  (рецепт `deploy/compose/secrets/README.md` исправлен в 001.25: раньше `-copy_extensions copy`
  переносил только SAN, и лист годился и как клиентский сертификат агентского порта). После
  разделения их выпускает серверный CA.

Найдено при 001.25, раунд 5 (решить здесь):

- **ключ онлайн-CA в `api` — принятый риск** (решение заказчика 2026-09-25, security.md §7.3):
  сертификат того же CA — единственный якорь агента для серверных сертификатов Control Plane, и
  чтение файлов или выполнение кода в любом публичном маршруте `api` дало бы ключ и подмену
  Control Plane перед нодами. Снижение — здесь: отдельный офлайн-CA серверных сертификатов
  агентских портов (он подписывает `agent.crt`, его сертификат — якорь bootstrap-команды, 001.61
  и 001.54), онлайн-CA — только для листов нод и `ssl_client_certificate`. Ключ онлайн-CA даёт и
  листы с серийными номерами по выбору: identity они не дают, но номер чужой ноды занимает её
  пределы по сертификату (слот отчётов — 429 ей), свежие номера — бюджеты без счёта, и карта
  отказа по `cert_serial` (001.66c) такой лист от листа жертвы не отличит — смена CA после кражи
  ключа обязательна (роаст 001.25, раунд 6);
- **сторона C-01 при разделении CA** (роаст 001.25, раунд 6): панель отдаёт якорь в ответе
  выдачи токена (`BootstrapToken.ca_pem`, сегодня — `get_ca_pem()` онлайн-CA в
  `app/api/admin/nodes.py`) — после разделения это сертификат серверного CA: у `api` — новый
  секрет с ним и настройка; `EnrollOut.ca_pem` (ответ обмена, `NodeService.enroll`) остаётся CA
  листов — с якорем он больше не совпадает, и его смысл в фикстуре
  `contracts/agent_v1/enroll.json` и README контрактов переписывается вместе со сверкой агента
  (001.54).

Нод в промышленном контуре до приёмки нет — перевыпуск парка при разделении не нужен; стенд
пересоздаёт секреты `dev-secrets.sh`.

Зависимости: 001.25. Приоритет: Critical. Оценка: 3 ч. Этап: 4 — парк нод и Node API.
