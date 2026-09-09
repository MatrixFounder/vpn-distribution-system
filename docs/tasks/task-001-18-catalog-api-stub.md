# Задача 001.18: API администратора: тарифы, группы, коды — маршруты и заглушки

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-09 Управление тарифами, группами и кодами

Требования RTM: R-18, R-30, R-32, R-33.

<!-- contract:goal -->

## Цель задачи

Объявить CRUD-маршруты `/api/v1/admin/plans`, `/admin/groups`, `/admin/codes` со схемами и
заглушками, проходящими сквозные тесты на фиксированных ответах.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `control-plane/app/api/admin/plans.py` — `GET/POST /admin/plans`, `PATCH/DELETE /admin/plans/{id}`; схема `PlanIn`: `name`, `price_amount?`, `price_currency?`, `duration_days`, `traffic_limit_bytes?`, `device_limit?`, `access_group_ids[]`, `profiles[]`
- `control-plane/app/api/admin/groups.py` — `/admin/groups/access` и `/admin/groups/billing` CRUD; `POST /admin/groups/billing/{id}/multiplier` (новый интервал)
- `control-plane/app/api/admin/codes.py` — `POST /admin/codes` (один), `POST /admin/codes/batch`, `GET /admin/codes/batch/{id}/export` (CSV), `GET /admin/codes/{id}/redemptions`
- `control-plane/app/domain/plans.py` — `class PlanService` — заглушки
- `control-plane/app/domain/groups.py` — `class GroupService` — заглушки
- `control-plane/app/domain/codes.py` — `class CodeService`: `create`, `create_batch(n, spec) -> batch_id`, `export(batch_id) -> Iterable[str]`, `redeem(user_id, code) -> Redemption` — заглушки; `def checksum_ok(code: str) -> bool` реализуется сразу: чистая функция без внешних зависимостей, тест в этой задаче
- `control-plane/tests/e2e/test_catalog.py` — сценарий UC-09 на заглушках

### Интеграция компонентов

Маршруты защищены `require_permission` (заглушка 001.12). Схемы совпадают с полями
`docs/architectures/data-model.md` §4.2.2 и §4.2.4.

Уточнения при реализации:

- защита операций панели: заглушка `require_permission` 001.12 поднимает
  `NotImplementedError` (500) и в маршруты не подключена; вместо неё каждая операция объявляет
  **одно** разрешение (R-35) в OpenAPI полем `x-permission` (`plans.read|write`,
  `groups.read|write`, `codes.read|write`, `dashboard.read`; тест сверяет, что оно есть у всех
  операций раздела), а проверку по матрице «роль × операция» подключает 001.46; до неё
  операции защищены сессией администратора: `security/deps.py::current_admin` реализован в
  минимальном виде (сессия вида `admin` по cookie `sid` с UUID-субъектом, иначе 401
  `unauthenticated`; сессия пользователя панель не открывает); выдача таких сессий (пароль +
  TOTP) и проверка подтверждённого второго фактора — 001.47, поэтому на стенде панель
  недоступна никому, а тесты создают сессию администратора прямо в Redis (`tests/e2e/_admin.py`);
- раздел `/admin` включён с `redis_required` (сессии в Redis → без Redis 503, fail-closed
  §9.1), мутации — под `require_csrf` (§7.3); заглушка `admin.dashboard` 001.10 перенесена в
  `api/admin/__init__.py` под сессию администратора (прежде отвечала 501 без сессии);
- маршруты: `plans` — `GET/POST /admin/plans`, `PATCH/DELETE /admin/plans/{plan_id}` (удаление
  = архивирование, UC-09 A4, 204); `groups` — CRUD `/admin/groups/access` и
  `/admin/groups/billing` (`{group_id}`), `POST /admin/groups/billing/{group_id}/multiplier`;
  `codes` — `POST /admin/codes`, `POST /admin/codes/batch`, `GET
  /admin/codes/batch/{batch_id}/export` (`text/csv`, заголовок `code,expires_at,plan`), `GET
  /admin/codes/{code_id}/redemptions` — всего восемнадцать операций с `dashboard`;
