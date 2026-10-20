# Отчёт о проверке — задача 001.34 «Приём отчёта: идемпотентность, допуск времени, порог аномалии»

Дата: 2026-10-19 — 2026-10-20. Стенд: VM (`ssh vm`), Compose `control-plane`, PostgreSQL 18.6 (стенд поднят
заново — контейнеры стояли с 2026-10-05). Тесты — с рабочей машины против живой базы стенда через
туннель (`PG_DSN`/`MIGRATE_DSN`/`REDIS_URL` → порты 15432/16379). Миграция задачи — 140, на
стенде применена и проверена откатом и повтором.

Runtime дерева: Python 3.14 (`control-plane/pyproject.toml` — `requires-python = ">=3.14"`,
`ruff target-version = "py314"`, `mypy python_version = "3.14"`); asyncpg ≥ 0.31.

## Регрессия

- Базовое число на чистом `HEAD` (`5518c60`): `pytest --collect-only -q` → **802 tests
  collected**; полный прогон того же дерева против стенда — **802 passed** за 98,82 с.
- `make check` на окончательном дереве (после раунда 3; код и тесты — те же, что у деревьев
  посадок `9a8ebcc1f25e` и `5a9223fe46b9`: после них менялись только документы, которых тесты не
  читают, — сверка sha256 изменённых файлов со снимками; окружение стенда в
  `PG_DSN`/`MIGRATE_DSN`/`REDIS_URL`) → **код 0**: ruff check и ruff format
  --check, `docs/PLAN.md: актуален`, mypy (150 файлов, «Success: no issues found»), pytest **859
  passed** за 101,78 с (до посадок после раунда 3 — 859 passed за 105,95 с), `go build ./... && go
  test ./...` — OK. Деревья, отданные роастам: раунд 1 — 843 passed, раунд 2 — 847, раунд 3 — 849.
- Прирост **57** = 802 → 859, по файлам (`--collect-only` на `HEAD` в отдельном worktree и на
  дереве задачи): `tests/e2e/test_reports.py` 103 → 137 (35 новых на живой базе минус снятый
  `test_a_report_is_accepted_with_the_stub_answer`), `tests/unit/accounting/test_acceptance.py`
  0 → 15, `tests/contract/test_agent_v1.py` 34 → 38 (четыре новые фикстуры),
  `tests/e2e/test_schema_accounting.py` 4 → 6, `tests/unit/db/test_schema_accounting.py` 14 → 15
  (права на `report_intervals`), `tests/unit/accounting/test_report_schema.py` 11 → 12 (метка вне
  UTC, раунд 3).
- Регрессионная команда задачи `cd control-plane && pytest tests/e2e/test_reports.py
  tests/unit/accounting` → **184 passed** за 9,61 с.
- Развёртка устаревших чисел: `docs/sweeps/001-34.json` (восемнадцать шаблонов), `python3
  docs/scripts/sweep_stale.py --patterns docs/sweeps/001-34.json --since 5518c60` → совпадений
  56, в истории 38, вне истории 18 — все объяснены (раздел «Развёртка»).

## Что задача принесла

`AccountingService.accept_report` перестал быть заглушкой: часть отчёта принимается в своей
транзакции по правилам §5.9 и контракта частей (README `contracts/agent_v1/`), состояние
интервала хранится в `report_intervals` (миграция 140), ответ — наибольший принятый номер эпохи.

1. **Перепроверка под блокировкой (001.25, раунд 2).** Строка ноды — `FOR NO KEY UPDATE`: один
   приём ноды за раз и порядок с отзывом identity, который держит ту же строку. Отдельным
   оператором после блокировки (в read committed — новый снимок) — identity запроса по отпечатку
   (не отозвана, не истекла, нода не выведена — иначе 401) и вентиль §4.6 (`pending` — 403).
2. **Ключ идемпотентности** `(node_id запроса, counter_epoch, report_seq)`. Повтор принятого или
   удержанного — 200 `duplicate: true` с наибольшим принятым номером эпохи; повтор отказанного с
   теми же записанными полями (границы, число частей, счётчики интерфейса) — тот же отказ с
   `reason: repeated`; тот же номер с другим из них — 422 `seq_reused`. Запись — `INSERT … ON
   CONFLICT DO NOTHING`.
3. **Время.** `period_end` не позже часов сервера плюс 5 с (`future`). Новый интервал — не
   старше 72 ч с допуском (`too_old`, раунд 2) и не раньше конца последнего интервала ноды, и
   через смену эпохи (`overlap`). Первый интервал ноды (у неё ещё нет ни одного; решение
   заказчика 2026-10-20 после раунда 2) — не раньше выдачи identity запроса и суток назад с
   допуском, и не раньше первого назначения ноды без допуска (`before_anchor`). Всё — 409
   `rejected_time`, часть записана со статусом.
4. **Части и интервалы.** `parts_total` не меньше прежнего и применяется до диапазона; номер — от
   первой принятой до первой + `parts_total` − 1 (`out_of_range`); закрытый интервал — `closed`
   (оба — 409 `interval_closed`, записаны); счётчики интерфейса — только у первой части,
   пользователь — в одной части (422 `report_inconsistent`). Интервал завершается последней
   частью (`completed`), закрывается неудержанной частью следующего интервала (`superseded`),
   сроком 25 ч от последней части (`deadline`) и потолком 48 ч от первой (`age`) — моментом
   истечения, а не приёма.
5. **Порог §5.9.** Полоса × длительность интервала на **сумму uplink + downlink** (байт
   пользователя проходит порт ноды дважды; решение заказчика 2026-10-20) к нарастающей сумме
   принятых частей. Сверх — `held_anomaly` (200, точка продолжения сдвигается), к сумме — доля в
   пределах порога, разложенная по направлениям пропорционально части. Потолки: 48 смен эпохи за
   сутки — смена соседних интервалов, возврат к прежней эпохе тоже (сверх — удержание, интервал
   открывается с `held`); 8 открытых интервалов — удержанная часть сверх потолка удерживается, её
   интервал рождается закрытым (`capped`) и не списывается (решение заказчика 2026-10-20).
   Удержанная часть интервалы до себя не закрывает.
6. **Состав ноды.** Строки и адреса пользователей вне `node_user_state` этой ноды отбрасываются
   с алертом при любом исходе части, кроме повтора ключа (он отвечает до разбора состава),
   остальное принимается.
7. **Алерты и метрика.** Записи журнала `app.accounting.service` (ERROR — `rejected_time`,
   `held_anomaly` с причинами и объёмами — после фиксации; `foreign_users` — при любом исходе,
   кроме повтора ключа;
   `report_inconsistent`; WARNING — `interval_closed`, `repeated`); ряд
   `control_plane_report_intervals{node_id,le}` — интервалы, открытые за последний час, по
   длительности.

### Решения

Пункты «решить здесь» задачи — как решены.

- **Столбец «Учитывается трафик» §4.6.** Отчёт принимается от ноды в любом подтверждённом
  статусе: `provisioning` — чтобы нода освободила буфер, `offline`/`disabled`/`suspended` — из
  буфера. Не тарифицировать `provisioning` — шаг списания 001.77 по статусу, прочитанному под той
  же блокировкой (передано: `_lock` статус пока не сохраняет).
- **Принадлежность пользователей — отбрасывание строки, а не отказ отчёта.** Отказ целиком терял
  бы трафик законных пользователей той же части (номер пропускается) и не защищал бы лучше: в
  обоих случаях чужой пользователь не получает ни списания, ни блокировки адреса. Строки и адреса
  пользователей вне `node_user_state` (любого состояния — `removed` тоже член состава: буфер
  досылает трафик до отзыва) отбрасываются, событие — ERROR `foreign_users` с числами. К суммам
  интервала идут только строки состава. После уплотнения `removed` (композиция состава) поздний
  трафик удалённого пользователя из буфера отбрасывается как чужой — без переплаты, с алертом;
  до уплотнения — принимается.
- **Окно приёма — явная проверка (раунд 2).** В раунде 1 окно выводилось из правил, и вывод
  оказался неверным: якорь действует только без открытых интервалов, и цепочка интервалов,
  каждый из которых закрывает недосланный предыдущий, отставала бы от часов без предела.
  Теперь `period_start` нового интервала не старше `REPORT_ACCEPT_WINDOW` — 72 ч (сутки буфера
  до первой части открытого интервала плюс его потолок возраста 48 ч) с допуском 5 с, иначе 409
  `rejected_time` с причиной `too_old`. Это внутри 14 суток партиций `traffic_lines`; впереди —
  не дальше 5 с (партиции на 7 суток вперёд).
- **Коды отказов.** `rejected_time` и `interval_closed` — **409** (конфликт с состоянием сервера:
  часы, интервалы), `details.reason` и `details.last_accepted_seq`, **без `Retry-After`**: часть не
  повторяется, следующая идёт сразу (пауза остановила бы слив буфера). Правило контрактного теста
  «у 403/409/426 обязателен `Retry-After`» уточнено: кроме 409 приёма отчёта. Противоречия
  принятому (`parts_total_decreased`, `counters_in_continuation`, `user_in_another_part`,
  `seq_reused`) — **422 `report_inconsistent`**, ключ не записывается (ошибка агента, как отказ
  модели). Агенту — пропустить номер, как после 409, и поднять алерт (README).
- **Схема.** `CHECK (report_seq >= 1)` и `traffic_reports.parts_total` (1…1000) — миграция 140;
  счётчик принятых частей `report_intervals.parts_accepted` вместо пересчёта строк; суммы по
  направлениям там же. Колонка `parts_total` добавляется с умолчанием 1, которое сразу снимается:
  писателя таблицы до 001.34 не было, умолчание — только для строк, оставшихся от тестов.
- **Граница транзакции** заложена: `accept_report` открывает транзакцию, `_Acceptance` возвращает
  `Outcome` (статус, точка продолжения, причины, доля, порог, состав) — шаги 001.77 продолжат её
  на том же подключении.
- **Бюджет пути приёма.** Синхронной работы в цикле событий не добавлено; транзакция — до 12
  операторов в худшем пути (новый интервал со сменой эпохи без удержания при истёкших открытых;
  закрытие истёкших — один `executemany` до восьми строк), 11 — у первого интервала ноды
  (якорь вместо смен эпохи: истёкших у ноды без интервалов нет) и у продолжения (раунд 3,
  F-7: прежнее «до 13» складывало якорь и смену эпохи, которые взаимоисключающие). Счёт смен
  эпохи читает интервалы от предшественника окна по индексу `(node_id, first_received_at)`, а
  не всю историю ноды (раунд 3, P-1). Резерв 50 мс гейта прокси — по-прежнему оценка; замер —
  001.73 (передано).
- **Ключ — по ноде identity запроса**, поле тела — сверка (`own_node`, 403 `node_mismatch`, до
  транзакции).
- **Нумерация после деления.** Номера частей, отказанных по размеру, приём не видит вовсе
  (413/422 модели — до службы), запоминать их нечего; отказанные 409 записаны — номер пропущен, а
  повтор того же тела отвечает тем же отказом.
- **`counter_epoch`.** Происхождение эпохи сервер не проверяет; правило одно — монотонность
  интервалов через смену эпохи, **без допуска** к концу прежней (перекрытие нарушило бы `EXCLUDE`
  и было бы двойным временем). Отдельного правила «базовая точка 0» на сервере нет: первый
  интервал эпохи ограничен тем же порогом полосы × длительность. Потолок — не больше 48 смен
  среди интервалов, открытых за скользящие сутки; смена — соседние по времени интервалы с
  разными эпохами, и возврат к прежней эпохе — тоже смена (раунд 1 считал различные эпохи —
  чередование двух эпох потолок обходило); интервал, менявший эпоху сверх потолка, —
  удержание.
