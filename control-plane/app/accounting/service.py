"""Приём отчётов о трафике (постановка §5.9; data-model.md §4.2.5 ``traffic_reports``,
``traffic_lines``, ``traffic_hourly``; interfaces.md §5.2 «Отчёт»; UC-04; R-19, R-21, R-22).

Задача 001.33: схема отчёта и ответ приёма. Задача 001.34 — приём (UC-04 шаги 5, 6, 9; шаги
1–3 §5.9): перепроверка identity и вентиля под блокировкой строки ноды, ключ идемпотентности
(поиск ключа под той же блокировкой; повтор → ``duplicate``, ``ON CONFLICT DO NOTHING`` вставки —
страховка), допуск часов 5 с, правила частей
и интервалов (``report_intervals``: монотонность, якорь первого интервала, завершение,
закрытие следующим интервалом, сроком и возрастом, потолки открытых интервалов и смен эпохи),
состав ноды, порог прироста по ``bandwidth_mbps`` — ``held_anomaly`` с долей в пределах
порога, — ответ ``last_accepted_seq`` и метрика длительности интервалов. Шаги списания 4–7 —
001.77 (коэффициент по ``period_start`` из 001.23, факты ``traffic_lines``, накопление часа,
``balance_entries``, ``user_online_ips``, ``node_interface_hourly``, ``close_hour``,
постановка ``limits.check``). Пороги и отзыв — 001.35, расход гранта — 001.36, адреса и лимит
устройств — 001.38, сверки и разрывы — 001.37, правила алертов — 001.68.

Форма отчёта (001.33):

- границы — ширина колонок §4.2.5 (``bigint``); метки времени — только текст RFC 3339 вида
  ``2026-09-10T12:00:57.123456+00:00``: разделитель ``T``, смещение ``Z`` или ``±hh:mm``, не
  более шести знаков дробной части (самая длинная строка грамматики — ``TIMESTAMP_MAX_CHARS``;
  границы периода формирует нода по своим часам, наивная метка сравнению с допуском §5.9 не
  поддаётся; числа и строки-числа секунд эпохи не принимаются); ``period_end > period_start``
  (``CHECK`` таблиц) и период не дольше ``REPORT_MAX_PERIOD`` (час). Штатный интервал — 60 с,
  и это обязанность агента (§5.9: интервал определяет верхнюю границу перерасхода Н-17 и не
  меняется без её пересмотра); длиннее — только дельта после перерыва снятия счётчиков
  (перезапуск агента при живом Xray): накопительные счётчики не делятся по часам, и отказ терял
  бы данные. Соблюдение обязанности Control Plane наблюдает, а не предполагает: длительность
  каждого принятого интервала — метрика по ноде с алертом на систематическое превышение
  (001.34/001.68). Строка отчёта относится к часу по ``period_start`` (§4.2.5), интервал,
  пересёкший границу часа UTC, целиком ложится в час начала и получает его коэффициент —
  ошибка ограничена длиной интервала (штатно 60 с; нода, снимающая счётчики на границе часа,
  её не допускает вовсе); перерыв дольше часа — новая эпоха счётчиков: это расширяет
  определение §5.9 (эпоха — при перезапуске Xray, базовая точка 0) до «базовой точки
  счётчиков агента», объявлено отклонением в задаче, и смена эпохи без перезапуска Xray —
  разрыв для сверки непрерывности (001.37) между концом последней принятой части прежней эпохи
  и началом первой части новой (признака «перезапуск Xray или перерыв агента» у Control Plane
  нет), не больше ``REPORT_MAX_EPOCHS_PER_DAY`` смен за скользящие сутки — сверх удержание и
  алерт; удержанная часть открывает свой интервал, не закрывая предыдущий, и списывается в
  пределах потолка канала (правила раунда 8 — README, §5.2), а часть сверх потолка открытых
  интервалов — закрытый интервал без списания (``capped``, решение заказчика 2026-10-20).
  Правила частей — README каталога контрактов: интервалы ноды монотонны и не перекрываются во
  времени (и через смену эпохи), ``period_end`` не позже часов сервера с допуском (001.34) —
  объявленное время не обгоняет стенное, и порог §5.9 (полоса × длительность интервала — к
  нарастающей сумме принятых частей на каждой части) ограничивает объём по стенным часам, не
  мешая сливу буфера Н-8; ``parts_total`` — объявленное число частей интервала, оно может
  только расти в пределах интервала (деление отказанной по размеру части добавляет части —
  продолжения несут новое число), действует последнее принятое и применяется до проверки
  диапазона, меньше прежнего — отказ; последняя часть — ``report_seq`` первой + ``parts_total``
  − 1; модель проверяет достаточное условие по номеру этой части (``report_seq`` +
  ``parts_total`` − 1 в ширине ``bigint``), точное по номеру первой — 001.34; интервал
  завершён, когда
  получены все части, и закрыт — завершением, частью следующего интервала ноды (части идут
  подряд, буфер сливается по возрастанию ``report_seq``) или сроком ``REPORT_PARTS_DEADLINE``
  (25 часов: глубина буфера Н-8 плюс час предела интервала — нода после суточной
  недоступности Control Plane досылает части, а не получает отказ); часть к закрытому
  интервалу — отказ ``interval_closed`` без повтора, недостающие части — разрыв (001.34/001.37);
  открытых интервалов у ноды — не больше ``REPORT_MAX_OPEN_INTERVALS``; состояние интервала —
  таблица ``report_intervals`` (§4.2.5); нумерация отчётов с единицы, номер отказанной части
  остаётся пропущенным (агент продолжает с номера за последним отправленным); UUID только в
  канонической записи; адреса — текст без zone id (``inet`` PostgreSQL его не хранит) — формы
  в ``canonical.py``;
- уникальность пользователя в строках и пары «пользователь × адрес» в адресах — первичные
  ключи ``traffic_lines`` и ``user_online_ips`` в пределах ноды (адрес сравнивается вместе с
  семейством: ``inet`` различает ``1.2.3.4`` и ``::1.2.3.4``): о них иначе споткнулась бы
  вставка 001.77, ошибкой базы вместо 422;
- пределы одной части отчёта выведены из норматива Н-4 (Node API: p95 ≤ 200 мс; по решению
  заказчика норматив считается по времени апстрима ``urt`` — прокси буферизует тело целиком до
  передачи, и заливка при полосе ноды в него не входит: она записана в README как обязанность
  таймаута агента) **с учётом одновременности, которую разрешает прокси**: разбор тела
  (``json.loads`` — до разрешения зависимостей, ``model_validate`` — после них, оба синхронно в
  цикле событий одного из двух процессов api; худший случай — все части в одном) — на стенде
  (``python:3.14-slim``, VM, замеры через nginx под потоком heartbeat) ``urt`` p95 50 мс на
  тело потолка из 3 000 элементов при двух потоках отчётов (раунд 5; один поток — 46 мс,
  раунд 6): 16,7 мкс на элемент (на теле из 6 000 элементов — p95 88 мс, 14,7 мкс: доля
  накладных на запрос падает с размером, гейт берёт большее); ``urt`` включает установку
  соединения с апстримом и ответ, а не только разбор, — стоимость завышена в безопасную
  сторону. Одновременно прокси пропускает не больше двух частей на весь парк (``limit_conn
  agent_report_total 2``): 3 000 элементов × 16,7 мкс × 2 = 100 мс — модель проверена замером
  двух одновременных частей (p95 второй из пары 69 мс ≤ 100) — плюс операции остальных видов,
  которые прокси пропускает за это окно по обеим зонам (три от парка при 15 в секунду и две от
  enrollment при 2 в секунду) по 4 и 12 мс (у парка — самое дорогое 64-КиБ тело из минимально
  коротких лишних ключей после проверки формы по байтам, p95 стенда; у enrollment — самый
  дорогой путь обмена, приём с подписью листа: наибольшее из консервативной оценки — стенка
  запроса в процессе api плюс тихий ``urt`` контроля — и p95 ``urt``, приведённого контролем своей
  серии, 001.25) — 136 мс ≤ 200 − 50 (резерв на транзакцию приёма 001.34/001.77 — оценка, 001.73
  меряет); худшее тело отчёта в пределах формы (18 000 ошибок pydantic) — 37 мс, дешевле приёма.
  Гейт —
  ``tests/unit/test_proxy_contract.py`` — считает от константы стоимости элемента, а не от
  пределов (частное от них сокращалось бы), и держит долю цикла,
  разрешённую остальным операциям (частота парка × стоимость пути отказа плюс то же у
  enrollment-server ≤ четверти цикла — решение, записанное в §5.2); цена двух слотов —
  пропускная способность парка 2 / (заливка + разбор) — 16,2 части в секунду при 50 Мбит/с
  самой широкой частью, 20,3 компактной, 27 при 100 Мбит/с (один цикл разбирает не больше ~20:
  на насыщении он занят отчётами целиком — это и есть назначение потолка одновременности,
  задержку держит оконная модель), при §5.7 нужно 50 частей в минуту.
  Путь отказа не дороже пути приёма: ``BoundedBodyRoute`` (``agent_api/body.py``, на каждой
  операции раздела по её модели) сверяет тело по байтам с ``BODY_MAX_OPENERS`` и
  ``BODY_MAX_COMMAS`` до разбора JSON — иначе тело в 1 МиБ из 131 000 лишних ключей порождало
  бы 131 000 ошибок pydantic (76 мс на рабочей машине) до усечения ответа; худшее тело,
  прошедшее проверку формы (3 000 элементов по три лишних ключа — 18 000 ошибок pydantic),
  меряет стенд (раунд 8). Рост парка
  до 20 000 пользователей (§5.7) контракт переживает
  без изменений — отчёт **делится** на части по правилам README каталога контрактов (один
  интервал — несколько ``report_seq``, пользователь в одной части, пороги §5.9 считаются по
  интервалу, счётчики интерфейса несёт первая часть). Тело такого размера пропускает только
  ``location = /agent/v1/reports`` nginx — с пределом, выведенным из этих же констант;
- нода отчитывается только за себя (§11.3 — нода недоверенная): ``node_id`` в теле
  необязателен, но предъявленный сверяется с identity запроса — 403 ``node_mismatch``.

Ответ 200 несёт ``last_accepted_seq`` — точку продолжения агента (§5.9): 0 — в этой эпохе
счётчиков ещё ничего не принято, поэтому номера отчётов начинаются с единицы.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.accounting.canonical import Address, CanonicalUuid, Timestamp
from app.accounting.device_limit import DeviceLimitService
from app.accounting.quota import QuotaService
from app.body_shape import shape_limits
from app.db.pool import transaction
from app.domain.composition import node_not_approved
from app.domain.nodes import Node
from app.domain.statuses import INT8_MAX, STREAM_GATE
from app.errors import ApiError

# Потолок одной части отчёта — из бюджета Н-4 (по ``urt``) при двух одновременных частях на
# парк (см. докстринг модуля): 16,7 мкс на элемент (p95 стенда при двух потоках отчётов на этом
# теле), 3 000 элементов — 50 мс, 2 × 50 + 3 × 4 + 2 × 12 (операции парка и enrollment за
# окно) = 136 мс ≤ 200 − 50. Пользователей больше — частей больше, контракт от этого не
# меняется; §5.7 (около 400 пользователей на ноду) — одна часть.
REPORT_MAX_LINES = 500
REPORT_MAX_ONLINE_IPS = 2_500
# Период отчёта: штатный интервал — 60 с (§5.9, обязанность агента: от него выведен Н-17);
# предел — час: длиннее только дельта после перерыва снятия счётчиков, которую агент не может
# разделить по часам (отказ терял бы данные). Строка относится к часу по ``period_start``
# (§4.2.5): интервал, пересёкший границу часа UTC, целиком ложится в час начала — ошибка
# ограничена длиной интервала; перерыв дольше часа — новая эпоха счётчиков (README).
REPORT_MAX_PERIOD = dt.timedelta(hours=1)
# Частей на интервал: 1 000 частей × 500 строк — полмиллиона пользователей на ноде, в 25 раз
# больше §5.7 (20 000 пользователей на весь парк); граница нужна, чтобы поле было ограничено с
# обеих сторон, как остальные, а не как ёмкость.
REPORT_MAX_PARTS = 1_000
# Срок принудительного закрытия интервала после приёма его последней части (001.34): выведен
# из Н-8 — нода буферизует статистику не меньше 24 часов и сливает буфер по возрастанию
# ``report_seq``, поэтому часть интервала законно приходит через сутки после предыдущей (Control
# Plane был недоступен) — плюс ``REPORT_MAX_PERIOD``. Раньше срока интервал закрывает часть
# следующего интервала ноды (части идут подряд). Число незавершённых интервалов ноды держит не
# срок, а ``REPORT_MAX_OPEN_INTERVALS`` (штатно открыт один). Числа контракта (README), читает
# 001.34; состояние — ``report_intervals`` (§4.2.5).
REPORT_PARTS_DEADLINE = dt.timedelta(hours=25)
REPORT_MAX_OPEN_INTERVALS = 8
# Потолок возраста открытого интервала от первой принятой части (закрытие по возрасту —
# 001.37, ``close_reason = 'age'``): срок ``REPORT_PARTS_DEADLINE`` считается от последней части
# и сам по себе позволял бы тянуть интервал по части в сутки до ``REPORT_MAX_PARTS`` суток;
# два буфера Н-8 покрывают законный перерыв (сутки недоступности плюс сутки на слив) и не
# меньше срока после последней части — иначе срок не применялся бы никогда.
REPORT_MAX_INTERVAL_AGE = dt.timedelta(hours=48)
# Смен эпохи счётчиков у ноды за сутки: каждая — разрыв для сверки (001.37), и без потолка
# смена эпохи была бы бесплатным способом плодить разрывы; перезапуски Xray и агента штатно —
# единицы в сутки. Сверх — удержание частей (``held_anomaly``) и алерт (001.34/001.68).
REPORT_MAX_EPOCHS_PER_DAY = 48
# Ошибка конфигурации агента (буфер или идентификатор чужой ноды): повтор без вмешательства
# оператора бессмысленен — пауза как у 426 (``deps.RETRY_AFTER_UPGRADE``).
RETRY_AFTER_NODE_MISMATCH = 3600
# Допуск часов ноды (§5.9: синхронизация по NTP, отклонение не больше 5 с) — к границам периода по
# часам сервера: ``period_end`` не позже часов сервера плюс допуск (опережение и есть отклонение,
# которое видно по одному отчёту: отставание неотличимо от досылки буфера), левая граница первого
# интервала — не раньше якоря минус допуск.
TIME_TOLERANCE = dt.timedelta(seconds=5)
# Глубина буфера Н-8: якорь **первого интервала ноды** — не раньше суток назад. Только первого
# (решение заказчика 2026-10-20, роаст 001.34, раунд 2): «и первого после закрытия всех» означало
# почти любой интервал — однокусочный закрывается сразу, — и якорь отказывал при ротации identity и
# сливе буфера после суточной недоступности Control Plane. Дальше держат монотонность и окно
# приёма (``REPORT_ACCEPT_WINDOW``).
REPORT_BUFFER_DEPTH = dt.timedelta(hours=24)
# Окно приёма по ``period_start`` нового интервала — явная проверка (``too_old``), а не вывод из
# правил: якорь действует только для первого интервала ноды, и цепочка интервалов, каждый из
# которых закрывает предыдущий недосланным, отставала бы от стенных часов без предела (роаст 001.34,
# раунд 1). Законное отставание — сутки буфера до первой части открытого интервала плюс его
# потолок возраста: 72 ч. Внутри 14 суток партиций ``traffic_lines`` (впереди — не дальше допуска:
# ``period_end`` не позже часов сервера плюс 5 с, планировщик держит партиции на 7 суток), так
# что вставка 001.77 не упадёт «no partition … found» ошибкой базы.
REPORT_ACCEPT_WINDOW = REPORT_BUFFER_DEPTH + REPORT_MAX_INTERVAL_AGE
# Метрика длительности интервалов (``interval_durations``): окно — последний час приёма, верхние
# границы корзин — секунды; штатный интервал (60 с) — первая корзина, предел периода (час) —
# последняя конечная.
REPORT_INTERVAL_WINDOW = dt.timedelta(hours=1)
REPORT_INTERVAL_BUCKETS = (60, 120, 300, 900, 3600)

# Причины закрытия интервала (``report_intervals.close_reason``): ``capped`` — интервал части
# сверх потолка открытых интервалов рождается закрытым (``_Acceptance._open``).
CloseReason = Literal["completed", "superseded", "deadline", "age", "capped"]


def anomaly_threshold(bandwidth_mbps: int, duration: dt.timedelta) -> int:
    """Порог прироста §5.9 — физически достижимый объём пользовательского трафика ноды за
    интервал: полоса порта (Мбит/с) × длительность. Байт пользователя проходит порт VPS дважды —
    входит от одной стороны и выходит к другой, — поэтому предел держит сумму uplink + downlink, а
    не каждое направление (решение заказчика 2026-10-20, вместо раунда 8 001.33). Целое число
    байт вниз: Мбит/с × мкс — это биты, делённые на восемь, — без дробной арифметики и с точностью
    меток времени (микросекунда). 100 Мбит/с за 60 с — 750 000 000 байт (TC-UNIT-01)."""
    return bandwidth_mbps * (duration // dt.timedelta(microseconds=1)) // 8


@dataclass(frozen=True, slots=True)
class Charge:
    """Доля части в пределах порога, разложенная по направлениям, — то, что прибавляется к
    нарастающим суммам интервала и что спишет 001.77, — и признак удержания (сумма интервала с
    частью превысила порог)."""

    uplink: int
    downlink: int
    held: bool


def charge(ceiling: int, accepted: tuple[int, int], part: tuple[int, int]) -> Charge:
    """Порог §5.9 к нарастающей сумме принятых частей интервала — к сумме uplink + downlink
    (``anomaly_threshold``). Сумма с этой частью не больше порога — часть целиком; больше —
    удержание, и к сумме идёт только остаток до порога: удержание не освобождает от списания, но
    и следующая удержанная часть не спишет тот же остаток повторно. Остаток раскладывается по
    направлениям пропорционально части — uplink вниз, downlink — дополнение до остатка: сумма
    равна остатку ровно, и ни одно направление не получает больше, чем прислано. ``accepted`` и
    ``part`` — пары (uplink, downlink); сумма, оказавшаяся выше порога (полосу ноды уменьшили между
    частями), даёт остаток ноль."""
    total = part[0] + part[1]
    room = max(0, ceiling - accepted[0] - accepted[1])
    if total <= room:
        return Charge(part[0], part[1], held=False)
    uplink = room * part[0] // total
    return Charge(uplink, room - uplink, held=True)


def closing(
    first_received_at: dt.datetime, last_received_at: dt.datetime
) -> tuple[dt.datetime, CloseReason]:
    """Момент и причина закрытия открытого интервала по времени: срок ``REPORT_PARTS_DEADLINE``
    после приёма последней части или потолок возраста ``REPORT_MAX_INTERVAL_AGE`` от первой — что
    раньше; при равенстве — возраст (жёсткий потолок). Закрытие записывается этим моментом, а не
    моментом, когда приём его заметил: сверке непрерывности (001.37) нужна граница, а не время
    обнаружения."""
    deadline = last_received_at + REPORT_PARTS_DEADLINE
    age = first_received_at + REPORT_MAX_INTERVAL_AGE
    return (age, "age") if age <= deadline else (deadline, "deadline")


class ReportLine(BaseModel):
    """Строка отчёта — дельты счётчиков пользователя за период (§5.9 «Хранимые поля записи»):
    колонки ``raw_uplink_bytes`` и ``raw_downlink_bytes`` таблиц §4.2.5 (сырой объём; списание
    считает Control Plane). Единица — в имени: та же величина в телеметрии — ``net_rx_bytes``,
    и число без единицы вторая сторона обмена прочла бы по-своему."""

    model_config = ConfigDict(extra="forbid")

    user_id: CanonicalUuid
    uplink_bytes: int = Field(strict=True, ge=0, le=INT8_MAX)
    downlink_bytes: int = Field(strict=True, ge=0, le=INT8_MAX)


class OnlineIp(BaseModel):
    """Наблюдаемый адрес пользователя с временем последней активности (§4.13; строка
    ``user_online_ips`` §4.2.5). Адреса назначения не собираются (§16.3)."""

    model_config = ConfigDict(extra="forbid")

    user_id: CanonicalUuid
    ip: Address
    last_seen: Timestamp


class ReportIn(BaseModel):
    """Тело ``POST /agent/v1/reports`` (§5.2): ключ идемпотентности ``(node_id, counter_epoch,
    report_seq)``, границы периода по часам ноды, строки по пользователям, наблюдаемые адреса,
    счётчики интерфейса ноды.

    Интервал отчёта задаётся ``(counter_epoch, period_start, period_end)``; интервалы ноды
    монотонны и не перекрываются во времени, ``period_end`` не позже часов сервера с допуском
    (001.34 — так порог §5.9 к нарастающей сумме частей ограничивает объём по стенным часам);
    отчёт за интервал может состоять из нескольких частей с последовательными ``report_seq``
    (README каталога контрактов): ``parts_total`` — объявленное число частей интервала
    (последняя — ``report_seq`` первой + ``parts_total`` − 1; модель проверяет достаточное
    условие по номеру этой части — ``report_seq`` + ``parts_total`` − 1 не дальше ``bigint``,
    точное по номеру первой — 001.34); оно может только расти в пределах интервала (деление
    отказанной по размеру части
    добавляет части), действует последнее принятое и применяется до проверки диапазона,
    меньше прежнего — отказ по контракту (001.34); интервал закрывается завершением, частью
    следующего интервала ноды или сроком ``REPORT_PARTS_DEADLINE``, часть к закрытому — отказ
    ``interval_closed``; открытых интервалов у ноды — не больше ``REPORT_MAX_OPEN_INTERVALS``;
    пользователь встречается в одной части интервала, а ``node_rx_bytes``/``node_tx_bytes`` —
    дельты счётчиков интерфейса за интервал (второй источник сверки §5.9, в списание не
    входят) — несёт только первая часть, продолжения шлют нули (001.77 суммирует части).

    ``node_id`` необязателен: ноду определяет identity запроса (§7.1), а предъявленный
    идентификатор — сверка, что буфер агента принадлежит этой ноде
    (``AccountingService.accept_report``)."""

    model_config = ConfigDict(extra="forbid")

    node_id: CanonicalUuid | None = Field(default=None, description="сверяется с identity запроса")
    counter_epoch: CanonicalUuid = Field(
        description="эпоха счётчиков Xray: новая после перезапуска"
    )
    report_seq: int = Field(
        strict=True,
        ge=1,
        le=INT8_MAX,
        description="номер в пределах эпохи с единицы; агент нумерует по возрастанию",
    )
    parts_total: int = Field(
        strict=True,
        ge=1,
        le=REPORT_MAX_PARTS,
        description="объявленное число частей интервала: растёт только вверх, действует "
        "последнее принятое",
    )
    period_start: Timestamp
    period_end: Timestamp
    lines: list[ReportLine] = Field(max_length=REPORT_MAX_LINES)
    online_ips: list[OnlineIp] = Field(max_length=REPORT_MAX_ONLINE_IPS)
    node_rx_bytes: int = Field(
        strict=True,
        ge=0,
        le=INT8_MAX,
        description="дельта счётчика интерфейса за интервал; в продолжениях 0",
    )
    node_tx_bytes: int = Field(strict=True, ge=0, le=INT8_MAX)

    @model_validator(mode="after")
    def _period_and_keys(self) -> ReportIn:
        """Границы периода и уникальность ключей — те же ограничения, что у таблиц §4.2.5
        (``CHECK (period_end > period_start)``, PK ``traffic_lines`` по пользователю, PK
        ``user_online_ips`` по паре в пределах ноды): отказ по контракту, а не ошибкой вставки;
        номер последней части интервала (``report_seq`` + ``parts_total`` − 1) — в ширине
        ``bigint``, иначе интервал нельзя было бы завершить.
        Ключи сравниваются как целые (``uuid.int``; адрес — вместе с версией, как ``inet``) —
        разобранный адрес, а не его текст, и хеширование на уровне C, а не в Python."""
        if self.period_end <= self.period_start:
            raise ValueError("period_end должен быть позже period_start")
        if self.period_end - self.period_start > REPORT_MAX_PERIOD:
            raise ValueError("период отчёта не дольше часа")
        if self.report_seq + self.parts_total - 1 > INT8_MAX:
            raise ValueError("номер последней части интервала вышел бы за предел bigint")
        if len({line.user_id.int for line in self.lines}) != len(self.lines):
            raise ValueError("пользователь встречается в строках отчёта дважды")
        pairs = {(row.user_id.int, row.ip.version, int(row.ip)) for row in self.online_ips}
        if len(pairs) != len(self.online_ips):
            raise ValueError("пара «пользователь × адрес» встречается в адресах дважды")
        return self


class Accept(BaseModel):
    """Ответ приёма (§5.9): точка продолжения агента и признак повторной доставки (Н-20).
    ``last_accepted_seq`` — наибольший номер **принятого** отчёта эпохи: продвигается и при
    удержании до разбора (``held_anomaly`` — отчёт сохранён, его строки и адреса пишет шаг
    фактов 001.77 в той же транзакции, и нода освобождает буфер), не продвигается при отказе
    (``rejected_time``, ``interval_closed`` отвечают 409 — ``refused()``, а не этим ответом; агент
    такой отчёт не повторяет — разрыв номеров фиксирует сверка непрерывности)."""

    last_accepted_seq: int = Field(
        strict=True, ge=0, le=INT8_MAX, description="0 — в эпохе ничего не принято"
    )
    duplicate: bool


# Пределы формы тела по байтам — для проверки до разбора JSON (``agent_api.reports``
# ``BoundedBodyRoute``): у любого допустимого тела открывающих скобок не больше, чем объектов и
# массивов в самом большом теле, а запятых — не больше, чем разделителей его членов и
# элементов; строки допустимых значений (UUID, метки, адреса) этих символов не содержат (тело с
# дублями ключей JSON модель приняла бы — побеждает последний, — но запятых у него больше, и оно
# отвергается как широкое; контракт дублей не предусматривает, сериализатор Go их не пишет).
# Тело сверх любого предела недопустимо заведомо, и разбирать его незачем: иначе 1 МиБ из 131 000
# лишних ключей стоил бы 131 000 ошибок pydantic (76 мс) до усечения ответа, а 350 000
# вложенных пустых массивов — 22 МиБ в ``json.loads``. Сама проверка — ``bytes.count`` и поиск
# серии цифр по маске, 0,5 мс на самой широкой части.
# Пределы формы тела части — из модели рекурсивно (``app.body_shape``): скобка объекта отчёта и
# двух списков плюс по скобке на строку и адрес; запятые — между полями отчёта, между полями
# каждой строки и адреса, между элементами списков. Тест ``test_body_shape.py`` сверяет вывод с
# ручной формулой, ``test_reports.py`` — со счётчиками самого большого тела; проверку по байтам
# выполняет ``agent_api.body.BoundedBodyRoute`` на каждой операции раздела по её модели.
REPORT_SHAPE = shape_limits(ReportIn)
BODY_MAX_OPENERS = REPORT_SHAPE.max_openers
BODY_MAX_COMMAS = REPORT_SHAPE.max_commas


def node_mismatch(expected: uuid.UUID) -> ApiError:
    """Отчёт несёт идентификатор другой ноды: буфер или конфигурация агента принадлежат не
    этой identity (пересозданная нода §4.5, скопированный образ). Ключ идемпотентности
    ``(node_id, …)`` при этом лёг бы в чужое пространство отчётов. В ``details`` — собственный
    идентификатор вызывающей ноды (он ей известен из enrollment), не чужой."""
    return ApiError(
        "node_mismatch",
        "отчёт содержит идентификатор другой ноды",
        status=403,
        details={"node_id": str(expected)},
        headers={"Retry-After": str(RETRY_AFTER_NODE_MISMATCH)},
    )


def own_node(node: Node, report: ReportIn) -> None:
    """Нода отчитывается только за себя (§11.3): ``node_id`` тела, если предъявлен, обязан
    совпасть с нодой запроса — иначе 403 ``node_mismatch``. Ключ идемпотентности строится по
    ноде запроса, никогда по полю тела; правило — до транзакции, базу оно не трогает."""
    if report.node_id is not None and report.node_id != node.id:
        raise node_mismatch(node.id)


# Исходы приёма, которые записываются строкой ``traffic_reports``: два принимающих (200 —
# точка продолжения продвигается) и два отказа (409 — не продвигается, агент часть не повторяет,
# номер остаётся пропущенным). Отказы по контракту частей (422 ``report_inconsistent``) ключа не
# записывают — как отказ модели.
Recorded = Literal["accepted", "held_anomaly", "rejected_time", "interval_closed"]
Refused = Literal["rejected_time", "interval_closed"]
ACCEPTING: frozenset[str] = frozenset(("accepted", "held_anomaly"))
_REFUSAL_MESSAGES: dict[str, str] = {
    "rejected_time": "границы периода вне допуска часов сервера или монотонности интервалов ноды",
    "interval_closed": "часть к закрытому интервалу или вне диапазона открытого",
}


def refused(status: Refused, reason: str, last_accepted_seq: int) -> ApiError:
    """409 отказа части: ``rejected_time`` (``future`` — ``period_end`` впереди часов сервера
    сверх допуска; ``too_old`` — новый интервал старше окна приёма ``REPORT_ACCEPT_WINDOW``;
    ``overlap`` — новый интервал начинается раньше конца последнего принятого интервала ноды, и
    через смену эпохи тоже; ``before_anchor`` — первый интервал раньше якоря) или
    ``interval_closed`` (``closed`` — интервал завершён или закрыт; ``out_of_range`` — номер вне
    диапазона открытого). ``repeated`` — повтор уже отказанного ключа с теми же записанными
    полями (границы, число частей, счётчики интерфейса): ответ потерян, отказ тот же. В
    ``details.last_accepted_seq`` — точка продолжения: отказ её не сдвигает, а агент продолжает с
    ``max(last_accepted_seq, номер отказанной) + 1``."""
    return ApiError(
        status,
        _REFUSAL_MESSAGES[status],
        status=409,
        details={"reason": reason, "last_accepted_seq": last_accepted_seq},
    )


def inconsistent(reason: str) -> ApiError:
    """422 ``report_inconsistent``: часть противоречит уже принятому — ``parts_total_decreased``
    (объявленное число частей меньше прежнего), ``counters_in_continuation`` (счётчики
    интерфейса несёт только первая часть), ``user_in_another_part`` (пользователь встречается в
    одной части интервала), ``seq_reused`` (номер уже принят или отказан с другими границами,
    числом частей или счётчиками интерфейса — ответ «дубликат» молча выбросил бы отчёт из
    буфера; строки сверит 001.77, когда они будут записаны). Ошибка агента: ключ не
    записывается, повтор как есть бессмысленен."""
    return ApiError(
        "report_inconsistent",
        "часть отчёта противоречит принятым частям интервала",
        status=422,
        details={"reason": reason},
    )


@dataclass(frozen=True, slots=True)
class Members:
    """Строки и адреса части, чьи пользователи состоят в составе ноды (``node_user_state``), и
    число отброшенных: нода недоверенная (§11.3), и строка чужого пользователя списала бы ему
    трафик, а адрес — заблокировал бы его на всех нодах (001.38)."""

    lines: list[ReportLine]
    online_ips: list[OnlineIp]
    dropped_lines: int
    dropped_ips: int

    @property
    def volume(self) -> tuple[int, int]:
        """Объём части по направлениям (uplink, downlink); их сумма — база порога §5.9."""
        return (
            sum(line.uplink_bytes for line in self.lines),
            sum(line.downlink_bytes for line in self.lines),
        )


@dataclass(frozen=True, slots=True)
class Outcome:
    """Итог приёма части в транзакции: записанный статус, точка продолжения, причина отказа или
    удержания, доля в пределах порога, порог и строки состава — то, что шаги списания 001.77
    возьмут из той же транзакции."""

    status: Recorded
    last_accepted_seq: int
    duplicate: bool = False
    reason: str = ""
    holds: tuple[str, ...] = ()
    charged: Charge | None = None
    ceiling: int = 0
    members: Members | None = None


_LOCK_NODE = (
    "select status, bandwidth_mbps, decommissioned_at is not null as gone from nodes "
    "where id = $1 for no key update"
)
# Отдельным оператором после блокировки: в read committed каждый оператор видит свой снимок, и
# отзыв, которого ждал оператор блокировки, здесь уже виден. Тот же оператор с соединением по
# node_identities проверил бы identity по снимку до ожидания (повторная проверка read committed
# перечитывает только заблокированную строку).
_IDENTITY_AFTER_LOCK = (
    "select statement_timestamp() as at, (select issued_at from node_identities "
    "where node_id = $1 and cert_fingerprint = $2 and revoked_at is null "
    "and expires_at > statement_timestamp()) as issued_at"
)
_KNOWN_KEY = (
    "select r.status::text as status, r.period_start, r.period_end, r.parts_total, "
    "r.node_rx_bytes, r.node_tx_bytes, "
    "(select report_seq from traffic_reports where node_id = $1 and counter_epoch = $2 "
    "and status in ('accepted', 'held_anomaly') order by report_seq desc limit 1) as last_accepted "
    "from (values (1)) as one (x) left join traffic_reports r "
    "on r.node_id = $1 and r.counter_epoch = $2 and r.report_seq = $3"
)
_OPEN_INTERVALS = (
    "select counter_epoch, period_start, period_end, first_received_at, last_received_at "
    "from report_intervals where node_id = $1 and closed_at is null"
)
_CLOSE_BY_TIME = (
    "update report_intervals set closed_at = $5, close_reason = $6 where node_id = $1 "
    "and counter_epoch = $2 and period_start = $3 and period_end = $4 and closed_at is null"
)
_INTERVAL = (
    "select first_seq, parts_total, parts_accepted, accepted_uplink_bytes, "
    "accepted_downlink_bytes, closed_at from report_intervals where node_id = $1 "
    "and counter_epoch = $2 and period_start = $3 and period_end = $4"
)
_MEMBERS = "select user_id from node_user_state where node_id = $1 and user_id = any($2::uuid[])"
_IN_ANOTHER_PART = (
    "select true from traffic_lines where period_start = $3 and node_id = $1 "
    "and counter_epoch = $2 and period_end = $4 and user_id = any($5::uuid[]) limit 1"
)
_LAST_INTERVAL = (
    "select counter_epoch, period_end from report_intervals where node_id = $1 "
    "order by period_end desc limit 1"
)
_FIRST_ASSIGNMENT = "select min(valid_from) from node_billing_assignments where node_id = $1"
# Смены эпохи — пары соседних по времени интервалов с разными эпохами, а не различные эпохи:
# чередование двух эпох — тоже смены (роаст 001.34, раунд 1). Смена случилась, когда открыт второй
# интервал пары, поэтому считаются пары, чей второй интервал открыт в окне, — и та, где первый
# открыт до окна (последний интервал до окна берётся предшественником; раунд 2, L-5). Выборка
# ограничена моментом открытия предшественника — по индексу ``(node_id, first_received_at)``: без
# этой границы каждая смена эпохи читала бы под блокировкой ноды всю её историю интервалов
# (раунд 3, P-1).
_EPOCH_CHANGES = (
    "with before as (select period_start, first_received_at from report_intervals "
    "where node_id = $1 and first_received_at <= $2 order by first_received_at desc limit 1) "
    "select count(*) filter (where previous <> counter_epoch and first_received_at > $2) from "
    "(select counter_epoch, first_received_at, "
    "lag(counter_epoch) over (order by period_start) as previous from report_intervals "
    "where node_id = $1 "
    "and first_received_at >= coalesce((select first_received_at from before), '-infinity') "
    "and period_start >= coalesce((select period_start from before), '-infinity')) as w"
)
_INSERT_REPORT = (
    "insert into traffic_reports (node_id, counter_epoch, report_seq, parts_total, "
    "period_start, period_end, received_at, status, node_rx_bytes, node_tx_bytes) "
    "values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) on conflict do nothing returning true"
)
_OPEN_INTERVAL = (
    "insert into report_intervals (node_id, counter_epoch, period_start, period_end, first_seq, "
    "parts_total, parts_accepted, accepted_uplink_bytes, accepted_downlink_bytes, held, "
    "first_received_at, last_received_at, closed_at, close_reason) "
    "values ($1, $2, $3, $4, $5, $6, 1, $7, $8, $9, $10, $10, $11, $12)"
)
# parts_accepted справа — значение до обновления: часть, принятая последней, завершает интервал.
# ``closed_at is null`` — интервал, закрытый встречной записью, не переоткрывается (приём держит
# строку ноды, и закрытие 001.37 обязано брать её же); ``greatest`` — часы базы, шагнувшие назад
# между частями, не нарушают ``last_received_at >= first_received_at`` ошибкой вставки.
_CONTINUE_INTERVAL = (
    "update report_intervals set parts_total = $5, parts_accepted = parts_accepted + 1, "
    "accepted_uplink_bytes = accepted_uplink_bytes + $6, "
    "accepted_downlink_bytes = accepted_downlink_bytes + $7, held = held or $8, "
    "last_received_at = greatest(last_received_at, $9), "
    "closed_at = case when parts_accepted + 1 = $5 then $9::timestamptz end, "
    "close_reason = case when parts_accepted + 1 = $5 then 'completed' end "
    "where node_id = $1 and counter_epoch = $2 and period_start = $3 and period_end = $4 "
    "and closed_at is null"
)
_SUPERSEDE = (
    "update report_intervals set closed_at = $2, close_reason = 'superseded' "
    "where node_id = $1 and closed_at is null and period_end <= $3"
)
# Окно счёта смен эпохи — скользящие сутки (``REPORT_MAX_EPOCHS_PER_DAY``).
REPORT_EPOCH_WINDOW = dt.timedelta(hours=24)

log = logging.getLogger(__name__)


class _Acceptance:
    """Приём одной части в транзакции вызывающего: блокировка ноды, перепроверка identity и
    вентиля, ключ, состав, время, закрытие истёкших, интервал, пороги, запись. Порядок — этого
    модуля (README задаёт правила, а не их порядок): повтор ключа отвечает раньше всего (ответ на
    повтор не зависит от часов и состава), состав — до первого отказа (алерт о чужих
    пользователях — и при отказе), отказ по времени — раньше правил интервала, правила открытого
    интервала — раньше порога."""

    def __init__(self, node_id: uuid.UUID, fingerprint: str, report: ReportIn) -> None:
        self.conn: asyncpg.Connection
        self.node_id = node_id
        self.fingerprint = fingerprint
        self.report = report
        self.key = (node_id, report.counter_epoch, report.period_start, report.period_end)
        self.at = dt.datetime.min.replace(tzinfo=dt.UTC)
        self.bandwidth_mbps = 0
        self.issued_at = self.at
        self.last_accepted = 0
        self.members: Members | None = None

    async def run(self, conn: asyncpg.Connection) -> Outcome:
        """Приём на подключении транзакции вызывающего."""
        self.conn = conn
        await self._lock()
        known = await self.conn.fetchrow(
            _KNOWN_KEY, self.node_id, self.report.counter_epoch, self.report.report_seq
        )
        self.last_accepted = known["last_accepted"] or 0
        if known["status"] is not None:
            return self._replay(known)
        # Состав — до первого отказа: строка чужого пользователя — сигнал §11.3 и тогда, когда
        # часть отказана по другой причине (алерт — ``accept_report``). Повтор ключа и
        # ``seq_reused`` отвечают раньше: их часть уже была разобрана при первой доставке.
        members = self.members = await self._members()
        if self.report.period_end > self.at + TIME_TOLERANCE:
            return await self._refuse("rejected_time", "future")
        still_open = await self._close_expired()
        interval = await self.conn.fetchrow(_INTERVAL, *self.key)
        if interval is not None:
            return await self._continue(interval, members)
        return await self._open(still_open, members)

    async def _lock(self) -> None:
        """Строка ноды под блокировкой — один приём ноды за раз (правила интервала читают и пишут
        её состояние) и порядок с отзывом identity, который держит ту же строку (001.25). Под
        блокировкой — перепроверка: identity запроса не отозвана и не истекла, нода не выведена
        (401, как раздел), вентиль §4.6 (``pending`` — 403) — отзыв или повторное опознание,
        зафиксированные между опознанием запроса и этой транзакцией, иначе не остановили бы
        начисление. Отсюда же полоса ноды (порог §5.9) и момент сервера ``at``."""
        from app.agent_api.deps import unknown_identity  # deps импортирует эту службу

        node = await self.conn.fetchrow(_LOCK_NODE, self.node_id)
        if node is None or node["gone"]:
            raise unknown_identity()
        identity = await self.conn.fetchrow(_IDENTITY_AFTER_LOCK, self.node_id, self.fingerprint)
        if identity["issued_at"] is None:
            raise unknown_identity()
        if STREAM_GATE[node["status"]].refused:
            raise node_not_approved()
        self.at = identity["at"]
        self.issued_at = identity["issued_at"]
        self.bandwidth_mbps = node["bandwidth_mbps"]

    def _replay(self, known: asyncpg.Record) -> Outcome:
        """Ключ уже записан. Те же записанные поля (границы, число частей, счётчики интерфейса) —
        повтор (Н-20): принятый и удержанный отвечают 200 с ``duplicate`` и текущей точкой
        продолжения, отказанный — тем же отказом. Иначе номер переиспользован (``seq_reused``)."""
        report = self.report
        recorded = (
            known["period_start"],
            known["period_end"],
            known["parts_total"],
            known["node_rx_bytes"],
            known["node_tx_bytes"],
        )
        sent = (*self.key[2:], report.parts_total, report.node_rx_bytes, report.node_tx_bytes)
        if recorded != sent:
            raise inconsistent("seq_reused")
        status: Recorded = known["status"]
        if status in ACCEPTING:
            return Outcome(status, self.last_accepted, duplicate=True)
        return Outcome(status, self.last_accepted, reason="repeated")

    async def _close_expired(self) -> int:
        """Закрыть открытые интервалы ноды, чей срок или возраст истёк к ``at`` (``closing``), и
        вернуть число оставшихся открытыми. Периодическое закрытие молчащих нод — 001.37; приём
        закрывает сам, иначе между проходами задачи правила частей видели бы истёкший интервал
        открытым."""
        rows = await self.conn.fetch(_OPEN_INTERVALS, self.node_id)
        expired = []
        for row in rows:
            moment, reason = closing(row["first_received_at"], row["last_received_at"])
            if moment <= self.at:
                expired.append(
                    (
                        self.node_id,
                        row["counter_epoch"],
                        row["period_start"],
                        row["period_end"],
                        moment,
                        reason,
                    )
                )
        if expired:
            await self.conn.executemany(_CLOSE_BY_TIME, expired)
        return len(rows) - len(expired)

    async def _members(self) -> Members:
        """Строки и адреса пользователей из состава ноды; остальное отбрасывается (алерт —
        ``accept_report``, при любом исходе части, кроме повтора ключа). Член состава — строка
        ``node_user_state`` в любом состоянии: ``removed`` до уплотнения тоже (буфер досылает
        трафик до отзыва)."""
        users = {line.user_id for line in self.report.lines}
        users |= {row.user_id for row in self.report.online_ips}
        found = {r["user_id"] for r in await self.conn.fetch(_MEMBERS, self.node_id, list(users))}
        lines = [line for line in self.report.lines if line.user_id in found]
        ips = [row for row in self.report.online_ips if row.user_id in found]
        return Members(
            lines,
            ips,
            dropped_lines=len(self.report.lines) - len(lines),
            dropped_ips=len(self.report.online_ips) - len(ips),
        )

    def _ceiling(self) -> int:
        return anomaly_threshold(
            self.bandwidth_mbps, self.report.period_end - self.report.period_start
        )

    async def _record(self, status: Recorded) -> bool:
        """Строка отчёта со статусом. Ключ проверен под блокировкой ноды, и конфликт здесь —
        только встречная запись того же ключа без неё (сегодня путь недостижим); ``ON CONFLICT DO
        NOTHING`` не даёт ей стать ошибкой вставки: приём отвечает дубликатом, отказ записанного
        ключа не дублирует."""
        report = self.report
        inserted = await self.conn.fetchval(
            _INSERT_REPORT,
            self.node_id,
            report.counter_epoch,
            report.report_seq,
            report.parts_total,
            report.period_start,
            report.period_end,
            self.at,
            status,
            report.node_rx_bytes,
            report.node_tx_bytes,
        )
        return bool(inserted)

    async def _refuse(self, status: Refused, reason: str) -> Outcome:
        """Отказ части записывается со статусом (``rejected_time``, ``interval_closed`` — §4.2.5):
        его номер остаётся пропущенным, разрыв видит сверка непрерывности (001.37)."""
        await self._record(status)
        return Outcome(status, self.last_accepted, reason=reason)

    async def _accept(self, status: Recorded, **fields: Any) -> Outcome:
        if not await self._record(status):
            return Outcome(status, self.last_accepted, duplicate=True)
        last = max(self.last_accepted, self.report.report_seq)
        return Outcome(status, last, **fields)

    async def _continue(self, interval: asyncpg.Record, members: Members) -> Outcome:
        """Часть открытого или закрытого интервала. Порядок — правила частей: закрытый — отказ;
        объявленное число частей не меньше прежнего и применяется до проверки диапазона; номер —
        от первой принятой до первой + ``parts_total`` − 1 (точное правило по номеру первой:
        модель держит ``report_seq`` + ``parts_total`` − 1 в ``bigint``, и для номера не ниже
        первой граница первой + ``parts_total`` − 1 тоже в нём); счётчики интерфейса — только у
        первой части; пользователь — в одной части интервала; затем порог к нарастающей
        сумме."""
        report = self.report
        if interval["closed_at"] is not None:
            return await self._refuse("interval_closed", "closed")
        if report.parts_total < interval["parts_total"]:
            raise inconsistent("parts_total_decreased")
        first = interval["first_seq"]
        if not first <= report.report_seq <= first + report.parts_total - 1:
            return await self._refuse("interval_closed", "out_of_range")
        if report.node_rx_bytes or report.node_tx_bytes:
            raise inconsistent("counters_in_continuation")
        users = [line.user_id for line in members.lines]
        if users and await self.conn.fetchval(_IN_ANOTHER_PART, *self.key, users):
            raise inconsistent("user_in_another_part")
        ceiling = self._ceiling()
        accepted = (interval["accepted_uplink_bytes"], interval["accepted_downlink_bytes"])
        part = charge(ceiling, accepted, members.volume)
        status: Recorded = "held_anomaly" if part.held else "accepted"
        outcome = await self._accept(
            status,
            holds=("anomaly",) if part.held else (),
            charged=part,
            ceiling=ceiling,
            members=members,
        )
        if outcome.duplicate:
            return outcome
        updated = await self.conn.execute(
            _CONTINUE_INTERVAL,
            *self.key,
            report.parts_total,
            part.uplink,
            part.downlink,
            part.held,
            self.at,
        )
        if updated != "UPDATE 1":
            raise RuntimeError(f"интервал отчёта закрыт встречной записью: {updated}")
        if not part.held:
            await self.conn.execute(_SUPERSEDE, self.node_id, self.at, report.period_start)
        return outcome

    async def _open(self, still_open: int, members: Members) -> Outcome:
        """Первая принятая часть нового интервала. Окно приёма — не старше
        ``REPORT_ACCEPT_WINDOW`` с допуском; монотонность — не раньше конца последнего интервала
        ноды (и через смену эпохи); якорь — только для первого интервала ноды (решение заказчика
        2026-10-20); удержания — порог, потолок смен эпохи и (для удержанной части) потолок открытых
        интервалов. Часть сверх потолка открытых интервал открывает **закрытым** (``capped``,
        удержан, без списания — решение заказчика 2026-10-20): он держит монотонность и правила
        частей (следующая часть того же интервала — ``interval_closed``), не занимая места среди
        открытых. Часть, которая не удержана, закрывает открытые интервалы до себя
        (``superseded``)."""
        report = self.report
        if report.period_start < self.at - REPORT_ACCEPT_WINDOW - TIME_TOLERANCE:
            return await self._refuse("rejected_time", "too_old")
        last = await self.conn.fetchrow(_LAST_INTERVAL, self.node_id)
        if last is not None and report.period_start < last["period_end"]:
            return await self._refuse("rejected_time", "overlap")
        if last is None and report.period_start < await self._anchor():
            return await self._refuse("rejected_time", "before_anchor")
        holds: list[str] = []
        ceiling = self._ceiling()
        part = charge(ceiling, (0, 0), members.volume)
        if part.held:
            holds.append("anomaly")
        if (
            last is not None
            and last["counter_epoch"] != report.counter_epoch
            and await self._epoch_over_cap()
        ):
            holds.append("epochs")
        capped = bool(holds) and still_open >= REPORT_MAX_OPEN_INTERVALS
        if capped:
            holds.append("open_intervals")
            part = Charge(0, 0, held=True)
        outcome = await self._accept(
            "held_anomaly" if holds else "accepted",
            holds=tuple(holds),
            charged=part,
            ceiling=ceiling,
            members=members,
        )
        if outcome.duplicate:
            return outcome
        reason: CloseReason | None = None
        if capped:
            reason = "capped"
        elif report.parts_total == 1:
            reason = "completed"
        await self.conn.execute(
            _OPEN_INTERVAL,
            *self.key,
            report.report_seq,
            report.parts_total,
            part.uplink,
            part.downlink,
            bool(holds),
            self.at,
            self.at if reason else None,
            reason,
        )
        if not holds:
            await self.conn.execute(_SUPERSEDE, self.node_id, self.at, report.period_start)
        return outcome

    async def _anchor(self) -> dt.datetime:
        """Левая граница первого интервала ноды (у ноды нет ни одного интервала): не раньше выдачи
        identity запроса и суток назад
        (глубина буфера Н-8) — обе с допуском часов ноды — и не раньше первого назначения ноды
        тарифицируемой группе без допуска: раньше него ``multiplier.resolve`` отвечает 1.0 по
        умолчанию с группой первого назначения, и секунды до ввода списались бы по 1.0
        (передано из 001.23). Ноду без назначений якорь не останавливает — её шаг списания
        отвечает ``NoBillingAssignmentError`` (001.77)."""
        bounds = [
            self.issued_at - TIME_TOLERANCE,
            self.at - REPORT_BUFFER_DEPTH - TIME_TOLERANCE,
        ]
        assigned = await self.conn.fetchval(_FIRST_ASSIGNMENT, self.node_id)
        if assigned is not None:
            bounds.append(assigned)
        return max(bounds)

    async def _epoch_over_cap(self) -> bool:
        """Часть меняет эпоху (её эпоха не та, что у последнего интервала ноды), и смен среди
        интервалов, открытых за скользящие сутки, с ней стало бы больше
        ``REPORT_MAX_EPOCHS_PER_DAY``. Смена — соседние по времени интервалы с разными эпохами,
        в том числе возврат к прежней эпохе."""
        changes = await self.conn.fetchval(
            _EPOCH_CHANGES, self.node_id, self.at - REPORT_EPOCH_WINDOW
        )
        return bool(changes + 1 > REPORT_MAX_EPOCHS_PER_DAY)


def _where(node_id: uuid.UUID, report: ReportIn) -> str:
    return f"node={node_id} epoch={report.counter_epoch} seq={report.report_seq}"


def _alert_foreign(node_id: uuid.UUID, report: ReportIn, members: Members | None) -> None:
    """ERROR об отброшенных строках и адресах чужих пользователей — при любом исходе части, в
    том числе при отказе и 422 по правилам интервала: попытка ноды списать трафик чужому
    пользователю — сигнал §11.3 сам по себе. Исключение — повтор ключа (``duplicate``,
    ``repeated``, ``seq_reused``): он отвечает до разбора состава (``_Acceptance.run``)."""
    if members is not None and (members.dropped_lines or members.dropped_ips):
        log.error(
            "отчёт: foreign_users %s lines=%d ips=%d — строки и адреса пользователей вне "
            "состава ноды отброшены",
            _where(node_id, report),
            members.dropped_lines,
            members.dropped_ips,
        )


def _alert(node_id: uuid.UUID, report: ReportIn, outcome: Outcome) -> None:
    """События приёма для алертов (§5.9: отклонение по времени и удержание «порождают алерт»;
    правила по журналу — 001.68): запись ERROR — отказ по времени, удержание с причинами и
    объёмами; WARNING — часть к закрытому интервалу (разрыв фиксирует сверка, 001.37) и повтор
    отказанного ключа (потерянный ответ, а не новое событие). Пишется после фиксации транзакции:
    откаченный приём алерта не даёт."""
    where = _where(node_id, report)
    members = outcome.members
    if outcome.duplicate:
        return
    if outcome.reason == "repeated" or outcome.status == "interval_closed":
        log.warning("отчёт: %s %s reason=%s", outcome.status, where, outcome.reason)
    elif outcome.status == "rejected_time":
        log.error(
            "отчёт: rejected_time %s reason=%s period=[%s, %s)",
            where,
            outcome.reason,
            report.period_start.isoformat(),
            report.period_end.isoformat(),
        )
    elif outcome.status == "held_anomaly" and members is not None and outcome.charged:
        uplink, downlink = members.volume
        log.error(
            "отчёт: held_anomaly %s reasons=%s uplink=%d downlink=%d charged=%d/%d ceiling=%d",
            where,
            ",".join(outcome.holds),
            uplink,
            downlink,
            outcome.charged.uplink,
            outcome.charged.downlink,
            outcome.ceiling,
        )


_INTERVAL_DURATIONS = (
    "select n.id as node_id, extract(epoch from r.period_end - r.period_start)::float8 as seconds "
    "from nodes n join lateral (select period_start, period_end from report_intervals r "
    "where r.node_id = n.id and r.first_received_at > now() - $1::interval) r on true"
)


async def interval_durations(
    conn: asyncpg.Connection, window: dt.timedelta = REPORT_INTERVAL_WINDOW
) -> dict[uuid.UUID, list[int]]:
    """Длительности интервалов, принятых за ``window``, по нодам: для каждой — число интервалов
    не длиннее каждой границы ``REPORT_INTERVAL_BUCKETS`` и последним элементом всех (накопительно,
    как корзины гистограммы). Штатный интервал — обязанность агента (§5.9: от него выведен Н-17),
    и Control Plane её наблюдает: алерт на систематическое превышение 60 с — 001.68. Выборка —
    по нодам через индекс ``(node_id, first_received_at)``: без ведущего ``node_id`` окно
    читало бы всю таблицу на каждом опросе метрик."""
    counts: dict[uuid.UUID, list[int]] = {}
    for row in await conn.fetch(_INTERVAL_DURATIONS, window):
        buckets = counts.setdefault(row["node_id"], [0] * (len(REPORT_INTERVAL_BUCKETS) + 1))
        for n, bound in enumerate(REPORT_INTERVAL_BUCKETS):
            if row["seconds"] <= bound:
                buckets[n] += 1
        buckets[-1] += 1
    return counts


class AccountingService:
    """Учёт трафика поверх пула asyncpg: приём отчёта — шаги 5, 6 и 9 UC-04 (001.34); шаги
    списания 4–7 §5.9 — 001.77.

    Граница транзакции: ``accept_report`` — операция верхнего уровня, она открывает транзакцию
    сама; шаги списания 001.77 лягут в неё же, на одно подключение: ``multiplier.resolve(conn,
    …)`` (один раз на отчёт — период принадлежит отчёту), ``QuotaService.on_report(conn, …)``,
    ``DeviceLimitService.evaluate(conn, …)`` и ``jobs.enqueue(conn, …)`` (outbox §5.4); в 001.34
    ни одна из них не вызывается.
    Доставка отчётов at-least-once (§5.9): всё, что списывается по отчёту, обязано лечь в ту же
    транзакцию, что и его ключ, — иначе повтор отчёта отвечает ``duplicate`` на половину
    сделанной работы. Поэтому службы, вызываемые из приёма, принимают подключение, а не пул,
    и приходят сюда из графа зависимостей (``deps.get_accounting_service``), а не создаются
    внутри: созданную внутри службу подмена в тесте не достигла бы (урок 001.28 для
    ``CommandService`` в ``CompositionService``)."""

    def __init__(
        self,
        pool: Any,
        quotas: QuotaService | None = None,
        device_limits: DeviceLimitService | None = None,
    ) -> None:
        self._pool = pool
        self._quotas = quotas or QuotaService(pool)
        self._device_limits = device_limits or DeviceLimitService(pool)

    async def accept_report(self, node: Node, report: ReportIn, fingerprint: str) -> Accept:
        """Принять часть отчёта ноды (UC-04 шаги 5, 6, 9; §5.9; правила частей — README
        каталога контрактов). ``fingerprint`` — отпечаток сертификата, которым запрос
        представился: identity перепроверяется под блокировкой строки ноды.

        Исходы: 200 с точкой продолжения — принято или удержано до разбора (``held_anomaly``: порог
        §5.9, потолок смен эпохи — доля в пределах порога прибавлена к суммам интервала; потолок
        открытых интервалов — интервал рождается закрытым, без списания); 200 с ``duplicate`` —
        повтор принятого ключа (Н-20); 409 ``rejected_time`` / ``interval_closed`` — отказ записан
        со статусом, точка продолжения не сдвинута; 422 ``report_inconsistent`` — часть противоречит
        принятым частям, ключ не записан; 401/403 — identity или вентиль, перепроверенные под
        блокировкой. Строки и адреса пользователей вне состава ноды отбрасываются. События для
        алертов (правила — 001.68) — записи журнала: ERROR — отказ по времени и удержание (после
        фиксации), 422 и отброшенные строки (при любом исходе части, кроме повтора ключа);
        WARNING — часть к закрытому интервалу и повтор отказанного ключа.

        Столбец «Учитывается трафик» §4.6: отчёт принимается в любом подтверждённом статусе —
        в ``provisioning``, чтобы нода освободила буфер (не тарифицирует его шаг списания
        001.77 по статусу, прочитанному под той же блокировкой), в ``offline``, ``disabled``,
        ``suspended`` — из буфера."""
        own_node(node, report)
        acceptance = _Acceptance(node.id, fingerprint, report)
        try:
            async with transaction(self._pool) as conn:
                outcome = await acceptance.run(conn)
        except ApiError as exc:
            _alert_foreign(node.id, report, acceptance.members)
            if exc.code == "report_inconsistent":
                log.error(
                    "отчёт: report_inconsistent node=%s epoch=%s seq=%s reason=%s",
                    node.id,
                    report.counter_epoch,
                    report.report_seq,
                    exc.details["reason"],
                )
            raise
        _alert_foreign(node.id, report, acceptance.members)
        _alert(node.id, report, outcome)
        if outcome.status == "rejected_time" or outcome.status == "interval_closed":
            raise refused(outcome.status, outcome.reason, outcome.last_accepted_seq)
        return Accept(last_accepted_seq=outcome.last_accepted_seq, duplicate=outcome.duplicate)

    async def close_hour(
        self, conn: asyncpg.Connection, node_id: uuid.UUID, at: dt.datetime
    ) -> None:
        """Закрыть накопление часа ноды на момент ``at`` (§4.9 «Датированность»: перенос ноды в
        другую тарифицируемую группу или смена переопределения закрывает период принудительно,
        новые записи идут в строку ``traffic_hourly`` с новым ключом коэффициента). Принимает
        подключение вызывающего: закрытие часа атомарно со сменой коэффициента (001.19 —
        каталог), реализация — 001.77; заглушка ничего не делает."""