- схемы — по колонкам `data-model.md`: `PlanIn(name 1…100, price_amount ≥ 0 (12,2)?,
  price_currency ISO 4217 из трёх заглавных букв?, duration_days > 0, traffic_limit_bytes ≥ 0?
  (None — Unlimited), device_limit ≥ 1?, access_group_ids[], profiles[] ≥ 1 из перечисления
  `inbound_profile`)`, `PlanPatch` (все поля необязательны), `Plan` (+ `id`, `status`
  `active|archived`, времена); `AccessGroupIn(name, description)`, `BillingGroupIn(name)`,
  `BillingGroup` с `current_multiplier`; `MultiplierIn(multiplier Decimal 0…10 с шагом 0.1 —
  UC-09 A2 проверяется схемой, valid_from?)`, `Multiplier` (интервал, `valid_to` null —
  действует); `CodeSpec(kind redeem|promo, plan_id?, expires_at?, max_uses ≥ 1,
  max_uses_per_user ≥ 1, traffic_bonus_bytes ≥ 0, duration_bonus_days ≥ 0)`, `Code` (сам код
  показывается один раз при создании — в базе хеш и шифртекст, §4.2.4), `BatchIn(count 1…10000,
  spec)`, `BatchOut`, `Redemption`; тела запросов принимают неизвестные поля молча (модели без
  `extra="forbid"` — как в data-model, где лишних полей нет; строгий режим — при логике 001.19);
  семантика `PATCH` (ревью раунда 1, L-1): непереданное поле не меняется (`model_fields_set`),
  поле, переданное как `null`, сбрасывается (`traffic_limit_bytes: null` — Unlimited, цена без
  значения), а `null` для полей NOT NULL (`name`, `duration_days`, `access_group_ids`,
  `profiles`, у групп — `name`, `description`) — 422;
- контрольная сумма кода (`domain/codes.py`, реализовано): формат `VPN-XXXX-XXXY`, алфавит из
  32 символов без `I`, `O`, `0`, `1`; контрольный символ — взвешенная сумма индексов семи
  символов полезной части с нечётными весами 1…13 по модулю 32: любая подмена одного символа
  ловится всегда, перестановка соседних — кроме пары с разностью индексов 16 (объявлено),
  случайный код проходит с вероятностью 1/32; `normalize_code` терпит регистр и отсутствие
  дефисов, `checksum_ok` на не-строке — ложь без исключения; выдача — `generate_code()` без
  генератора берёт `secure_source()` = `random.SystemRandom` (страж: подмена источника на
  детерминированный — красная, ревью раунда 1, C-1); перебор случайных кодов (1/32) сдерживает
  порог §5.12 — 001.20; `CodeService.redeem` уже отклоняет неверную сумму
  400 `invalid_code` без обращения к базе (§16.8), остальное в `redeem` — заглушка (логика
  001.20; маршрут `POST /me/subscription/redeem` из 001.15 к сервису подключает 001.20);
- заглушки: `PlanService`/`GroupService` возвращают переданные поля под фиксированными
  идентификаторами (`STUB_PLAN`, `STUB_ACCESS_GROUP`, `STUB_BILLING_GROUP`) без записи в базу,
  удаления/архивирование ничего не делают; `CodeService.create` выдаёт настоящий сгенерированный
  код с фиксированным id, `create_batch` — фиксированный `batch_id`, `export` — заголовок и три
  сгенерированных кода (строки CSV — модуль `csv`: кавычки и запятые в имени тарифа
  экранируются, ячейка с ведущими `= + - @` получает апостроф от формул электронных таблиц;
  ответ — `text/csv` с CRLF, `Content-Disposition: attachment` и `Cache-Control: no-store` —
  коды в открытом виде не кэшируются), `redemptions` — одна запись; аудит (UC-09 шаг 7) —
  001.46/001.19; тег OpenAPI `admin` — только на родительском роутере; передано в 001.19:
  имя тарифа сейчас допускает перевод строки (`min_length=1, max_length=100` без шаблона) — при
  логике либо запретить его в имени, либо отдавать CSV целиком, а не построчно (ревью N-8), и
  нормализовать коэффициент к `multiplier_milli` (эхо `1E+1` — N-5);
- тесты 001.10/001.12 скорректированы: `admin.dashboard` больше не открытая заглушка 501,
  независимость от Redis показывает `/openapi.json`; `current_admin` в `test_stubs` — 401 без
  cookie вместо `NotImplementedError`.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** CRUD тарифа на заглушках
   - Входные данные: создание, чтение, изменение, архивирование
   - Ожидаемый результат: фиксированные ответы 201/200/200/204
   - Примечание: заглушка
2. **TC-E2E-02:** Экспорт партии
   - Входные данные: `GET /admin/codes/batch/{id}/export`
   - Ожидаемый результат: `text/csv` с заголовком `code,expires_at,plan`
   - Примечание: заглушка

### Модульные тесты

1. **TC-UNIT-01:** Контрольная сумма кода
   - Проверяемая функция: `app/domain/codes.py::checksum_ok`
   - Входные данные: `VPN-ABCD-EFGH` с верной и неверной суммой
   - Ожидаемый результат: истина / ложь без обращения к базе

### Регрессионные тесты

- Команда: `cd control-plane && pytest tests/e2e/test_catalog.py tests/unit/domain/test_codes.py`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [x] Маршруты в OpenAPI со схемами
- [x] `checksum_ok` реализована (чистая функция, §16.8)
- [x] Сквозные тесты проходят на заглушках

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Зависимости: 001.12, 001.05. Приоритет: High. Оценка: 3 ч. Этап: 3 — тарифы, подписки, коды.
