# Отчёт о проверке — задача 001.18 «API администратора: тарифы, группы, коды — маршруты и заглушки»

Дата: 2026-09-10 (раунд 2 после ревью — см. «Раунд 2»; разделы ниже описывают текущее
состояние кода). Стенд: VM (`ssh vm`), Compose `control-plane` — все роли `Up`, `api`
healthy. Тесты — с рабочей машины через `ASGITransport` против живых базы и Redis стенда; сессия
администратора создаётся тестами прямо в Redis (вход администратора с TOTP — 001.47).

## Регрессия

- `make check` (nvm use 24, `PG_DSN`/`MIGRATE_DSN`/`REDIS_URL` → стенд) → код 0: ruff, ruff
  format, mypy strict (92 файла), pytest **264 passed** (прежние 223 + 001.18: 41, включая 18 + 13
  параметризованных случаев), go, web.
- `cd control-plane && pytest tests/e2e/test_catalog.py tests/unit/domain/test_codes.py` →
  45 passed. Тесты контрольной суммы были красными (`ModuleNotFoundError`) до реализации.

## Стенд (через nginx)

```text
GET  /api/v1/admin/plans без сессии → 401 unauthenticated
POST /api/v1/admin/codes без сессии → 401
/openapi.json: 18 операций /api/v1/admin, у каждой x-permission; схемы Settings нет
```

## Сквозные тесты (`control-plane/tests/e2e/test_catalog.py`, 38 тестов; `_admin.py` — стенд)

1. `test_admin_operations_in_openapi_with_one_permission_each`: ровно восемнадцать операций
   раздела (`dashboard`, 4 тарифов, 9 групп, 4 кодов), у каждой одно `x-permission` (R-35),
   раздел которого равен сегменту пути, а действие — `read` у `GET` и `write` у мутаций; у
   `GET`/`DELETE` нет `requestBody`; тег `admin` один; у экспорта только `text/csv`; схемы `PlanIn` (восемь
   полей §4.2.2) и `CodeSpec` (семь полей §4.2.4) по колонкам data-model; экспорт объявляет
   `text/csv`; схемы `Settings` в компонентах нет.
2. `test_operations_require_an_admin_session` ×18: без cookie, с несуществующим `sid` и с
   сессией **пользователя** → 401 `unauthenticated`.
3. `test_every_mutation_requires_csrf` ×13: под сессией администратора без `X-CSRF-Token` → 403.
4. **TC-E2E-01** `test_uc09_plan_crud_on_stubs`: `POST` 201 с переданными полями и
   `status = active`, `GET` 200 список, `PATCH` 200 под тем же id — `null` сбрасывает лимит в
   Unlimited, непереданный `device_limit` не тронут, `null` для `name`/`duration_days`/
   `profiles`/`access_group_ids` → 422; `DELETE` 204 (архивирование, UC-09 A4); 422 — срок 0,
   валюта строчными, профиль вне перечисления, пустые профили, лимит устройств 0, отрицательная
   цена, пустое имя, не-UUID в пути.
5. `test_uc09_groups_and_multiplier_step`: группы доступа и тарифицируемые (201/200/204),
   коэффициент `0`, `0.1`, `2.0`, `10`, `9.9` → 201; `2.05`, `10.1`, `-0.1`, `abc`, `0.15` → 422
   (UC-09 A2).
6. **TC-E2E-02** `test_uc09_codes_batch_export_and_redemptions`: один код → 201 с кодом, который
   проходит `checksum_ok`; партия → 201 с `batch_id` и `count`; экспорт → 200 `text/csv` с
   CRLF, `Cache-Control: no-store`, `Content-Disposition: attachment`, первая строка
   `code,expires_at,plan`, каждая строка — код с верной суммой, срок и UUID тарифа; использования → 200 со схемой `Redemption`; 422 — вид `gift`, `max_uses` 0,
   отрицательный бонус, `count` 0.