- **Удержанный отчёт сохраняется целиком.** Строка отчёта и состояние интервала — здесь; строки и
  адреса удержанной части пишет шаг фактов 001.77 в той же транзакции, `billable` — только доля в
  пределах порога (правило деления доли между строками — передано), у части сверх потолка
  открытых интервалов — ноль.
- **Порог — на сумму направлений (решение заказчика 2026-10-20, вместо раунда 8 001.33).** Порт
  VPS один: байт пользователя входит от одной стороны и выходит к другой, поэтому физически
  `uplink + downlink ≤ полоса × время`. Правило раунда 8 (каждое направление ≤ полоса × время)
  позволяло подставной ноде заявить до двух пропускных способностей порта; честную ноду новый
  предел не задевает. Суммы интервала хранятся по направлениям, порог — к их сумме; остаток
  удержанной части раскладывается пропорционально части: uplink — вниз, downlink — дополнение
  до остатка.
- **Часть сверх потолка открытых интервалов (решение заказчика 2026-10-20).** Удерживается и не
  списывается до разбора оператором: восемь открытых удержанных интервалов — частей, которых нода
  не досылает, — уже значат, что самоотчёту ноды не верят. Её интервал рождается закрытым
  (`capped`): остальные части — `interval_closed`, перекрывающие — `overlap`, повторного
  списания нет; места среди открытых он не занимает. Потолок считает именно открытые интервалы:
  удержанный однокусочный закрывается сразу (`completed`, `held`) и места не занимает, истёкший
  по сроку — тоже. Обратная сторона (роаст раунда 2, L-2): нода, держащая восемь удержанных
  многокусочных интервалов открытыми, уходит от списания следующих удержанных частей — стопор
  этого пути — перевод в `suspended` по повторным удержаниям (001.30/001.35, передано).
- **Якорь и первое назначение (передано из 001.23).** Якорь — не раньше первого назначения
  **без допуска**: раньше него `resolve` отвечает 1.0 по умолчанию. Нода без назначений якорем не
  останавливается — её шаг списания отвечает `NoBillingAssignmentError` (001.77).
- **Граница `bandwidth_mbps` (передано из 001.23) — 100 000 Мбит/с** в `CHECK` (миграция 140) и
  в модели записи ноды (`BANDWIDTH_MBPS_MAX`). Свойство в тесте: при 10.0 и полном канале год
  одной ноды по порогу на сумму направлений — 3,942·10¹⁷ байт сырого объёма, 3,942·10¹⁸ байт
  списания ≤ 2⁶³ − 1; граница вдесятеро шире уже выходит за `bigint`.
- **Блокировка строки ноды** держится всю транзакцию приёма (`FOR NO KEY UPDATE`): обновления
  строки `nodes` (heartbeat, статусы 001.30) ждут её — на время транзакции приёма, без
  взаимоблокировки (приём читает `node_identities` и назначения без блокировок, а отзыв и обмен
  берут строку ноды первыми).
- **Якорь — только для первого интервала ноды (решение заказчика 2026-10-20 после раунда 2).**
  Правило контракта «и первого после закрытия всех» срабатывало почти на каждом интервале:
  однокусочный закрывается сразу, и «открытых нет» — обычное состояние. Тогда отказывали
  интервал, начатый до выдачи новой identity (ротация, Н-28), и старейшие части буфера при
  сливе после суточной недоступности Control Plane (у суточной границы — 5 с запаса против часа
  у срока 25 ч). Теперь после первого интервала ноды держат монотонность и окно приёма 72 ч
  (`too_old`); нода после долгой паузы отчитывается до 72 ч назад, а не до 24.
- **Метка времени приводится к UTC моделью (раунд 3, F-5).** Грамматика допускает год 0001 с
  положительным смещением и год 9999 с отрицательным; такой момент в UTC вне `datetime`, и
  asyncpg падал при кодировании параметра — 500 и бесконечный повтор агента. Теперь
  непредставимая метка — 422 `validation_error` модели, края диапазона в UTC доходят до приёма
  (409 `too_old` и `future`).
- **Счётчики интерфейса порогом не облагаются**: «суммарный отчёт ноды» §5.9 прочитан как сумма
  строк пользователей (база списания); показания интерфейса сверяет 001.37.

## Тесты

Сквозные на живой базе — `control-plane/tests/e2e/test_reports.py`, раздел 001.34 (35; стенд —
`tests/e2e/_reporting.py`):

- `test_tc_e2e_01_a_repeated_report_changes_nothing_and_says_duplicate`
- `test_tc_e2e_02_growth_beyond_the_channel_is_held_and_charged_up_to_it`
- `test_the_threshold_follows_the_length_of_the_interval` (раунд 3)
- `test_tc_e2e_03_a_period_ahead_of_the_server_clock_is_refused`
- `test_a_decreasing_counter_is_refused_by_the_model_and_leaves_no_trace`
- `test_parts_complete_an_interval_and_a_part_past_it_is_refused`
- `test_the_declared_number_of_parts_only_grows_and_is_applied_before_the_range`
- `test_a_continuation_carries_no_interface_counters_and_no_user_of_another_part`
- `test_intervals_of_a_node_are_monotonic_across_epochs`
- `test_the_first_interval_is_anchored_to_the_buffer_depth`
- `test_the_anchor_binds_only_the_first_interval_of_a_node`
- `test_the_first_interval_is_anchored_to_the_identity_and_the_first_assignment`
- `test_the_anchor_takes_the_first_assignment_and_none_does_not_stop_it` (раунд 3)
- `test_moments_at_the_edge_of_the_range_get_a_contract_answer` (раунд 3)
- `test_an_interval_closes_on_the_next_interval_the_deadline_and_its_age`
- `test_a_new_interval_older_than_the_acceptance_window_is_refused`
- `test_a_continuation_is_not_bound_by_the_acceptance_window` (раунд 3)
- `test_a_held_part_does_not_close_the_interval_before_it`
- `test_a_part_that_is_not_held_closes_the_intervals_before_it`
- `test_a_held_continuation_does_not_close_the_intervals_before_it` (раунд 3)
- `test_a_clock_stepped_back_between_parts_keeps_the_interval`
- `test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed`
- `test_a_held_single_part_interval_takes_no_place_among_the_open` (раунд 3)
- `test_epoch_changes_are_capped_per_day`
- `test_an_epoch_change_across_the_window_edge_is_counted`
- `test_an_epoch_hold_over_the_open_cap_is_born_closed` (раунд 3)
- `test_epoch_changes_are_read_by_the_index_from_the_predecessor_on` (раунд 3)
- `test_lines_and_addresses_of_users_outside_the_node_are_dropped`
- `test_a_revocation_during_the_request_stops_the_report`
- `test_two_acceptances_of_a_node_take_turns` (раунд 3)
- `test_the_continuation_point_and_repeated_refusals`
- `test_every_approved_status_has_its_reports_accepted`
- `test_interval_durations_are_a_metric_per_node`
- `test_a_failing_interval_series_does_not_mark_the_database_down`
- `test_the_recorded_report_answers_are_what_the_live_service_gives`

Модульные — `control-plane/tests/unit/accounting/test_acceptance.py` (10 функций, 15 случаев с
параметрами):

- `test_tc_unit_01_the_threshold_is_bandwidth_times_duration`
- `test_the_threshold_counts_microseconds_and_floors`
- `test_a_part_within_the_threshold_is_charged_in_full`
- `test_a_part_over_the_threshold_is_held_and_charged_up_to_it`
- `test_a_sum_already_over_the_threshold_charges_nothing_more`
- `test_an_open_interval_closes_at_its_deadline_or_its_age_whichever_comes_first`
- `test_the_bandwidth_bound_keeps_a_node_year_inside_bigint`
- `test_the_node_record_bounds_the_bandwidth`
- `test_the_clock_tolerance_is_five_seconds`
- `test_the_windows_of_acceptance_are_literal`

Модель — `tests/unit/accounting/test_report_schema.py::test_a_moment_outside_utc_is_refused_by_the_model`
(раунд 3). Схема 140 — `control-plane/tests/e2e/test_schema_accounting.py` (2):
`test_migration_140_apply_rollback_reapply`, `test_report_constraints_of_140`; снимок каталога — `tests/unit/db/test_schema_accounting.py` и `test_schema_nodes.py`. Контракт —
`tests/contract/test_agent_v1.py` (четыре новые фикстуры, `RecordedAccounting`, правило
`Retry-After` для 409 отчёта).

## Красная фаза (tdd-strict)

Тесты написаны до логики. Первый прогон — ошибка сбора (имён ещё нет): по Stub-First объявлены
интерфейс и заглушки (`anomaly_threshold` → 0, `charge` без порога, `closing`, `own_node`,
`fingerprint` в сигнатуре, `BANDWIDTH_MBPS_MAX`). Второй прогон выявил ошибки самих тестов
(неоднозначный тип параметра `$n + interval` в стенде и в стражe 140) и ожидаемое следствие
миграции (существующий тест вставлял отчёт без `parts_total`). После их исправления, на
заглушках: **28 failed, 113 passed** (`pytest tests/unit/accounting/test_acceptance.py
tests/e2e/test_reports.py tests/e2e/test_schema_accounting.py`, 24,03 с) — все падения на
утверждениях. Зелёными на заглушках остались ровно поведения, которые уже были: отказ модели на
отрицательной дельте (A3), вентиль статусов, ограничения схемы 140. После реализации одно
ожидание теста оказалось неверным (интервал с пропущенным отказанным номером не завершается —
это и есть контракт), второе — в новом тесте «неудержанное продолжение закрывает интервалы до
себя» (превышение по uplink делало удержанным и продолжение) — оба исправлены в тестах.

## Посадки стражей

Харнес (`plant.py` в каталоге сессии): точная замена в файле по полному пути, образец обязан
совпасть ровно один раз (иначе «НЕ ИЗМЕРЕНО»); правка подтверждена сменой байтов; файл-маркер на
время правки; очистка `__pycache__` и снятие замка yoyo (`migrate --break-lock`) перед каждым
прогоном; проверка связи со стендом (база и Redis через туннель) до и после прогона и поиск
отказа подключения в журнале — иначе «НЕ ИЗМЕРЕНО»; полный прогон набора **без `-x` и
`--maxfail`** — `pytest -q -p no:cacheprovider -rfE` из `control-plane/`; восстановление из копии
в памяти и сверка sha256 с исходным, `git status --porcelain` до и после одинаков. Посадки в
миграцию 140 (M01–M05, R24): откат 140, правка, применение, прогон, восстановление, откат,
применение и страж каталога (`tests/unit/db/test_schema_accounting.py`, `test_schema_nodes.py`)
на восстановленной схеме. Вердикт «КРАСНЫЙ» — только если среди упавших есть все ожидаемые тесты
посадки. Базовый прогон окончательного дерева — 859 passed (`make check`).

История замеров:

