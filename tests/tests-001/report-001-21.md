# Отчёт о проверке — задача 001.21 «Служба подписок: интерфейс состояния, периодов и переходов — заглушки»

Дата: 2026-09-22. Стенд: VM (`ssh vm`), Compose `control-plane`. Тесты — с рабочей машины через
`ASGITransport` против живых базы и Redis стенда (`PG_DSN`/`MIGRATE_DSN`/`REDIS_URL` → порты
15432/16379); сессия пользователя настоящая (`_auth.auth_stand`, 001.14).

Runtime дерева: Python 3.14 (`control-plane/pyproject.toml` — `requires-python = ">=3.14"`,
`ruff target-version = "py314"`, `mypy python_version = "3.14"`; в venv 3.14.4).

## Регрессия

- `make check` (окружение стенда в `PG_DSN`/`MIGRATE_DSN`/`REDIS_URL`) → **код 0**: ruff
  check и ruff format --check, mypy strict (138 файлов), pytest **627 passed** за 81,4 с,
  `go build ./... && go test ./...`, gofmt, go vet, golangci-lint 2.13.2, web (eslint,
  prettier, tsc, vitest), `unittest` скриптов документации (10 тестов). Числа — от прогона на
  финальном дереве, после правок раунда 1.
- Набор: **627 passed** против базового `HEAD` (1c7e517) — **612**, прирост **15**.
  Базовое число измерено на чистом дереве: `git stash push -u` → `pytest --collect-only -q` →
  `612 tests collected`; `git stash pop`; восстановление сверено `shasum -a 256 -c` по десяти
  файлам — все совпали. Прирост — 4 сквозных (`tests/e2e/test_subscriptions.py`) и 11 модульных
  (`tests/unit/domain/test_subscriptions.py`, из них 7 — параметризованные случаи стража
  сигнатур); `tests/unit/jobs/test_handlers.py` и `tests/e2e/test_me.py` числа тестов не меняли:
  в первом расширены утверждения внутри прежних трёх тестов, во втором — только перенесён
  помощник.
- Регрессионная команда задачи `cd control-plane && pytest tests/e2e/test_subscriptions.py`
  → 4 passed; вместе с добавленным модульным файлом
  (`… tests/e2e/test_subscriptions.py tests/unit/domain/test_subscriptions.py`) → 15 passed.
- Резолвер позиционных ссылок: `python3 .agent/skills/documentation-standards/scripts/
  check_positional_refs.py --targets-changed` на **финальном** дереве → код 0, «OK: 8 of 8
  reference(s) in 7 document(s) resolve against the working tree»; 88 координат — «not examined»
  (нет смежной цели), это не дефект (§4.1). Прогон сделан после того, как правки стали
  окончательными: ранний прогон (до появления этого отчёта) давал другие числа и предупреждение
  `DRIFT_SUSPECT` на ссылку в `task-001-20`, которую эта же задача правит, — ссылка заменена на
  указание по разделу, и предупреждение снято.
- Развёртка устаревших чисел: шаблон `docs/sweeps/001-21.json` не заводился — задача не меняла
  ни предела, ни частоты, ни числового правила. Единственная смена формулировки — имя операции
  начисления (`bonus` → `add_traffic`); прогон `grep -rn` по `docs/`, `web/`, `node-agent/`,
  `contracts/` нашёл ровно одно прежнее упоминание — в разделе «Описание изменений» самой
  задачи-получателя `docs/tasks/task-001-20-codes-logic.md` (строка про `redeem`), и оно
  объяснено разделом «Найдено при 001.21» ниже по тому же файлу (описание задачи — подписанная
  история, её не переписывают). Ссылка дана по разделу, а не по номеру строки: позиционная
  координата в документ, который эта же задача правит, уедет при следующей его правке.

## Что задача принесла

Служба `SubscriptionService` объявлена заглушкой, но три правила в ней настоящие, и каждое
охраняется:

1. **Набор состояний — один на систему.** `SubscriptionState` объявлен в домене и сверяется с
   `enum_range(null::subscription_state)` живой базы; кабинет (`app/api/me.py`) вторую копию
   литерала потерял и импортирует домен, а `SubscriptionOut.state` в OpenAPI сверяется с тем же
   литералом. Было три возможных списка (база, OpenAPI, домен) — стал один источник и две сверки.
2. **Переход публикует состав.** Состояние подписки едет на ноды потоком состава (§3.4, §4.11), и
   переход, не разбудивший поток, не даёт ни отзыва в пределах Н-14, ни восстановления доступа.
   Все шесть операций перехода вызывают `CompositionService.publish_user` (существует заглушкой с
   001.28), `remaining` как чтение — нет. Требование раздела «Интеграция компонентов» стало
   проверяемым свойством, а не строкой в докстринге.