7. `test_redeem_rejects_bad_checksum_without_database`: `CodeService(pool=None).redeem` с
   неверной суммой → `ApiError` 400 `invalid_code` без обращения к пулу; верный код в нижнем
   регистре доходит до заглушки (§16.8).
8. `test_user_logout_all_keeps_admin_session`: «выход везде» настоящего пользователя (свой
   субъект) убивает его сессию и не трогает сессию администратора.

Скорректированы: `test_skeleton.py::test_stubs_return_501` — без `admin.dashboard` (теперь под
сессией администратора); `test_security_fail_closed.py` — все восемнадцать операций `/admin`
без Redis → 503 с `Retry-After`, независимость показывает `/openapi.json`;
`test_stubs.py` — `current_admin` без cookie → 401.

## Модульные тесты

- **TC-UNIT-01** `unit/domain/test_codes.py`: для 300 сгенерированных кодов — любая подмена
  одного символа (все 31 альтернативы в каждой позиции полезной части и контрольного символа)
  → ложь; перестановка соседних символов полезной части → ложь, кроме пары с разностью индексов
  16 (свойство суммы по модулю 32, объявлено); регистр, дефисы, краевые пробелы не важны;
  мусор, лишняя длина, символы вне алфавита (`I`, `O`, `0`, `1`), чужой префикс → ложь без
  исключений; `EXAMPLE_VALID` (`VPN-ABCD-EFG` + контрольный) истина, `EXAMPLE_INVALID` ложь;
  1000 кодов без повторов; веса нечётные; `checksum_char` короткой полезной части →
  `ValueError`. `test_issued_codes_come_from_the_system_source_not_the_module_generator`: выдача
  без генератора спрашивает `secure_source()` при каждом вызове (подменённый детерминированный
  источник воспроизводится повтором), `secure_source()` — `SystemRandom`, генератор Мерсенна в
  выдаче не участвует (его `getrandbits` подменён на ошибку — боевая выдача жива, `Random(0)`
  падает), 64 выдачи без повторов. `test_checksum_ok_never_raises_on_non_strings`: `None`,
  число, `bytes`, список, словарь, NUL-строка, строка в 10 000 символов → ложь.
  `test_csv_rows_escape_and_neutralize`: кавычки и запятые экранируются, ведущие `=`/`-`
  получают апостроф, переводов строк в ячейках нет.

## Посадки стражей (до ревью, по одной; файлы восстановлены, `cmp` совпадает)

| Посадка | Результат |
| :--- | :--- |
| 1 `GET /admin/plans` без зависимости `current_admin` | e2e: `200 == 401` |
| 2 `current_admin` принимает сессию пользователя | e2e: `501 == 401` на `dashboard` с сессией пользователя |
| 3 партия без `require_csrf` | e2e: `201 == 403` |
| 4 операция без `x-permission` | e2e: «операция без разрешения (R-35)» |
| 5 коэффициент без проверки шага 0.1 | e2e: `2.05` → `201 == 422` |
| 6 роутер `/admin` без `redis_required` | fail-closed: `401 == 503` |
| 7 `redeem` без проверки суммы | e2e: «DID NOT RAISE ApiError» |
| 8 заголовок экспорта `code,plan,expires_at` | e2e: не равен `code,expires_at,plan` |
| 9 экспорт `text/plain` | e2e: `content-type` не `text/csv` |

После посадок: 42 passed.

## Раунд 2 — правки по ревью и их проверка

Ревью раунда 1 (`sarcasmotron-001-18`, REJECTED): C-1 «`generate_code()` — SystemRandom» не
охранялось (ассерт измерял длину строки; посадка `random.Random(0)` — 261 passed); S-1 страж
`x-permission` проверял форму, не смысл (мутация с `plans.read` — зелёная); L-1 `PATCH` с
`exclude_none` не умел сбросить поле в `null` (Unlimited), тест посылал ровно этот случай без
утверждений; D-1 отчёт и докстроки обещали больше, чем проверялось (лишнее поле, «разные
субъекты»); N-1 двойной тег `admin`; N-2 `text/plain` в OpenAPI при `text/csv` в ответе; N-3
CSV руками без экранирования, без `no-store`/вложения; N-4 `checksum_ok(bytes)` бросал; N-5
`1E+1` в эхо коэффициента (без действий).