- **Раунд 1 (дерево роаста `d9eab1c8496b`)** — 49 посадок (P01–P44, M01–M05): 48 красные на
  ожидаемых тестах; P04 (допуск сужен до 2 с) — красный, но не на ожидаемом тесте: TC-E2E-03
  строил точку «в пределах допуска» из самой константы — входы тестов переведены на литералы
  требований, 13 посадок перемерены, все красные. Под P39 первого прохода три посторонних теста
  `tests/unit/security` упали на таймауте Redis через туннель (шум окружения).
- **Раунд 2 (окончательное дерево)** — 75 посадок: прежние 49 (образцы P02, P21–P23, P25, P37
  обновлены под изменённый код) и 26 новых R01–R26 под правила и стражи раунда 2. Первый проход
  остановлен по просьбе заказчика на P12 (P01–P11 — красные; P12 восстановлена вручную точной
  обратной заменой — харнес, запущенный в фоне, SIGINT игнорирует) и отброшен, после решения о
  пороге на сумму — полный перемер: **75 из 75 красные на ожидаемых тестах, восстановление
  подтверждено во всех 75**, сбоев окружения нет.
- **Раунд 3 (окончательное дерево, `4618c81c1ff4`)** — 89 посадок: прежние 75 (образцы P17 и R04
  обновлены под изменённый код, R12 снята вместе с правилом «якорь первого после закрытия
  всех») и 15 новых R27–R41 под стражи раунда 3. Перемер прерван по просьбе заказчика после
  M05 (дождался её восстановления харнесом, затем остановил на R01 — кодовая посадка — и
  восстановил её вручную точной обратной заменой; дерево проверено отпечатком до продолжения) и
  продолжен с R01: **89 из 89 красные на ожидаемых тестах, восстановление подтверждено во всех
  89**, сбоев окружения нет; посторонний красный под R19 — `test_scheduler_leadership_lock`,
  известный флак RF-1 (`docs/issues/rf-1-scheduler-leadership-lock-flake.md`; код посадкой не
  тронут, вердикт — по ожидаемым тестам).
- **После раунда 3 (окончательное дерево)** — правки находок без четвёртого роаста (решение
  заказчика). 18 новых посадок T01–T18 под стражи и правки раунда 3 (образцы G01–G05, G07–G10,
  G08b, G12–G14 линзы стражей и посадки на правки F-3, F-4, F-5, P-1 и `CHECK` для `capped`) на
  дереве `9a8ebcc1f25e`: **18 из 18 красные на ожидаемых тестах, восстановление подтверждено во
  всех 18**. Затем перемер прежних посадок на дереве `5a9223fe46b9` (от `9a8ebcc1f25e` — только
  документы) по правилу WI-25 (покрыто WI-13 и WI-2): перемеряны 45 посадок, у которых после
  дерева `4618c81c1ff4` изменился код вокруг образца (запрос смен эпохи, миграция 140) или ожидаемый
  тест (TC-E2E-02, тесты окна, отзыва, чужих пользователей, потолка открытых, метрики, ограничений
  140 и каталога, границы полосы), и все, чьи ожидаемые тесты идут через переписанный счёт смен
  эпохи; образец R28 обновлён под новый запрос (снимает то же: предшественник окна заменён первым
  интервалом в окне). **45 из 45 красные на ожидаемых тестах, восстановление подтверждено во
  всех 45**; посторонний красный под P33, M05 и R30 — тот же RF-1 (повтор записан в RF-1). Для
  остальных 44 не изменились ни образец, ни код вокруг него, ни ожидаемые тесты; сухой прогон
  подтвердил, что каждый из 89 образцов совпадает с окончательным деревом ровно один раз
  (R28 — после обновления). Общие для всех тестов правки поведения не меняют: стенд
  `_reporting._stand` выполняет те же операторы (перенесены в `insert_user`; границы вчерашней
  партиции — с явным `+00`), а приведение метки к UTC сохраняет моменты. Итог таблицы: **107 из 107 красные**, строки
  с пометкой «перемер» — окончательное дерево (859 тестов), остальные строки P/M/R — дерево
  `4618c81c1ff4` (849).
- Не посажены (не достижимы поведением сегодня): условие `closed_at is null` и проверка «ровно
  одна строка» в обновлении продолжения (без встречного писателя, минующего блокировку ноды, путь
  недостижим — описано в передаче 001.37) и путь `ON CONFLICT DO NOTHING` → «дубликат» в
  `_accept` и `_refuse` (под блокировкой ноды ключ проверен раньше вставки). Отдельная обработка
  сбоя ряда интервалов в `/metrics`, в раунде 2 не посаженная, с раунда 3 проверена тестом и
  посадкой R41.