3. **Заглушка очереди отказывает, а не «успевает».** `subscription.expire` и
   `subscription.notify_expiring` зарегистрированы в `HANDLERS` (тип вне реестра исполнитель не
   выбирает — задачи копились бы в `pending` без красного сигнала) и поднимают
   `NonRetryableError`: «успех» истечения, которого не было, оставил бы пользователя с истёкшей
   подпиской в inbound и отчитался бы об обратном. В `scheduler.SCHEDULE` типы не входят до
   001.22/001.83.

## Сквозные тесты (`control-plane/tests/e2e/test_subscriptions.py`, 4 теста)

1. **TC-E2E-01** `test_activation_answers_the_period_the_cabinet_shows`: `activate` возвращает
   `STUB_PERIOD_ID`; `GET /api/v1/me/subscription` под настоящей сессией → 200, `state = active`,
   `period.id` равен возвращённому периоду, `remaining` равен ответу службы.
2. `test_uc06_walk_on_stubs`: UC-06 по шагам — продление (1–4), смена тарифа администратором
   (A1), начисление трафика (A4), `set_state` в `suspended_quota`, истечение (A3); продление
   возвращает тот же фиксированный период, остаток после прогона прежний. Через кабинет сценарий
   не проходит: маршрутов продления и смены тарифа у `/me` нет (административные — 001.49/001.50;
   погашение кода доводит до службы 001.20), и это названо в докстринге. Сверки ответа кабинета
   до и после прогона тест не делает намеренно: маршрут от службы пока не зависит (связать их
   обязана 001.22), и такая сверка была бы сравнением константы с собой — отсутствие обращений
   к базе закреплено часовым в модульном страже.
3. `test_the_states_are_the_enum_of_the_database`: `SubscriptionState` и `PeriodSource` равны
   `enum_range(null::subscription_state)` и `enum_range(null::period_source)` живой базы —
   порядком и составом.
4. `test_the_cabinet_state_comes_from_the_same_set`: `SubscriptionOut.state` в `/openapi.json`
   перечисляет ровно состояния домена.

## Модульные тесты (`control-plane/tests/unit/domain/test_subscriptions.py`, 11 случаев)

- `test_the_declared_signature_is_the_one_logic_tasks_will_find` ×7: сигнатуры семи методов
  текстом — их найдут 001.22, 001.20, 001.35, 001.16.
- `test_every_transition_wakes_the_composition_stream`: все шесть переходов вызывают
  `publish_user` ровно по разу и за того пользователя, которого им дали (поток — записывающий
  наследник `CompositionService`).
- `test_reading_the_remainder_publishes_nothing`: `remaining` не публикует — иначе поток
  просыпался бы на каждом открытии кабинета.
- `test_the_stub_answers_the_same_period_for_any_arguments`: период фиксирован; аргументы теста
  намеренно отличаются от фиксированных значений, иначе страж не отличил бы выдачу по аргументу
  от выдачи константы.
- `test_the_stub_numbers_agree_with_the_cabinet_the_user_sees`: `STUB_PERIOD_ID`,
  `STUB_LIMIT_BYTES`, `STUB_USED_BILLABLE_BYTES`, `STUB_REMAINING_BYTES` сходятся с
  `accounting.stats.STUB_STATS` и `plans.STUB_PLAN`; сами три числа закреплены литералами
  (107374182400, 21474836480, 85899345920), а не равенством «остаток = лимит − израсходовано» —
  последнее верно по построению констант и не поймало бы их подмену.

Пул и поток состава в модульных тестах — часовые (`Sentinel.__getattr__` валит тест): обращение
заглушки к базе поймано бы здесь.

Расширены прежние три теста `tests/unit/jobs/test_handlers.py` (числа тестов не изменились):
`SUBSCRIPTION_TYPES` в реестре своими обработчиками, отказ без повторов с номером задачи в
причине, отсутствие обоих типов в `SCHEDULE`.

## Посадки стражей

Харнес: точная замена в файле по полному пути (совпадение образца проверяется — иначе «НЕ
ИЗМЕРЕНО»), полный прогон набора **без `-x` и `--maxfail`**, восстановление из резервной копии и
сверка `cmp` побайтно, очистка `__pycache__`, файл-маркер `PLANTED` на время правки. Команда:
`VM_IP=<адрес> control-plane/.venv/bin/python <харнес>/plant.py`. Базовый прогон — 627 passed.