- **C-1:** `secure_source()` (= `SystemRandom`) вынесен и охраняется: подмена на
  детерминированный источник воспроизводится повтором, `secure_source` вызывается при каждой
  выдаче, генератор Мерсенна не участвует, 64 выдачи без повторов. Посадки: `secure_source →
  Random(0)` → `isinstance` красный; выдача мимо `secure_source` → «каждая выдача спрашивает
  secure_source(): 0 == 2».
- **S-1:** страж сверяет раздел разрешения с сегментом пути и действие с методом. Посадка
  ревьюера (`plans.read` на `POST /admin/plans`) → `'read' == 'write'`.
- **L-1:** `model_dump(exclude_unset=True)` в `PlanService.update` и обоих `update_*` групп;
  `null` для NOT NULL полей — 422 (валидаторы `_not_null`); тест проверяет сброс лимита,
  неизменность непереданного поля и 422 на четырёх полях. Посадка (`exclude_none`) → «null
  сбрасывает лимит в Unlimited». Объявлено в «Уточнениях».
- **D-1:** докстроки и отчёт приведены к проверяемому; тест «выход везде» переписан на настоящего
  пользователя. **N-1:** тег только на родителе (страж `tags == ["admin"]`). **N-2:** `Response`
  с `text/csv`, в OpenAPI только `text/csv` (страж). **N-3:** `csv_rows` (модуль `csv`,
  апостроф перед `= + - @`), CRLF, `Content-Disposition: attachment`, `Cache-Control: no-store`
  (стражи). **N-4:** `checksum_ok` на не-строке — ложь (страж). **N-5:** без действий (объявлено
  001.19 — нормализация к `multiplier_milli`).

После правок: `make check` → 0, **264 passed**; стенд пересобран, `api` healthy.

### Вердикт раунда 2

`sarcasmotron-001-18`: **APPROVED**. Три посадки ревьюера красные: выдача мимо источника
(`random.choice` при живом `secure_source()`), `plans.read` на `POST /admin/plans`,
`exclude_none` в `PlanService.update`; стенд — 401 без сессии, теги и `content` экспорта
проверены вживую. Вкусовщина закрыта сразу: импорты `csv`/`io` наверху, апостроф и для ведущих
табуляции/возврата каретки (OWASP), точные ассерты CSV с делом табуляции. Замечание N-8 (перевод
строки в имени тарифа и сборка строк CSV) — передано 001.19 вместе с нормализацией коэффициента.

## Критерии приёмки

- [x] Маршруты в OpenAPI со схемами — тест 1, стенд (18 операций)
- [x] `checksum_ok` реализована (чистая функция, §16.8) — TC-UNIT-01, тест 7, посадка 7
- [x] Сквозные тесты проходят на заглушках — 45 passed; полный набор 264 passed

## Отклонения от описания задачи

- `require_permission` (заглушка 001.12) не подключён — операции объявляют `x-permission`, а
  защищены `current_admin`, реализованным здесь в минимальном виде (сессия вида `admin`; TOTP и
  выдача сессий — 001.47, матрица разрешений — 001.46); раздел `/admin` fail-closed без Redis;
  `admin.dashboard` ушла под сессию; `MultiplierIn` проверяет шаг 0.1 схемой; `CodeService.redeem`
  отклоняет неверную сумму без базы. Всё объявлено в «Уточнениях при реализации»; карты
  `app/api/.AGENTS.md`, `app/.AGENTS.md`, `control-plane/.AGENTS.md`.