| Посадка | Что снято | Итог полного прогона | Красные |
| :--- | :--- | :--- | :--- |
| P01 | повтор принятого ключа не отвечает duplicate | КРАСНЫЙ: 4 failed, 845 passed in 125.06s (0:02:05) | `test_reports.py::test_tc_e2e_01_a_repeated_report_changes_nothing_and_says_duplicate`, `test_reports.py::test_tc_e2e_02_growth_beyond_the_channel_is_held_and_charged_up_to_it`, `test_reports.py::test_the_continuation_point_and_repeated_refusals`, `test_reports.py::test_the_recorded_report_answers_are_what_the_live_service_gives` |
| P02 | номер с другими границами считается повтором (нет seq_reused) | КРАСНЫЙ: 1 failed, 848 passed in 123.97s (0:02:03) | `test_reports.py::test_the_continuation_point_and_repeated_refusals` |
| P03 | допуск часов втрое шире (15 с) | КРАСНЫЙ: 3 failed, 846 passed in 122.65s (0:02:02) | `test_reports.py::test_lines_and_addresses_of_users_outside_the_node_are_dropped`, `test_reports.py::test_tc_e2e_03_a_period_ahead_of_the_server_clock_is_refused`, `test_reports.py::test_the_recorded_report_answers_are_what_the_live_service_gives` |
| P04 | допуск часов уже (2 с) | КРАСНЫЙ: 5 failed, 844 passed in 119.43s (0:01:59) | `test_reports.py::test_a_new_interval_older_than_the_acceptance_window_is_refused`, `test_reports.py::test_tc_e2e_03_a_period_ahead_of_the_server_clock_is_refused`, `test_reports.py::test_the_first_interval_is_anchored_to_the_buffer_depth`, `test_reports.py::test_the_first_interval_is_anchored_to_the_identity_and_the_first_assignment`, `test_acceptance.py::test_the_clock_tolerance_is_five_seconds` |
| P05 | истёкшие интервалы не закрываются по времени | КРАСНЫЙ: 2 failed, 847 passed in 116.59s (0:01:56) | `test_reports.py::test_an_interval_closes_on_the_next_interval_the_deadline_and_its_age`, `test_reports.py::test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed` |
| P06 | причины закрытия по времени перепутаны | КРАСНЫЙ: 3 failed, 846 passed in 117.37s (0:01:57) | `test_reports.py::test_an_interval_closes_on_the_next_interval_the_deadline_and_its_age`, `test_reports.py::test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed`, `test_acceptance.py::test_an_open_interval_closes_at_its_deadline_or_its_age_whichever_comes_first` |
| P07 (перемер) | строки чужих пользователей не отбрасываются | КРАСНЫЙ: 2 failed, 857 passed in 108.07s (0:01:48) | `test_reports.py::test_a_continuation_carries_no_interface_counters_and_no_user_of_another_part`, `test_reports.py::test_lines_and_addresses_of_users_outside_the_node_are_dropped` |
| P08 (перемер) | адреса чужих пользователей не отбрасываются | КРАСНЫЙ: 1 failed, 858 passed in 103.46s (0:01:43) | `test_reports.py::test_lines_and_addresses_of_users_outside_the_node_are_dropped` |
| P09 | часть к закрытому интервалу принимается | КРАСНЫЙ: 3 failed, 846 passed in 118.09s (0:01:58) | `test_reports.py::test_an_interval_closes_on_the_next_interval_the_deadline_and_its_age`, `test_reports.py::test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed`, `test_reports.py::test_parts_complete_an_interval_and_a_part_past_it_is_refused` |
| P10 | объявленное число частей может уменьшаться | КРАСНЫЙ: 2 failed, 847 passed in 118.70s (0:01:58) | `test_reports.py::test_the_declared_number_of_parts_only_grows_and_is_applied_before_the_range`, `test_reports.py::test_the_recorded_report_answers_are_what_the_live_service_gives` |
| P11 | диапазон по прежнему объявленному числу (новое — после проверки) | КРАСНЫЙ: 1 failed, 848 passed in 114.08s (0:01:54) | `test_reports.py::test_the_declared_number_of_parts_only_grows_and_is_applied_before_the_range` |
| P12 | продолжение со счётчиками интерфейса принимается | КРАСНЫЙ: 1 failed, 848 passed in 117.71s (0:01:57) | `test_reports.py::test_a_continuation_carries_no_interface_counters_and_no_user_of_another_part` |
| P13 | пользователь во второй части интервала принимается | КРАСНЫЙ: 1 failed, 848 passed in 116.95s (0:01:56) | `test_reports.py::test_a_continuation_carries_no_interface_counters_and_no_user_of_another_part` |
| P14 (перемер) | порог продолжения без суммы прежних частей | КРАСНЫЙ: 3 failed, 856 passed in 103.67s (0:01:43) | `test_reports.py::test_a_held_continuation_does_not_close_the_intervals_before_it`, `test_reports.py::test_tc_e2e_02_growth_beyond_the_channel_is_held_and_charged_up_to_it`, `test_reports.py::test_two_acceptances_of_a_node_take_turns` |
| P15 | неудержанное продолжение не закрывает интервалы до себя | КРАСНЫЙ: 1 failed, 848 passed in 107.71s (0:01:47) | `test_reports.py::test_a_part_that_is_not_held_closes_the_intervals_before_it` |
| P16 | монотонность интервалов снята | КРАСНЫЙ: 2 failed, 847 passed in 119.85s (0:01:59) | `test_reports.py::test_intervals_of_a_node_are_monotonic_across_epochs`, `test_reports.py::test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed` |
| P17 | якорь первого интервала снят | КРАСНЫЙ: 2 failed, 847 passed in 118.13s (0:01:58) | `test_reports.py::test_the_first_interval_is_anchored_to_the_buffer_depth`, `test_reports.py::test_the_first_interval_is_anchored_to_the_identity_and_the_first_assignment` |
| P18 | якорь первого назначения с допуском часов | КРАСНЫЙ: 1 failed, 848 passed in 112.79s (0:01:52) | `test_reports.py::test_the_first_interval_is_anchored_to_the_identity_and_the_first_assignment` |
| P19 | якорь без выдачи identity | КРАСНЫЙ: 1 failed, 848 passed in 113.38s (0:01:53) | `test_reports.py::test_the_first_interval_is_anchored_to_the_identity_and_the_first_assignment` |
| P20 | якорь без глубины буфера | КРАСНЫЙ: 1 failed, 848 passed in 120.19s (0:02:00) | `test_reports.py::test_the_first_interval_is_anchored_to_the_buffer_depth` |
| P21 (перемер) | потолок смен эпохи снят | КРАСНЫЙ: 3 failed, 856 passed in 102.71s (0:01:42) | `test_reports.py::test_an_epoch_change_across_the_window_edge_is_counted`, `test_reports.py::test_an_epoch_hold_over_the_open_cap_is_born_closed`, `test_reports.py::test_epoch_changes_are_capped_per_day` |
| P22 (перемер) | потолок смен эпохи на единицу строже | КРАСНЫЙ: 2 failed, 857 passed in 103.12s (0:01:43) | `test_reports.py::test_an_epoch_change_across_the_window_edge_is_counted`, `test_reports.py::test_epoch_changes_are_capped_per_day` |
| P23 (перемер) | потолок открытых интервалов снят | КРАСНЫЙ: 2 failed, 857 passed in 103.09s (0:01:43) | `test_reports.py::test_an_epoch_hold_over_the_open_cap_is_born_closed`, `test_reports.py::test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed` |
| P24 | удержанная часть закрывает интервалы до себя | КРАСНЫЙ: 4 failed, 845 passed in 115.68s (0:01:55) | `test_reports.py::test_a_held_part_does_not_close_the_interval_before_it`, `test_reports.py::test_a_part_that_is_not_held_closes_the_intervals_before_it`, `test_reports.py::test_epoch_changes_are_capped_per_day`, `test_reports.py::test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed` |
| P25 | интервал из одной части не завершается | КРАСНЫЙ: 1 failed, 848 passed in 115.61s (0:01:55) | `test_reports.py::test_tc_e2e_01_a_repeated_report_changes_nothing_and_says_duplicate` |
| P26 | счётчик принятых частей не растёт | КРАСНЫЙ: 3 failed, 846 passed in 115.96s (0:01:55) | `test_reports.py::test_parts_complete_an_interval_and_a_part_past_it_is_refused`, `test_reports.py::test_tc_e2e_02_growth_beyond_the_channel_is_held_and_charged_up_to_it`, `test_reports.py::test_the_declared_number_of_parts_only_grows_and_is_applied_before_the_range` |
| P27 (перемер) | identity не перепроверяется под блокировкой | КРАСНЫЙ: 1 failed, 858 passed in 103.09s (0:01:43) | `test_reports.py::test_a_revocation_during_the_request_stops_the_report` |
| P28 (перемер) | вентиль не перепроверяется под блокировкой | КРАСНЫЙ: 1 failed, 858 passed in 107.40s (0:01:47) | `test_reports.py::test_a_revocation_during_the_request_stops_the_report` |
| P29 (перемер) | строка ноды не блокируется | КРАСНЫЙ: 2 failed, 857 passed in 114.71s (0:01:54) | `test_reports.py::test_a_revocation_during_the_request_stops_the_report`, `test_reports.py::test_two_acceptances_of_a_node_take_turns` |
| P30 | отказ двигает точку продолжения | КРАСНЫЙ: 4 failed, 845 passed in 117.76s (0:01:57) | `test_reports.py::test_intervals_of_a_node_are_monotonic_across_epochs`, `test_reports.py::test_parts_complete_an_interval_and_a_part_past_it_is_refused`, `test_reports.py::test_tc_e2e_03_a_period_ahead_of_the_server_clock_is_refused`, `test_reports.py::test_the_recorded_report_answers_are_what_the_live_service_gives` |
| P31 | точка продолжения — номер части, а не наибольший | КРАСНЫЙ: 1 failed, 848 passed in 117.66s (0:01:57) | `test_reports.py::test_a_held_part_does_not_close_the_interval_before_it` |
| P32 | точка продолжения учитывает отказанные номера | КРАСНЫЙ: 1 failed, 848 passed in 116.95s (0:01:56) | `test_reports.py::test_intervals_of_a_node_are_monotonic_across_epochs` |
| P33 (перемер) | алерт об отброшенных строках снят | КРАСНЫЙ: 3 failed, 856 passed in 108.12s (0:01:48) | `test_jobs.py::test_scheduler_leadership_lock`, `test_reports.py::test_a_continuation_carries_no_interface_counters_and_no_user_of_another_part`, `test_reports.py::test_lines_and_addresses_of_users_outside_the_node_are_dropped` |
| P34 (перемер) | алерт удержания снят | КРАСНЫЙ: 4 failed, 855 passed in 102.53s (0:01:42) | `test_reports.py::test_an_epoch_hold_over_the_open_cap_is_born_closed`, `test_reports.py::test_epoch_changes_are_capped_per_day`, `test_reports.py::test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed`, `test_reports.py::test_tc_e2e_02_growth_beyond_the_channel_is_held_and_charged_up_to_it` |
| P35 | алерт отказа по времени снят | КРАСНЫЙ: 1 failed, 848 passed in 118.89s (0:01:58) | `test_reports.py::test_tc_e2e_03_a_period_ahead_of_the_server_clock_is_refused` |
| P36 (перемер) | порог вдвое выше (деление на 4) | КРАСНЫЙ: 12 failed, 847 passed in 102.00s (0:01:41) | `test_reports.py::test_a_held_single_part_interval_takes_no_place_among_the_open`, `test_reports.py::test_tc_e2e_02_growth_beyond_the_channel_is_held_and_charged_up_to_it`, `test_reports.py::test_the_threshold_follows_the_length_of_the_interval`, `test_reports.py::test_two_acceptances_of_a_node_take_turns`, `test_acceptance.py::test_tc_unit_01_the_threshold_is_bandwidth_times_duration`, `test_acceptance.py::test_the_bandwidth_bound_keeps_a_node_year_inside_bigint`, `test_acceptance.py::test_the_threshold_counts_microseconds_and_floors[1-duration0-7500000]`, `test_acceptance.py::test_the_threshold_counts_microseconds_and_floors[1-duration4-0]`, `test_acceptance.py::test_the_threshold_counts_microseconds_and_floors[100-duration2-45000000000]`, `test_acceptance.py::test_the_threshold_counts_microseconds_and_floors[100-duration5-750000012]`, `test_acceptance.py::test_the_threshold_counts_microseconds_and_floors[1000-duration1-7500000000]`, `test_acceptance.py::test_the_threshold_counts_microseconds_and_floors[8-duration3-1]` |
| P37 (перемер) | доля downlink без порога | КРАСНЫЙ: 2 failed, 857 passed in 109.65s (0:01:49) | `test_reports.py::test_tc_e2e_02_growth_beyond_the_channel_is_held_and_charged_up_to_it`, `test_acceptance.py::test_a_part_over_the_threshold_is_held_and_charged_up_to_it` |
| P38 (перемер) | корзина метрики не включает свою границу | КРАСНЫЙ: 1 failed, 858 passed in 102.42s (0:01:42) | `test_reports.py::test_interval_durations_are_a_metric_per_node` |
| P39 (перемер) | окно метрики пусто | КРАСНЫЙ: 1 failed, 858 passed in 102.01s (0:01:42) | `test_reports.py::test_interval_durations_are_a_metric_per_node` |
| P40 | отказ части — 422 вместо 409 | КРАСНЫЙ: 12 failed, 837 passed in 117.00s (0:01:57) | `test_reports.py::test_a_new_interval_older_than_the_acceptance_window_is_refused`, `test_reports.py::test_an_interval_closes_on_the_next_interval_the_deadline_and_its_age`, `test_reports.py::test_intervals_of_a_node_are_monotonic_across_epochs`, `test_reports.py::test_lines_and_addresses_of_users_outside_the_node_are_dropped`, `test_reports.py::test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed`, `test_reports.py::test_parts_complete_an_interval_and_a_part_past_it_is_refused`, `test_reports.py::test_tc_e2e_03_a_period_ahead_of_the_server_clock_is_refused`, `test_reports.py::test_the_continuation_point_and_repeated_refusals`, `test_reports.py::test_the_declared_number_of_parts_only_grows_and_is_applied_before_the_range`, `test_reports.py::test_the_first_interval_is_anchored_to_the_buffer_depth`, `test_reports.py::test_the_first_interval_is_anchored_to_the_identity_and_the_first_assignment`, `test_reports.py::test_the_recorded_report_answers_are_what_the_live_service_gives` |
| P41 | отказ части не записывается | КРАСНЫЙ: 4 failed, 845 passed in 118.25s (0:01:58) | `test_reports.py::test_parts_complete_an_interval_and_a_part_past_it_is_refused`, `test_reports.py::test_tc_e2e_03_a_period_ahead_of_the_server_clock_is_refused`, `test_reports.py::test_the_continuation_point_and_repeated_refusals`, `test_reports.py::test_the_declared_number_of_parts_only_grows_and_is_applied_before_the_range` |
| P42 | маршрут не передаёт отпечаток запроса | КРАСНЫЙ: 25 failed, 824 passed in 114.96s (0:01:54) | `test_reports.py::test_a_clock_stepped_back_between_parts_keeps_the_interval`, `test_reports.py::test_a_continuation_carries_no_interface_counters_and_no_user_of_another_part`, `test_reports.py::test_a_held_part_does_not_close_the_interval_before_it`, `test_reports.py::test_a_new_interval_older_than_the_acceptance_window_is_refused`, `test_reports.py::test_a_part_that_is_not_held_closes_the_intervals_before_it`, `test_reports.py::test_a_revocation_during_the_request_stops_the_report`, `test_reports.py::test_an_epoch_change_across_the_window_edge_is_counted`, `test_reports.py::test_an_interval_closes_on_the_next_interval_the_deadline_and_its_age`, `test_reports.py::test_epoch_changes_are_capped_per_day`, `test_reports.py::test_every_approved_status_has_its_reports_accepted`, `test_reports.py::test_interval_durations_are_a_metric_per_node`, `test_reports.py::test_intervals_of_a_node_are_monotonic_across_epochs`, `test_reports.py::test_lines_and_addresses_of_users_outside_the_node_are_dropped`, `test_reports.py::test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed`, `test_reports.py::test_parts_complete_an_interval_and_a_part_past_it_is_refused`, `test_reports.py::test_routes_hand_the_parsed_bodies_to_the_domain_and_return_its_answer`, `test_reports.py::test_tc_e2e_01_a_repeated_report_changes_nothing_and_says_duplicate`, `test_reports.py::test_tc_e2e_02_growth_beyond_the_channel_is_held_and_charged_up_to_it`, `test_reports.py::test_tc_e2e_03_a_period_ahead_of_the_server_clock_is_refused`, `test_reports.py::test_the_anchor_binds_only_the_first_interval_of_a_node`, `test_reports.py::test_the_continuation_point_and_repeated_refusals`, `test_reports.py::test_the_declared_number_of_parts_only_grows_and_is_applied_before_the_range`, `test_reports.py::test_the_first_interval_is_anchored_to_the_buffer_depth`, `test_reports.py::test_the_first_interval_is_anchored_to_the_identity_and_the_first_assignment`, `test_reports.py::test_the_recorded_report_answers_are_what_the_live_service_gives` |
| P43 (перемер) | запись ноды без верхней границы полосы | КРАСНЫЙ: 1 failed, 858 passed in 101.54s (0:01:41) | `test_acceptance.py::test_the_node_record_bounds_the_bandwidth` |
| P44 | фикстура записала не ту причину отказа | КРАСНЫЙ: 1 failed, 848 passed in 116.32s (0:01:56) | `test_reports.py::test_the_recorded_report_answers_are_what_the_live_service_gives` |
| M01 (перемер) | номер отчёта с нуля в схеме | КРАСНЫЙ: 2 failed, 857 passed in 101.69s (0:01:41) | `test_schema_accounting.py::test_report_constraints_of_140`, `test_schema_accounting.py::test_catalog_matches_data_model` |
| M02 (перемер) | интервалы ноды могут перекрываться в схеме | КРАСНЫЙ: 2 failed, 857 passed in 106.88s (0:01:46) | `test_schema_accounting.py::test_report_constraints_of_140`, `test_schema_accounting.py::test_catalog_matches_data_model` |
| M03 (перемер) | пользователь в двух частях интервала в схеме | КРАСНЫЙ: 2 failed, 857 passed in 107.18s (0:01:47) | `test_reports.py::test_a_continuation_carries_no_interface_counters_and_no_user_of_another_part`, `test_schema_accounting.py::test_catalog_matches_data_model` |
| M04 (перемер) | граница полосы снята в схеме | КРАСНЫЙ: 2 failed, 857 passed in 102.45s (0:01:42) | `test_schema_accounting.py::test_report_constraints_of_140`, `test_schema_nodes.py::test_catalog_matches_data_model` |
| M05 (перемер) | принятых частей больше объявленных в схеме | КРАСНЫЙ: 3 failed, 856 passed in 107.84s (0:01:47) | `test_jobs.py::test_scheduler_leadership_lock`, `test_schema_accounting.py::test_report_constraints_of_140`, `test_schema_accounting.py::test_catalog_matches_data_model` |
| R01 (перемер) | окно приёма нового интервала снято (too_old) | КРАСНЫЙ: 2 failed, 857 passed in 101.94s (0:01:41) | `test_reports.py::test_a_new_interval_older_than_the_acceptance_window_is_refused`, `test_reports.py::test_moments_at_the_edge_of_the_range_get_a_contract_answer` |
| R02 (перемер) | часть сверх потолка открытых списывается до порога | КРАСНЫЙ: 2 failed, 857 passed in 102.44s (0:01:42) | `test_reports.py::test_an_epoch_hold_over_the_open_cap_is_born_closed`, `test_reports.py::test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed` |
| R03 (перемер) | интервал части сверх потолка рождается открытым | КРАСНЫЙ: 2 failed, 857 passed in 103.00s (0:01:42) | `test_reports.py::test_an_epoch_hold_over_the_open_cap_is_born_closed`, `test_reports.py::test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed` |
| R04 (перемер) | смены эпохи не считаются | КРАСНЫЙ: 3 failed, 856 passed in 103.12s (0:01:43) | `test_reports.py::test_an_epoch_change_across_the_window_edge_is_counted`, `test_reports.py::test_an_epoch_hold_over_the_open_cap_is_born_closed`, `test_reports.py::test_epoch_changes_are_capped_per_day` |
| R05 (перемер) | окно смен эпохи шире суток (25 ч) | КРАСНЫЙ: 2 failed, 857 passed in 103.84s (0:01:43) | `test_reports.py::test_epoch_changes_are_capped_per_day`, `test_acceptance.py::test_the_windows_of_acceptance_are_literal` |
| R06 (перемер) | окно смен эпохи уже суток (23 ч) | КРАСНЫЙ: 4 failed, 855 passed in 102.15s (0:01:42) | `test_reports.py::test_an_epoch_change_across_the_window_edge_is_counted`, `test_reports.py::test_an_epoch_hold_over_the_open_cap_is_born_closed`, `test_reports.py::test_epoch_changes_are_capped_per_day`, `test_acceptance.py::test_the_windows_of_acceptance_are_literal` |
| R07 | повтор сверяет только границы | КРАСНЫЙ: 1 failed, 848 passed in 101.73s (0:01:41) | `test_reports.py::test_the_continuation_point_and_repeated_refusals` |
| R08 | момент последнего приёма идёт назад вместе с часами | КРАСНЫЙ: 1 failed, 848 passed in 103.16s (0:01:43) | `test_reports.py::test_a_clock_stepped_back_between_parts_keeps_the_interval` |
| R09 (перемер) | состав — после проверки времени (алерт у отказа future теряется) | КРАСНЫЙ: 1 failed, 858 passed in 103.52s (0:01:43) | `test_reports.py::test_lines_and_addresses_of_users_outside_the_node_are_dropped` |
| R10 | алерт о чужих пользователях при 422 снят | КРАСНЫЙ: 1 failed, 848 passed in 101.73s (0:01:41) | `test_reports.py::test_a_continuation_carries_no_interface_counters_and_no_user_of_another_part` |
| R11 (перемер) | потолок открытых интервалов на единицу строже | КРАСНЫЙ: 2 failed, 857 passed in 101.19s (0:01:41) | `test_reports.py::test_a_held_single_part_interval_takes_no_place_among_the_open`, `test_reports.py::test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed` |
| R13 | счётчик tx в продолжении допускается | КРАСНЫЙ: 1 failed, 848 passed in 101.90s (0:01:41) | `test_reports.py::test_a_continuation_carries_no_interface_counters_and_no_user_of_another_part` |
| R14 | допуск часов в полтора раза шире (7,5 с) | КРАСНЫЙ: 1 failed, 848 passed in 101.33s (0:01:41) | `test_reports.py::test_tc_e2e_03_a_period_ahead_of_the_server_clock_is_refused` |
| R15 | якорь по глубине буфера без допуска | КРАСНЫЙ: 1 failed, 848 passed in 102.07s (0:01:42) | `test_reports.py::test_the_first_interval_is_anchored_to_the_buffer_depth` |
| R16 (перемер) | перепроверка — любая живая identity ноды, а не предъявленная | КРАСНЫЙ: 2 failed, 857 passed in 106.05s (0:01:46) | `test_reports.py::test_a_revocation_during_the_request_stops_the_report`, `test_reports.py::test_the_anchor_binds_only_the_first_interval_of_a_node` |
| R17 (перемер) | перепроверка без срока identity | КРАСНЫЙ: 1 failed, 858 passed in 103.98s (0:01:43) | `test_reports.py::test_a_revocation_during_the_request_stops_the_report` |
| R18 (перемер) | перепроверка без вывода ноды | КРАСНЫЙ: 1 failed, 858 passed in 107.28s (0:01:47) | `test_reports.py::test_a_revocation_during_the_request_stops_the_report` |
| R19 | отказ части с Retry-After | КРАСНЫЙ: 4 failed, 845 passed in 107.17s (0:01:47) | `test_jobs.py::test_scheduler_leadership_lock`, `test_reports.py::test_parts_complete_an_interval_and_a_part_past_it_is_refused`, `test_reports.py::test_tc_e2e_03_a_period_ahead_of_the_server_clock_is_refused`, `test_reports.py::test_the_recorded_report_answers_are_what_the_live_service_gives` |
| R20 | новая эпоха не закрывает интервал прежней | КРАСНЫЙ: 1 failed, 848 passed in 106.99s (0:01:46) | `test_reports.py::test_intervals_of_a_node_are_monotonic_across_epochs` |
| R21 | неудержанное продолжение снимает признак удержания | КРАСНЫЙ: 1 failed, 848 passed in 102.06s (0:01:42) | `test_reports.py::test_a_part_that_is_not_held_closes_the_intervals_before_it` |
| R22 (перемер) | окно метрики — два часа | КРАСНЫЙ: 2 failed, 857 passed in 102.57s (0:01:42) | `test_reports.py::test_interval_durations_are_a_metric_per_node`, `test_acceptance.py::test_the_windows_of_acceptance_are_literal` |
| R23 | окно приёма — 96 ч | КРАСНЫЙ: 2 failed, 847 passed in 102.88s (0:01:42) | `test_reports.py::test_a_new_interval_older_than_the_acceptance_window_is_refused`, `test_acceptance.py::test_the_windows_of_acceptance_are_literal` |
| R24 (перемер) | интервал capped в схеме не допускается | КРАСНЫЙ: 4 failed, 855 passed in 104.83s (0:01:44) | `test_reports.py::test_an_epoch_hold_over_the_open_cap_is_born_closed`, `test_reports.py::test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed`, `test_schema_accounting.py::test_report_constraints_of_140`, `test_schema_accounting.py::test_catalog_matches_data_model` |
| R25 (перемер) | порог вдвое — как по каждому направлению | КРАСНЫЙ: 10 failed, 849 passed in 102.42s (0:01:42) | `test_reports.py::test_a_held_continuation_does_not_close_the_intervals_before_it`, `test_reports.py::test_a_held_part_does_not_close_the_interval_before_it`, `test_reports.py::test_a_held_single_part_interval_takes_no_place_among_the_open`, `test_reports.py::test_a_part_that_is_not_held_closes_the_intervals_before_it`, `test_reports.py::test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed`, `test_reports.py::test_tc_e2e_02_growth_beyond_the_channel_is_held_and_charged_up_to_it`, `test_reports.py::test_the_threshold_follows_the_length_of_the_interval`, `test_reports.py::test_two_acceptances_of_a_node_take_turns`, `test_acceptance.py::test_a_part_over_the_threshold_is_held_and_charged_up_to_it`, `test_acceptance.py::test_a_sum_already_over_the_threshold_charges_nothing_more` |
| R26 (перемер) | остаток удержанной части — сначала uplink, а не пропорционально | КРАСНЫЙ: 2 failed, 857 passed in 103.75s (0:01:43) | `test_reports.py::test_tc_e2e_02_growth_beyond_the_channel_is_held_and_charged_up_to_it`, `test_acceptance.py::test_a_part_over_the_threshold_is_held_and_charged_up_to_it` |
| R27 | якорь — всякий раз, когда открытых интервалов нет (прежнее правило) | КРАСНЫЙ: 2 failed, 847 passed in 108.69s (0:01:48) | `test_reports.py::test_a_new_interval_older_than_the_acceptance_window_is_refused`, `test_reports.py::test_the_anchor_binds_only_the_first_interval_of_a_node` |
| R28 (перемер) | смена эпохи на краю окна не считается | КРАСНЫЙ: 1 failed, 858 passed in 104.53s (0:01:44) | `test_reports.py::test_an_epoch_change_across_the_window_edge_is_counted` |
| R29 (перемер) | член состава — только в состоянии active | КРАСНЫЙ: 1 failed, 858 passed in 110.88s (0:01:50) | `test_reports.py::test_lines_and_addresses_of_users_outside_the_node_are_dropped` |
| R30 (перемер) | удержание по потолку эпох не списывается | КРАСНЫЙ: 2 failed, 857 passed in 109.43s (0:01:49) | `test_jobs.py::test_scheduler_leadership_lock`, `test_reports.py::test_epoch_changes_are_capped_per_day` |
| R31 (перемер) | удержанная по эпохам часть закрывает интервалы до себя | КРАСНЫЙ: 1 failed, 858 passed in 105.45s (0:01:45) | `test_reports.py::test_epoch_changes_are_capped_per_day` |
| R32 (перемер) | истёкший интервал считается открытым | КРАСНЫЙ: 1 failed, 858 passed in 106.40s (0:01:46) | `test_reports.py::test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed` |
| R33 (перемер) | повтор удержанного ключа — не дубликат | КРАСНЫЙ: 1 failed, 858 passed in 105.50s (0:01:45) | `test_reports.py::test_tc_e2e_02_growth_beyond_the_channel_is_held_and_charged_up_to_it` |
| R34 | повтор отказа interval_closed отвечает rejected_time | КРАСНЫЙ: 1 failed, 848 passed in 101.22s (0:01:41) | `test_reports.py::test_parts_complete_an_interval_and_a_part_past_it_is_refused` |
| R35 (перемер) | окно метрики вдвое уже | КРАСНЫЙ: 1 failed, 858 passed in 105.10s (0:01:45) | `test_reports.py::test_interval_durations_are_a_metric_per_node` |
| R36 (перемер) | окно приёма вшестеро шире в самой проверке | КРАСНЫЙ: 1 failed, 858 passed in 105.37s (0:01:45) | `test_reports.py::test_a_new_interval_older_than_the_acceptance_window_is_refused` |
| R37 (перемер) | допуск окна приёма в полтора раза шире | КРАСНЫЙ: 1 failed, 858 passed in 110.05s (0:01:50) | `test_reports.py::test_a_new_interval_older_than_the_acceptance_window_is_refused` |
| R38 | допуск якоря по identity вдвое уже | КРАСНЫЙ: 1 failed, 848 passed in 100.66s (0:01:40) | `test_reports.py::test_the_first_interval_is_anchored_to_the_identity_and_the_first_assignment` |
| R39 | допуск якоря по глубине буфера в полтора раза шире | КРАСНЫЙ: 1 failed, 848 passed in 101.55s (0:01:41) | `test_reports.py::test_the_first_interval_is_anchored_to_the_buffer_depth` |
| R40 | допуск часов «в будущее» вдвое уже | КРАСНЫЙ: 1 failed, 848 passed in 101.43s (0:01:41) | `test_reports.py::test_tc_e2e_03_a_period_ahead_of_the_server_clock_is_refused` |
| R41 | сбой ряда интервалов роняет /metrics в «база недоступна» | КРАСНЫЙ: 1 failed, 848 passed in 101.77s (0:01:41) | `test_reports.py::test_a_failing_interval_series_does_not_mark_the_database_down` |
| T01 | порог по константе 60 с, а не по длине интервала | КРАСНЫЙ: 1 failed, 858 passed in 102.07s (0:01:42) | `test_reports.py::test_the_threshold_follows_the_length_of_the_interval` |
| T02 | состав — член любой ноды | КРАСНЫЙ: 1 failed, 858 passed in 103.78s (0:01:43) | `test_reports.py::test_lines_and_addresses_of_users_outside_the_node_are_dropped` |
| T03 | состав — пользователь существует | КРАСНЫЙ: 1 failed, 858 passed in 102.25s (0:01:42) | `test_reports.py::test_lines_and_addresses_of_users_outside_the_node_are_dropped` |
| T04 | удержанный однокусочный интервал остаётся открытым | КРАСНЫЙ: 2 failed, 857 passed in 108.80s (0:01:48) | `test_reports.py::test_a_held_single_part_interval_takes_no_place_among_the_open`, `test_reports.py::test_tc_e2e_02_growth_beyond_the_channel_is_held_and_charged_up_to_it` |
| T05 | окно приёма по period_end вместо period_start | КРАСНЫЙ: 2 failed, 857 passed in 103.02s (0:01:43) | `test_reports.py::test_a_new_interval_older_than_the_acceptance_window_is_refused`, `test_reports.py::test_epoch_changes_are_read_by_the_index_from_the_predecessor_on` |
| T06 | якорь — последнее назначение (max), а не первое | КРАСНЫЙ: 1 failed, 858 passed in 103.47s (0:01:43) | `test_reports.py::test_the_anchor_takes_the_first_assignment_and_none_does_not_stop_it` |
| T07 | блокировка ноды ослаблена до FOR KEY SHARE | КРАСНЫЙ: 2 failed, 857 passed in 114.05s (0:01:54) | `test_reports.py::test_a_revocation_during_the_request_stops_the_report`, `test_reports.py::test_two_acceptances_of_a_node_take_turns` |
| T08 | блокировка ноды ослаблена до FOR SHARE | КРАСНЫЙ: 1 failed, 858 passed in 102.87s (0:01:42) | `test_reports.py::test_two_acceptances_of_a_node_take_turns` |
| T09 | нода без назначений отказана якорем | КРАСНЫЙ: 1 failed, 858 passed in 102.97s (0:01:42) | `test_reports.py::test_the_anchor_takes_the_first_assignment_and_none_does_not_stop_it` |
| T10 | метрика без удержанных интервалов | КРАСНЫЙ: 1 failed, 858 passed in 106.80s (0:01:46) | `test_reports.py::test_interval_durations_are_a_metric_per_node` |
| T11 | окно приёма и к продолжениям открытого интервала | КРАСНЫЙ: 1 failed, 858 passed in 108.21s (0:01:48) | `test_reports.py::test_a_continuation_is_not_bound_by_the_acceptance_window` |
| T12 | удержанное продолжение закрывает интервалы до себя | КРАСНЫЙ: 1 failed, 858 passed in 106.98s (0:01:46) | `test_reports.py::test_a_held_continuation_does_not_close_the_intervals_before_it` |
| T13 | верхняя граница полосы снята в NodePatch | КРАСНЫЙ: 1 failed, 858 passed in 108.07s (0:01:48) | `test_acceptance.py::test_the_node_record_bounds_the_bandwidth` |
| T14 | часть сверх потолка открытых закрывает интервалы до себя | КРАСНЫЙ: 1 failed, 858 passed in 103.33s (0:01:43) | `test_reports.py::test_open_intervals_are_capped_and_a_part_over_the_cap_is_born_closed` |
| T15 | потолок открытых — только к удержанию порогом | КРАСНЫЙ: 1 failed, 858 passed in 102.80s (0:01:42) | `test_reports.py::test_an_epoch_hold_over_the_open_cap_is_born_closed` |
| T16 | метка не приводится к UTC моделью | КРАСНЫЙ: 2 failed, 857 passed in 104.18s (0:01:44) | `test_reports.py::test_moments_at_the_edge_of_the_range_get_a_contract_answer`, `test_report_schema.py::test_a_moment_outside_utc_is_refused_by_the_model` |
| T17 | счёт смен эпохи без границы по индексу | КРАСНЫЙ: 1 failed, 858 passed in 103.47s (0:01:43) | `test_reports.py::test_epoch_changes_are_read_by_the_index_from_the_predecessor_on` |
| T18 | CHECK capped в схеме снят | КРАСНЫЙ: 2 failed, 857 passed in 102.92s (0:01:42) | `test_schema_accounting.py::test_report_constraints_of_140`, `test_schema_accounting.py::test_catalog_matches_data_model` |