| Посадка | Что снято | Файл | Итог полного прогона | Первые красные |
| :--- | :--- | :--- | :--- | :--- |
| P01 | имя параметра в сигнатуре: plan_id → plan (страж сигнатур) | `control-plane/app/domain/subscriptions.py` | 1 failed, 626 passed | `test_the_declared_signature…[SubscriptionService.activate]` |
| P02 | тип возврата remaining: `int \| None` → `int` (страж сигнатур) | `control-plane/app/domain/subscriptions.py` | 1 failed, 626 passed | `test_the_declared_signature…[SubscriptionService.remaining]` |
| P03 | переход expire не публикует состав (страж публикации) | `control-plane/app/domain/subscriptions.py` | 1 failed, 626 passed | `test_every_transition_wakes_the_composition_stream` |
| P04 | переход set_state не публикует состав (страж публикации) | `control-plane/app/domain/subscriptions.py` | 1 failed, 626 passed | `test_every_transition_wakes_the_composition_stream` |
| P05 | чтение remaining публикует состав (страж «чтение молчит») | `control-plane/app/domain/subscriptions.py` | 1 failed, 626 passed | `test_reading_the_remainder_publishes_nothing` |
| P06 | период зависит от аргументов (страж фиксированного периода) | `control-plane/app/domain/subscriptions.py` | 2 failed, 625 passed | `test_uc06_walk_on_stubs`; `test_the_stub_answers_the_same_period_for_any_arguments` |
| P07 | лимит заглушки разошёлся с кабинетом: 100 ГиБ → 150 ГиБ (страж согласия чисел) | `control-plane/app/domain/subscriptions.py` | 2 failed, 625 passed | `test_activation_answers_the_period_the_cabinet_shows`; `test_the_stub_numbers_agree_with_the_cabinet_the_user_sees` |
| P08 | период заглушки разошёлся со статистикой (страж согласия чисел) | `control-plane/app/domain/subscriptions.py` | 2 failed, 625 passed | `test_activation_answers_the_period_the_cabinet_shows`; `test_the_stub_numbers_agree_with_the_cabinet_the_user_sees` |
| P09 | из набора состояний выпало suspended_admin (страж перечисления базы) | `control-plane/app/domain/subscriptions.py` | 2 failed, 625 passed | `test_eight_operations_in_openapi_with_schemas`; `test_the_states_are_the_enum_of_the_database` |
| P10 | в наборе состояний лишнее removed из user_node_state (страж перечисления базы) | `control-plane/app/domain/subscriptions.py` | 2 failed, 625 passed | `test_eight_operations_in_openapi_with_schemas`; `test_the_states_are_the_enum_of_the_database` |
| P11 | порядок источников периода разошёлся с базой (страж перечисления базы) | `control-plane/app/domain/subscriptions.py` | 1 failed, 626 passed | `test_the_states_are_the_enum_of_the_database` |
| P12 | кабинет вернул себе копию набора состояний (страж единого литерала) | `control-plane/app/api/me.py` | 2 failed, 625 passed | `test_eight_operations_in_openapi_with_schemas`; `test_the_cabinet_state_comes_from_the_same_set` |
| P13 | тип subscription.expire не зарегистрирован (страж реестра) | `control-plane/app/jobs/handlers/__init__.py` | 1 failed, 626 passed | `test_the_new_job_types_are_registered_with_their_own_handlers` |
| P14 | заглушка subscription.expire отвечает «успех» (страж отказа без повторов) | `control-plane/app/jobs/handlers/subscriptions.py` | 1 failed, 626 passed | `test_the_stubs_refuse_without_retries_and_without_touching_the_connection` |
| P15 | заглушка notify_expiring отвечает «успех» (страж отказа без повторов) | `control-plane/app/jobs/handlers/subscriptions.py` | 1 failed, 626 passed | `test_the_stubs_refuse_without_retries_and_without_touching_the_connection` |
| P16 | заглушка subscription.expire попала в расписание (страж расписания) | `control-plane/app/jobs/scheduler.py` | 1 failed, 626 passed | `test_the_stub_types_stay_off_the_schedule_until_they_are_real` |

Ни одной «НЕ ИЗМЕРЕНО»: во всех шестнадцати прогонах красными были именованные тесты из
`tests/`, ошибок сбора и импорта не было. Дерево после прогона сверено `shasum -a 256 -c`
по десяти файлам — все совпали, `PLANTED` снят.


## Раунд 1 — состязательное ревью и правки

Шесть линз только на чтение (соответствие контракту, стражи, логика, безопасность, слоп и
техдолг, документы и числа) на сертифицированном отпечатке дерева; каждая находка прошла
опровержение тремя скептиками с разными углами (чтение кода, сверка с постановкой, проверка
сценария отказа), закрытие — большинством. Вердикт раунда: **REJECTED**.

**46 находок, пережили опровержение две.** Обе исправлены:

