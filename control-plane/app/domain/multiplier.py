"""Разрешение коэффициента трафика по дате и целочисленное списание (постановка §4.9;
data-model.md §4.2.2 ``billing_group_multipliers``, ``node_billing_assignments``,
``multiplier_override_milli``; UC-04 шаг 7, A7; R-23).

Сигнатуры объявила 001.33, логику ввела 001.23 (``tdd-strict``). Разрешение «нода →
тарифицируемая группа → 1.0» идёт на момент ``period_start`` отчёта, а не на момент приёма:
буфер досылает отчёты возрастом до 24 часов, а ретроактивное изменение списаний запрещено.
Значение, уровень и группу оно берёт только из датированной истории
(``node_billing_assignments``, ``billing_group_multipliers``); таблицу ``nodes`` не читает
вовсе — её ``billing_group_id`` и ``multiplier_milli`` хранят действующее назначение, а не
назначение момента. Интервал истории — ``[valid_from, valid_to)``, ``valid_to`` NULL означает
«действует». Выборка ``tstzrange(valid_from, valid_to) @> момент`` повторяет выражение
``EXCLUDE``-индексов обеих таблиц, а само ограничение гарантирует не больше одного интервала
ноды и одного интервала группы на момент.

Группа результата — группа назначения на ``period_start``, а не на момент приёма. Так
прочитана строка §5.9 «тарифицируемая группа на момент создания записи»: data-model §4.2.2
разрешает назначение по ``period_start``, и группа обязана быть той, чей коэффициент применён, —
``traffic_hourly`` держит пару ``(billing_group_id, multiplier_milli)`` в одном ключе, и
коэффициент одной группы под именем другой был бы бессмыслицей.

Момент до ввода ноды (раньше первого назначения) получает 1.0 из ``default`` и группу первого
назначения — ту, в которой нода вошла в эксплуатацию. Это единственное исключение из правила
«группа — та, чей коэффициент применён»: 1.0 здесь — не значение группы, а записанная пара
неотличима от «группа с 1.0» (уровень в записи не хранится). Поэтому приём до этой ветки
доводить не должен — якорь первого интервала 001.34 (передано). Разрыв истории после ввода —
нарушение целостности (data-model §4.2.2: интервалы ноды покрывают время от ввода без
пробелов), и он, как и нода без истории, — ``NoBillingAssignmentError``, а не молчаливое 1.0.

Смена коэффициента или назначения внутри ``[period_start, period_end)`` интервал не делит: весь
интервал получает значение своего начала (data-model §4.2.2). Штатный интервал агента — 60 с,
наибольший — час (``REPORT_MAX_PERIOD``), и на столько может запоздать новое значение.

Представление: целое число тысячных (``1000`` — 1.0), диапазон 0…10000 с шагом 100 (``CHECK``
таблиц §4.2.2 и §4.2.5) — ``Resolved`` держит это как инвариант; ``0`` — трафик ноды не
списывается, ``raw_bytes`` записывается.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Literal

import asyncpg

# Три уровня §4.9 «Модель и разрешение», по убыванию приоритета.
Source = Literal["node", "group", "default"]
MILLI_PER_UNIT = 1000  # хранение в тысячных: 1000 — 1.0; панель показывает дробь (groups.py)
DEFAULT_MULTIPLIER_MILLI = 1000  # 1.0 — системное значение по умолчанию (§4.9)
MULTIPLIER_MILLI_MAX = 10000  # 10.0
MULTIPLIER_STEP_MILLI = 100  # шаг 0.1


@dataclass(frozen=True, slots=True)
class Resolved:
    """Коэффициент, действовавший на момент запроса: значение в тысячных, тарифицируемая группа
    назначения на тот момент (обе величины фиксируются в записи статистики; прочтение §5.9 и
    исключение для момента до ввода — в докстринге модуля) и уровень, с которого значение взято.
    Значение не целое, вне диапазона или не кратное шагу — ошибка программы, а не данные:
    ``CHECK`` таблиц его всё равно не примет, а ``float`` протащил бы плавающую точку в
    списание."""

    multiplier_milli: int
    billing_group_id: uuid.UUID
    source: Source

    def __post_init__(self) -> None:
        if type(self.multiplier_milli) is not int:
            raise TypeError("коэффициент — целое число тысячных")
        if not 0 <= self.multiplier_milli <= MULTIPLIER_MILLI_MAX:
            raise ValueError(f"коэффициент вне диапазона 0…{MULTIPLIER_MILLI_MAX} тысячных")
        if self.multiplier_milli % MULTIPLIER_STEP_MILLI:
            raise ValueError(f"коэффициент не кратен шагу {MULTIPLIER_STEP_MILLI} тысячных")


class NoBillingAssignmentError(LookupError):
    """Назначения на момент нет и момент не раньше ввода: нода неизвестна, у неё нет истории
    или в истории разрыв. Каждый случай — ошибка программы или нарушение целостности, и
    списание по 1.0 на нём было бы молчаливой ошибкой. Свой класс, а не голый ``LookupError``:
    ``except LookupError`` вызывающего проглотил бы и чужие ``KeyError``/``IndexError``."""


# Назначение на момент и коэффициент его группы на тот же момент — одна строка или ни одной.
_COVERING = (
    "select a.billing_group_id, a.multiplier_override_milli, m.multiplier_milli "
    "from node_billing_assignments a "
    "left join billing_group_multipliers m on m.billing_group_id = a.billing_group_id "
    "and tstzrange(m.valid_from, m.valid_to) @> $2::timestamptz "
    "where a.node_id = $1 and tstzrange(a.valid_from, a.valid_to) @> $2::timestamptz"
)
# Первое назначение ноды — только когда покрывающего нет: до ввода или нарушение целостности.
_FIRST = (
    "select billing_group_id, valid_from from node_billing_assignments "
    "where node_id = $1 order by valid_from limit 1"
)


async def resolve(conn: asyncpg.Connection, node_id: uuid.UUID, at: dt.datetime) -> Resolved:
    """Коэффициент ноды на момент ``at``: переопределение назначения, действовавшего в этот
    момент, иначе коэффициент группы этого назначения на тот же момент, иначе 1.0 (``source =
    default``: у группы без коэффициента и до ввода ноды). ``0`` — значение уровня, а не его
    отсутствие. Вызывается один раз на отчёт: границы периода принадлежат отчёту, а не строке,
    и все строки получают одно значение; кэш не нужен.

    ``at`` — с часовым поясом (``ValueError`` иначе): наивный момент asyncpg толкует в поясе
    процесса, и ответ зависел бы от окружения. Назначения на ``at`` нет, а ``at`` не раньше
    первого назначения (или назначений нет вовсе) — ``NoBillingAssignmentError``."""
    if at.utcoffset() is None:
        raise ValueError("момент разрешения коэффициента — с часовым поясом")
    row = await conn.fetchrow(_COVERING, node_id, at)
    if row is not None:
        if row["multiplier_override_milli"] is not None:
            return Resolved(row["multiplier_override_milli"], row["billing_group_id"], "node")
        if row["multiplier_milli"] is not None:
            return Resolved(row["multiplier_milli"], row["billing_group_id"], "group")
        return Resolved(DEFAULT_MULTIPLIER_MILLI, row["billing_group_id"], "default")
    first = await conn.fetchrow(_FIRST, node_id)
    if first is None:
        raise NoBillingAssignmentError(f"у ноды {node_id} нет истории назначений")
    if at < first["valid_from"]:
        return Resolved(DEFAULT_MULTIPLIER_MILLI, first["billing_group_id"], "default")
    raise NoBillingAssignmentError(f"разрыв истории назначений ноды {node_id} на {at.isoformat()}")


def billable(raw: int, milli: int) -> int:
    """Списываемый объём ``floor(raw × milli / 1000)`` в целых (§4.9: без чисел с плавающей
    точкой). ``//`` над целыми Python и есть ``floor``, переполнения нет: функция считает
    точно при любом ``raw``. Сам результат ширину колонки ``bigint`` превысить может — ``raw``
    строки это ``uplink + downlink`` (§5.9 «База списания»), до двух ``INT8_MAX``, и коэффициент
    до 10.0 — поэтому границу строки обязан держать приём (передано в 001.34 и 001.77)."""
    return raw * milli // MILLI_PER_UNIT