## Критерии приёмки

- [x] AC-06 выполнен — TC-E2E-01: повтор ключа — 200 `duplicate: true`, строка отчёта одна,
  состояние интервала не сдвинулось (P01, P25); утверждение «записей баланса по ключу нет»
  содержательно только с 001.77 — приём 001.34 баланс не пишет вовсе.
- [x] Отчёт с аномалией удерживается до разбора — TC-E2E-02: `held_anomaly`, интервал с `held`,
  доля в пределах порога на сумму направлений, алерт (P14, P34, P36, P37, R25, R26).
- [x] Ответ содержит `last_accepted_seq` — все тесты приёма; точка продолжения — наибольший
  принятый номер, отказ её не сдвигает (P30, P31, P32).

## Отклонения от описания задачи

- **Сигнатура** `accept_report(node, report)` → `accept_report(node, report, fingerprint)`:
  перепроверка identity под блокировкой нужна по отпечатку предъявленного сертификата (проверка
  «есть живая identity у ноды» пропустила бы отчёт старого сертификата после повторного обмена).
  Страж сигнатуры (`test_stubs.py`) обновлён.
- **Цель задачи** перечисляет «запись фактов, накопление часа, `balance_entries`», а «Описание
  изменений» и план — шаги 1–3 §5.9; факты и баланс — 001.77 (её файл задачи). Сделано по
  описанию изменений.