- **MAJOR (документы).** Числа резолвера позиционных ссылок в отчёте были от прогона, сделанного
  **до** появления самого отчёта: на финальном дереве команда давала не «8 of 8 … in 6
  document(s)», а девять ссылок в семи документах и предупреждение `DRIFT_SUSPECT` на
  позиционную ссылку в `task-001-20` — документ, который эта же задача правит. Ссылка заменена
  на указание по разделу, прогон повторён на финальном дереве, в отчёт внесён его фактический
  вывод (§4.1: позиционные ссылки проверяются после того, как правки стали окончательными).
- **MINOR (контракт).** В докстринге `remaining` и в примечании 001.22 Н-17б был назван пределом
  перерасхода в автономном режиме. Н-17б — потолок списания за перерасход
  (`Н-17 × traffic_multiplier`), автономный режим ограничен грантом Н-17в; пределы разделены в
  обоих местах.

Остальные 44 закрыты опровержением. Наиболее показательные из закрытых — те, что выглядели
самыми весомыми:

- «страж публикации считает сумму, а не привязывает публикацию к переходу» (CRITICAL/MAJOR у трёх
  линз) — опровергнута 3 из 3: ассерт `composition.published == [USER] * 6` есть точное равенство
  списка и закреплён с двух сторон, поэтому любая одиночная мутация красная (5 ≠ 6 при снятии
  публикации, 7 ≠ 6 при лишней, поэлементное равенство при чужом пользователе); заявленный отказ
  требует двух согласованных правок, а не регрессии. Посадки P03 и P04 это и измерили;
- «сверка набора состояний с OpenAPI тавтологична» — опровергнута: рядом стоит сверка с
  `enum_range` живой базы, и подмену ловит она (посадки P09…P12);
- «комментарий у `SUBSCRIPTION_TYPES` врёт» — опровергнута: реестр читает константы модуля, а
  формулировка дословно повторяет принятую в 001.33.

## Критерии приёмки

- [x] **Сигнатуры объявлены** — семь методов закреплены текстом
  (`unit/domain/test_subscriptions.py`); посадки P01 (переименование параметра) и P02 (сужение
  типа возврата) красные.
- [x] **Состояния `none | active | suspended_quota | suspended_admin | expired` определены** —
  набор сверяется с `enum_range(null::subscription_state)` живой базы и с `SubscriptionOut.state`
  в OpenAPI; посадки P09 (выпало состояние), P10 (лишнее `removed`), P11 (порядок
  `period_source`), P12 (кабинет завёл свою копию) красные.
- [x] **Тесты проходят на заглушках** — 627 passed (базовый `HEAD` — 612), из них 15 новых;
  регрессионная команда задачи — 4 passed.


## Отклонения от описания задачи

- «Модульные тесты не требуются» — добавлен `tests/unit/domain/test_subscriptions.py`: критерий
  «сигнатуры объявлены» иначе ничем не охраняется, а свойство «переход будит поток состава»
  объявлено самим разделом «Интеграция компонентов». Так же поступили 001.28 и 001.33.
- `renew` возвращает `PeriodId` — в описании тип возврата объявлен только у `activate`. Продление
  начинает новый период (§4.11), и его ключ нужен 001.20 для `balance_entries` бонуса того же
  погашения.
- Правки в существующих файлах, которых описание не называло: `app/api/me.py` (импорт литерала
  вместо копии), `app/jobs/handlers/__init__.py` (реестр), `tests/e2e/_auth.py` +
  `tests/e2e/test_me.py` (перенос `Cabinet`/`logged_in` в общий помощник — вошедший пользователь
  понадобился второму модулю), `tests/unit/jobs/test_handlers.py` (два типа в прежних стражах).
  Поведение соседних задач ни одна из них не меняет.
- Параметр `bytes` у `add_traffic` затеняет встроенный тип внутри метода; имя оставлено из
  описания задачи — по нему сигнатуру ищут 001.22, 001.20 и 001.35.

## Передано соседним задачам

- **001.22** — куда ложится начисление `add_traffic` (§4.11 требует увеличить лимит периода,
  §4.2.4 объявляет `used_billable_bytes` суммой `balance_entries` с источником `bonus` — два
  места для одного начисления, и выбор виден в §4.12); `remaining` ограничивается нулём
  (перерасход до Н-17б делает разность отрицательной, а `remaining` в моделях ответа — `ge=0`);
  `renew` возвращает `PeriodId`; перевод переходов на outbox §5.4 обязан сохранить страж
  публикации; `tests/e2e/test_subscriptions.py` уже создан здесь.
- **001.20** — метод начисления называется `add_traffic`, а не `bonus`.