- **`tests/e2e/test_reports.py`** в задаче — «новый файл»; он существовал с 001.33 и дополнен.
- **«Баланс без изменений».** Приём 001.34 баланс не меняет вовсе. В TC-E2E-01 утверждение —
  нет записей `balance_entries` с ключом ноды; смысл оно получит с 001.77. В TC-E2E-02 «баланс
  без изменений» описания задачи не утверждается: удержанная часть списывается в пределах порога
  («удержание не освобождает от списания») — утверждается доля в суммах интервала.
- **Порог — на сумму направлений**, а не на каждое (примечание задачи по раунду 8 001.33):
  решение заказчика 2026-10-20, раздел «Решения».
- **Якорь — только для первого интервала ноды**, а не «и первого после закрытия всех»
  (примечание задачи): решение заказчика 2026-10-20 после роаста раунда 2, раздел «Решения».
- **Часть сверх потолка открытых интервалов** открывает интервал закрытым и не списывается — в
  примечании задачи «интервала не открывает» и «списания не отменяет»: решение заказчика
  2026-10-20 после роаста раунда 1.
- **Оценка**: в задаче 4 ч, в плане 8 ч — расхождение документов, не исправлялось.
- **Сверх модели:** `CHECK` таблицы `report_intervals` (порядок границ, первая часть с единицы,
  принятых 1…объявленных, закрытие и причина вместе, `last_received_at ≥ first_received_at`) и
  `traffic_reports.parts_total` 1…1000 — те же пределы, что у модели и правил.
- **Контракт:** четыре новые фикстуры (`report-duplicate`, `report-rejected-time`,
  `report-interval-closed`, `report-inconsistent`); у фикстур отчёта в `given` — `server_time` и
  `accepted`. Герметичный прогон отвечает записанным (`RecordedAccounting`), а
  `test_the_recorded_report_answers_are_what_the_live_service_gives` держит каждую фикстуру
  отчёта ответом живого приёма при записанном предусловии.
- **Алерты** — записи журнала (соглашение кода: ERROR — событие для алерта, правила — 001.68), не
  ряды метрик.

## Решения заказчика

- **2026-10-20, до роаста:** часть, удержанная по потолку открытых интервалов, не списывается до
  разбора оператором (вариант «а» вопроса, вынесенного в первом отчёте).
- **2026-10-20, после роаста раунда 1 (находка C-2 линзы логики):** порог §5.9 — на сумму
  `uplink + downlink`, а не на каждое направление.
- **2026-10-20, после роаста раунда 2 (находка L-1 линзы логики):** якорь — только для первого
  интервала ноды; дальше — монотонность и окно приёма 72 ч.
- **2026-10-20, после роаста раунда 3 (предел раундов исчерпан):** правки находок без четвёртого
  роаста, затем приёмка.

## Передано соседним задачам

- **001.77**: продолжение `_Acceptance` и `Outcome`; факты удержанных частей и правило деления
  доли между строками; часть сверх потолка открытых — `billable = 0`; статус ноды под
  блокировкой для `provisioning`; счётчики интерфейса — у первой принятой части; страховочный
  индекс `traffic_lines_interval_user_key`; нода без назначений; граница `bandwidth_mbps` введена
  (поправка к блоку 001.23); без блокирующих чтений `node_billing_assignments` в приёме.
- **001.37**: закрытие молчащих нод тем же `closing()` и под строкой ноды; источники разрывов
  (закрытые без всех частей, `capped` — на весь объём, отказанные номера, смена эпохи, интервал
  без первой части); срок хранения `traffic_reports` и `report_intervals`.
- **001.68**: события журнала приёма и ряд `control_plane_report_intervals`.
- **001.30**: повторные удержания → `suspended`; индекса `(node_id, received_at)` у
  `traffic_reports` нет.
- **001.56**: реакция агента на 409 и 422 приёма; `given.server_time`/`accepted` фикстур.
- **001.73**: замер транзакции приёма (до 12 операторов) против резерва 50 мс.

## Развёртка

`docs/sweeps/001-34.json` — восемнадцать шаблонов правил, которые задача сменила: приём-заглушка
без состояния, невыбранный код отказа части, полоса без верхней границы, «вводит / миграция —
001.34», обязательный `Retry-After` у каждого 409 раздела, число фикстур каталога; с раунда 2 —
порог по каждому направлению, «часть сверх потолка открытых интервала не открывает», «окно
следует из правил», «49 эпох», «тем же телом»; с раунда 3 — якорь «и первого после закрытия
всех», производные прежнего порога («в оба направления», «7,88») и «неразобранные удержания»;
после раунда 3 — «в прошлое дальше буфера», «удержания переводят ноду» настоящим временем, «до
тринадцати операторов», «монотонный номер».
История — подписанные строки, этот раздел и следующие за ним, отчёт 001.23 целиком
(`history_sections`). Совпадения вне истории — объяснены:

- `control-plane/migrations/140_report_acceptance.rollback.sql:10` и
  `control-plane/tests/e2e/test_schema_accounting.py:535` — откат 140 и его страж возвращают
  прежнее `CHECK (bandwidth_mbps > 0)`: так и должно быть;
- `docs/tasks/task-001-77-…:125` — исходная передача 001.23 («без верхней границы»); поправка
  001.34 — в блоке «Найдено при 001.34» того же файла;
- `control-plane/tests/contract/test_agent_v1.py:319` — докстринг стража заголовков: правило
  «403, 409, 426 — `Retry-After`», исключение для 409 отчёта — следующим предложением того же
  докстринга;
- `control-plane/app/accounting/service.py:543` — комментарий к счёту смен эпохи называет
  прежний счёт («а не различные эпохи»), чтобы объяснить нынешний;
- `contracts/agent_v1/README.md:244` и `tests/tests-001/report-001-34.md` (решение о якоре и
  раздел «Отклонения») — цитаты прежних правил («и первого после закрытия всех», «интервала не
  открывает», «списания не отменяет») рядом с решением заказчика, которое их сменило;
- строка R25 таблицы посадок («порог вдвое — как по каждому направлению») — описание поведения,
  которое снимает посадка;
- `docs/tasks/task-001-30-…:149` — правило «повторные удержания переводят ноду в `suspended`»
  передано в 001.30 цитатой: его вводит она;
- `docs/tasks/task-001-34-…` (семь совпадений: строки 98 и 143 «код выбрать здесь», 150 «к
  каждой», 156 «интервала не открывает», 166 «переводят ноду» и примечание на строках 80–81) —
  исходная постановка задачи; сменённые правила перечислены примечанием в начале «Примечаний»
  со ссылкой на решения заказчика, коды отказов выбраны в разделе «Решения».

## Раунд 1 — роаст (отпечаток дерева `d9eab1c8496b`)

Две линзы параллельно, бриф с отпечатком, исполнением (`make check` — 843 passed, 49 посадок) и
версиями runtime. Линза логики (только чтение) и линза стражей (команды только для чтения,
посадки — в копии дерева в каталоге сессии, 24 прогона подмножества) — обе **REJECTED**. После
раунда отпечаток пересчитан — `d9eab1c8496b`, `git status` тот же, строк тестов на стенде нет:
роаст дерево не тронул.

Решение заказчика 2026-10-20 по вопросу, вынесенному до роаста: часть сверх потолка открытых
интервалов **не списывается** до разбора оператором (вариант «а»).

| Находка | Суть | Исход |
| :--- | :--- | :--- |
| Логика F1 / стражи M1 (MAJOR) | часть, удержанная по потолку открытых интервалов, интервала не открывала — следующие части того же интервала шли как новый интервал с нулевыми суммами, мимо `parts_total`, счётчиков, пользователя другой части и монотонности | исправлено: интервал такой части рождается закрытым (`close_reason = 'capped'`, удержан, суммы 0 — решение заказчика), его части — `interval_closed`, перекрытие — `overlap`; значение `capped` в `CHECK` миграции 140; тест переписан (R02, R03, R11, R24) |
| Логика F2 / стражи C5 (MAJOR / CRITICAL) | окно 72 ч не следовало из правил: якорь действует только без открытых интервалов, цепочка недосланных интервалов отставала от часов без предела — до «no partition … found» | исправлено: явная проверка `period_start` нового интервала не старше `REPORT_ACCEPT_WINDOW` (72 ч) с допуском — 409 `rejected_time`, причина `too_old`; утверждения «окно следует из правил», «мёртвый страж» сняты из кода, `.AGENTS.md` и отчёта; тест (R01, R23) |
| Логика F3 (MAJOR) | потолок смен эпохи считал различные эпохи: чередование A→B→A не облагалось | исправлено: смены — соседние по времени интервалы с разными эпохами (`lag` по окну), возврат к прежней эпохе — смена; тест на чередовании (R04) |
| Логика G1 / стражи C2 | потолок открытых интервалов виден только сверху | тест: при семи удержанная часть открывает интервал, при восьми — `capped` (R11) |
| Логика G2 | допуск якоря по глубине буфера не откалиброван | точка «сутки и 3 с» — приём (R15) |
| Логика G3 / стражи C3 | окно смен эпохи нигде не закреплено | интервалы на 23 ч 59 мин (в окне) и 24 ч 1 мин (вне) в тесте, литерал в `test_acceptance.py` (R05, R06) |
| Логика G4 | раннее закрытие по времени не ловилось | продолжение интервала за час до срока принимается (P05, P06) |
| Логика G5 | якорь «первого после закрытия всех» не проверен | тест (R12); правило снято решением заказчика после раунда 2 — якорь только для первого интервала, R12 заменена R27 |
| Логика G6 | счётчик tx продолжения не проверен | тест (R13) |
| Логика G7 | `seq_reused` — только при обеих границах | каждое записанное поле по отдельности (R07) |
| Логика G8 / стражи C1 | допуск «в будущее» не откалиброван (+3 и +10 с) | +6 с — отказ, +4 с — приём (R14, P03, P04) |
| Стражи C4 | перепроверка identity — только отзыв всех и `pending` | отзыв именно предъявленной identity при живой второй, истечение, вывод ноды (R16–R18) |
| Стражи M2 | отсутствие `Retry-After` у 409 — только в файлах фикстур | утверждается на живых ответах TC-E2E-03, `interval_closed` и воспроизведения фикстур (R19) |
| Стражи M3 | закрытие интервала прежней эпохи новой не проверено | тест (R20) |
| Стражи M4 | окно метрики не закреплено | интервал за 61 минуту вне ряда, литерал часа (R22) |
| Стражи M5 | числа развёртки не воспроизводились | развёртка перезапущена после всех правок; прошлые отчёты и разделы истории — `history_sections` (раздел «Развёртка») |
| Логика F4 (MINOR) | обновление продолжения могло переоткрыть интервал, закрытый встречной записью | исправлено: `closed_at is null` в условии и проверка «ровно одна строка»; порядок блокировок — в передаче 001.37. Посадки нет: без встречного писателя, минующего блокировку ноды, путь недостижим |
| Логика F5 / стражи m8 (MINOR) | членство — любая строка `node_user_state`, `removed` тоже; уплотнение удаляет `removed` | оставлено и записано: до уплотнения трафик удалённого пользователя из буфера принимается, после — отбрасывается как чужой (без переплаты, с алертом) |
| Логика F6 (MINOR) | номер нового интервала не обязан превышать принятый | не принято: контракт такого отказа не задаёт, нарушение видит сверка непрерывности (001.37) |
| Логика F7 / стражи n1 | повтор сверял только границы | сверяются все записанные поля (границы, `parts_total`, счётчики интерфейса); строки — 001.77; README уточнён (R07) |
| Логика F8 | алерт о чужих пользователях терялся у отказа и 422 | состав — до первого отказа, алерт — при любом исходе (R09, R10) |
| Логика F9 | шаг часов назад между частями — нарушение `CHECK`, 500 | `greatest(last_received_at, …)`; тест (R08) |
| Логика F10 / стражи m5 | докстринги и `.AGENTS.md` расходились с кодом | исправлены |
| Логика F11 | сбой ряда интервалов показывал `control_plane_db_up 0` | ошибка ряда обрабатывается отдельно, база остаётся «доступна» |
| Логика F12 | блокирующее чтение назначений в приёме дало бы взаимоблокировку с `set_billing_group` | передано 001.77 |
| Логика C-1 | вопрос заказчику занижал переплату | снят решением заказчика (часть сверх потолка не списывается) |
| Логика C-2 | физически порт несёт байт пользователя и в rx, и в tx: предел — `uplink + downlink ≤ полоса × время`, а не каждое направление | вынесено заказчику; решение 2026-10-20 — порог на сумму: `charge()` (остаток — пропорционально части), TC-E2E-02 и модульные случаи литералами, README, модель данных, interfaces, security, миграция 140 (комментарий), граница полосы (P36, P37, R25) |
| Стражи m1, m6, n3–n6 | число тестов в `.AGENTS.md`; «нет записей баланса» только у TC-E2E-01; закрепление констант; причины 422 в OpenAPI; воспроизведение фикстур без `given.node_status` и заголовков; текст метрики | исправлены |
| Стражи m2, m3, m4 | признак удержания мог сниматься; исключение «известная эпоха»; события WARNING и ERROR 422 не проверены | тест признака (R21); исключение заменено счётом смен; утверждения журнала в тестах частей, продолжения и точки продолжения (R10) |
| Стражи m7 | строка `traffic_lines` на `base` в первые десять минут суток UTC — без партиции | стенд создаёт вчерашнюю партицию подключением владельца |

## Раунд 2 — роаст (отпечаток дерева `c8e677319ef8`)

Те же две линзы; бриф — исполнение (`make check` — 847 passed, 75 посадок), решения заказчика и
таблица исходов раунда 1 для проверки. Обе — **REJECTED**; линза логики подтвердила денежное ядро
(переплаты сверх полосы × время и двойного учёта нет, оба решения заказчика реализованы верно),
линза стражей — числа отчёта (802 → 847 по файлам, 75 посадок, развёртка 22 / 16 / 6, 25 фикстур).
После раунда отпечаток пересчитан — `c8e677319ef8`, строк тестов на стенде нет.

Решение заказчика 2026-10-20 по L-1: якорь — только для первого интервала ноды.

| Находка | Суть | Исход |
| :--- | :--- | :--- |
| Логика L-1 (MAJOR) | якорь «первого интервала» срабатывал почти на каждом: однокусочный интервал закрывается сразу — отказ при ротации identity и при сливе буфера после суточной недоступности | решение заказчика: якорь — только для первого интервала ноды; README, interfaces, `_anchor`, `REPORT_BUFFER_DEPTH`; тест «якорь держит только первый интервал» (слив за 25 ч после паузы, интервал до выдачи новой identity) (R27) |
| Логика L-2 (MINOR) | потолок считает открытые интервалы, а не «неразобранные удержания»; нода может намеренно уйти от списания | рационал исправлен (README, отчёт), путь записан в security.md и передан 001.30 (стопор — `suspended`) |
| Логика L-3 / стражи m3, m4 | устаревшие тексты: `.AGENTS.md` (удержанная часть «по потолку открытых — не открывает», причины закрытия без `capped`), «7,88·10¹⁸», «в оба направления» (`nodes.py`, передача 001.77) | исправлены; шаблоны развёртки расширены |
| Логика L-4 / стражи C3 | окно `too_old` проверено только на 20 сутках | 72 ч и 7 с — отказ, 72 ч и 3 с — приём (R36, R37) |
| Логика L-5 | смена эпохи на краю окна не считалась | считается: предшественник — последний интервал до окна; тест на краю (R28) |
| Логика L-6 / стражи n-* | неточности докстрингов (уровень журнала отказов, путь конфликта, утверждение теста якоря), «3 с внутри, 7 и 10 с снаружи», вырожденная проверка «closed» | исправлены |
| Логика L-7 | `user_in_another_part` бездействует до 001.77 | передано 001.77: строки — для каждой принятой, удержанной и `capped` части в транзакции приёма |
| Логика L-8 | нет `lock_timeout`; разбор состава — синхронная работа O(строк) | передано 001.73 |
| Логика L-9 | запасная ветка `/metrics` не проверена | тест: сбой ряда интервалов — `control_plane_db_up 1`, ряда нет (R41) |
| Стражи C1 | допуск якоря по глубине буфера не откалиброван с широкой стороны | сутки и 7 с — отказ, сутки и 3 с — приём (R39) |
| Стражи C2 | допуск якоря по identity не откалиброван с узкой стороны | 7 с — отказ, 3 с — приём (R38) |
| Стражи C4 | окно метрики не откалибровано с узкой стороны | интервал за 59 минут в ряду, за 61 — нет (R35) |
| Стражи M1 | списание удержания по потолку эпох не проверено | удержанная по эпохам часть со строками: суммы интервала — её объём (R30) |
| Стражи M2 | «удержанная часть не закрывает прежний» — только для порога | проверено и для удержания по эпохам (R31) |
| Стражи M3 | счёт открытых без истёкших не проверен | истёкший интервал места среди открытых не занимает (R32) |
| Стражи M4 | членство «в любом состоянии» без стража | член в состоянии `removed` — свой (R29) |
| Стражи m1, m2 | повтор удержанного и отказанного `interval_closed` не проверены | тесты (R33, R34) |
| Стражи m5 | README подавал перевод в `suspended` как правило 001.34/001.35 | «должны переводить — вводят 001.30/001.35» |
| Стражи m6 | убитый прогон оставлял строку `interval_closed`, и откат через 140 краснел навсегда | `clear_test_reports` перед откатом через 140 (`rollback_through`, `--rollback-all`) |
| Стражи m7 | утверждение о балансе в TC-E2E-01 пусто до 001.77 | так и записано в критериях приёмки |
| Стражи m8 | путь `ON CONFLICT DO NOTHING` недостижим и не назван непосаженным | назван в «Не посажены» |
| Стражи m9 | README переоценивал, что покрывает `given.server_time` | уточнено: нода введена и получила identity задолго до него |
| Стражи NIT: +6 с у границы | ~1 с запаса до шага часов VM | точки +7 и +3 с — по 2 с запаса |
| Стражи NIT: `wait_blocked_by` | задача запроса не отменялась при сбое ожидания | отменяется |
| Стражи NIT: `last_seen` адресов | не ограничен часами сервера | вне задачи — 001.38 |

## Раунд 3 — роаст (отпечаток дерева `2dd5b093e4df`)

Те же две линзы; бриф — исполнение (`make check` — 849 passed, 89 посадок на дереве
`4618c81c1ff4`), решения заказчика и таблицы исходов раундов 1–2. Обе — **REJECTED**; денежного
дефекта не нашла ни одна: линза логики прошла пути переплаты сверх полосы × время, двойного
учёта по частям, интервалам и эпохам, обхода якоря и монотонности, гонок ключа, семантики
блокировки в read committed, `closing()`, границ обоих потолков и допусков и подтвердила все три
решения заказчика; линза стражей воспроизвела числа отчёта (849 на дереве задачи и 802 на
`HEAD`, 174 в подмножестве, развёртка 14 / 35 / 25 / 10, 89 строк посадок, 25 фикстур) и
поставила 18 посадок в копии дерева — 14 из них зелёные. После раунда отпечаток пересчитан —
`2dd5b093e4df`, `git status` тот же (44 записи), строк тестов на стенде нет.

Предел раундов (три) исчерпан. Решение заказчика 2026-10-20: правки находок без четвёртого
роаста, затем приёмка. Посадки правок — раздел «Посадки стражей», «После раунда 3».

| Находка | Суть | Исход |
| :--- | :--- | :--- |
| Стражи C1 (CRITICAL) | зависимость порога от длины интервала не показана: все части с байтами — по 60 с, порог по константе 60 с проходил (G01) | тест: при 1 Мбит/с интервал 30 с — 3 750 000 байт приём, 3 750 001 — удержание; час — 450 000 000 и 450 000 001; доля — ровно порог, литералами (T01) |
| Стражи C2 (CRITICAL) | состав проверялся только на несуществующем идентификаторе: «член любой ноды» и «пользователь существует» проходили (G02, G03) | в тесте чужих пользователей — настоящий пользователь другой ноды и пользователь вне составов (T02, T03) |
| Стражи C3 (CRITICAL) | держатель строки ноды в тесте отзыва брал `FOR UPDATE` — ослабление блокировки приёма до `FOR KEY SHARE` и `FOR SHARE` проходило (G08, G08b); взаимное исключение двух приёмов не проверено | держатель — оператор блокировки отзыва и обмена из `nodes.py` (`FOR NO KEY UPDATE`); тест двух приёмов: первый — настоящий `_Acceptance` в открытой транзакции, второй ждёт его, видит его сумму и удерживается, суммы интервала — ровно порог (T07, T08) |
| Логика F-1 (MAJOR) / стражи M1 | «удержанный однокусочный интервал закрывается сразу» без стража (G04): регрессия довела бы восемь удержаний до потолка, и следующие удержанные части не списывались бы | утверждение `completed` в TC-E2E-02; тест: при семи открытых удержанный однокусочный — `completed`, следующая удержанная часть открывает интервал со списанием до порога (T04) |
| Логика F-2 / стражи M2 | «удержанное продолжение не закрывает интервалы до себя» без стража (G13) | тест: прежний интервал открыт, его вторая часть принята (T12) |
| Логика F-3 | «часть сверх потолка не закрывает открытые» без стража: список причин в конце теста потолка тот же | утверждение «восьмой открытый не закрыт» сразу после части сверх потолка (T14) |
| Логика F-4 | потолок открытых к удержанию одним потолком смен эпохи без стража | тест: восемь открытых и сорок девятая смена эпохи в пределах порога — `capped`, суммы 0, причины `epochs,open_intervals` (T15) |
| Стражи M3 | край окна 72 ч проверялся интервалами в 1 с — проверка по `period_end` вместо `period_start` проходила (G05) | отказанная сторона края — интервал 60 с (T05) |
| Логика F-5 (MINOR) | метка у края диапазона (`0001-01-01T00:00:00+00:01`, `9999-12-31T23:59:59-00:01`) проходила модель и падала `OverflowError` при кодировании параметра asyncpg — 500, бесконечный повтор агента | `Timestamp` приводится к UTC моделью (`canonical._utc`), непредставимый момент — 422 `validation_error`; модульный тест и сквозной: края диапазона в UTC доходят до записи — 409 `too_old` и `future` (T16) |
| Логика P-1 | выборка смен эпохи не ограничена по индексу — вся история интервалов ноды под блокировкой на каждой части со сменой эпохи | граница — момент открытия предшественника окна (индекс `(node_id, first_received_at)`); тест плана при запрете последовательного чтения: каждое чтение `report_intervals` — по индексу с условием на `first_received_at` (T17) |
| Стражи m1 | окно приёма для продолжений не проверено (G12) | тест: продолжение открытого интервала, чьё начало старше 72 ч, принимается (T11) |
| Стражи m2 | метрика без удержанных интервалов проходила (G10) | в тесте метрики — удержанный интервал (T10) |
| Стражи m3 | якорь по последнему назначению проходил (G07) | тест: два назначения, первый интервал между ними — приём (T06) |
| Стражи m4 | отказ якоря ноде без назначений проходил (G09) | тест: нода без назначений, первый интервал 23 ч назад — приём (T09) |
| Стражи m5 | верхняя граница полосы в `NodePatch` без стража (G14) | модульный тест: 100 000 — приём, 100 001 — 422 (T13) |
| Логика F-6 / стражи m6 | «не уходит в прошлое дальше буфера» устарело после решения о якоре; «повторные удержания переводят ноду в `suspended`» — настоящим временем без владельца | interfaces.md и security.md: новый интервал не старше 72 ч, время отстаёт не больше окна приёма; «должны переводить — правило и переход вводят 001.30/001.35»; шаблоны развёртки |
| Логика F-7 / стражи m7 | «до 13 операторов» складывало якорь и смену эпохи — они взаимоисключающие | 12 у нового интервала со сменой эпохи, 11 у первого интервала и у продолжения; отчёт и передача 001.73 |
| Логика F-8 / стражи m7 | «847 passed» окончательного дерева устарело | числа «Регрессии» и посадок — окончательного дерева |
| Стражи m7 | «сбоев окружения нет» рядом с посторонним красным под R19 | посторонний красный — RF-1 (`docs/issues/rf-1-scheduler-leadership-lock-flake.md`: гонка теста лидерства планировщика, код посадкой не тронут; вердикт R19 — по трём ожидаемым тестам); повтор записан в RF-1 |
| Логика F-9 / стражи m8 | посадки раунда 3 мерились на `4618c81c1ff4`, отпечаток роаста — `2dd5b093e4df` | между деревьями изменился только этот отчёт: после посадок правился только он, отпечаток `2dd5b093e4df` пересчитан до и после роаста; посадки окончательного дерева — «После раунда 3» в разделе посадок |
| Логика F-10 | докстринги: порядок проверок `_Acceptance` (состав — до проверки времени, порядок — этого модуля, а не README), алерт о чужих «при любом исходе» (повтор ключа и `seq_reused` отвечают до разбора состава), «дубликат по `ON CONFLICT`» (повтор находит поиск ключа под блокировкой) | исправлены в `service.py` и `accounting/.AGENTS.md` |
| Логика F-11 | файл задачи хранил сменённые правила (порог по направлению, «интервала не открывает», «и первого после закрытия всех») | примечание в файле задачи со ссылкой на решения заказчика |
| Стражи m9 | исход F6 слаб: `report_seq` описан «монотонным», а меньший номер у нового интервала принимается | README и описание поля: агент нумерует по возрастанию; Control Plane держит монотонными время интервалов и ключ, порядок номеров между интервалами видит сверка 001.37 |
| Стражи NIT | докстринг `AccountingService` утверждал вызовы шагов 001.77 | «лягут в эту транзакцию в 001.77; в 001.34 ни одна не вызывается» |
| Стражи NIT | ряд `control_plane_report_intervals` — gauge с `le`, а не histogram | оставлено: окно — скользящий час (HELP ряда), а histogram Prometheus накопителен за жизнь процесса; `TYPE gauge` закреплён тестом |
| Стражи NIT | вчерашняя партиция стенда — границы датами в часовом поясе сессии | границы с явным `+00` |
| Стражи NIT | схема не связывала `capped` с удержанием и нулевыми суммами | `CHECK` в 140, модель данных, тест ограничений (T18) |
| Стражи NIT | путь `ON CONFLICT` — мёртвый | уже назван в «Не посажены» |
| Стражи G06 | `user_in_another_part` без `period_end` проходил | недостижимо: `EXCLUDE` не допускает у ноды двух интервалов с одним началом |
