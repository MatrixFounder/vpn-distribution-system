"""Таблица случаев разрешения коэффициента и целочисленного списания (задача 001.23, tdd-strict;
постановка §4.9; data-model.md §4.2.2; UC-04 шаг 7, A7; R-23).

``billable`` — чистая функция, её случаи без базы. ``resolve`` — выборка по истории назначений
ноды и коэффициентов группы, поэтому её таблица идёт против базы стенда: подменённое подключение
проверило бы подмену, а не границы интервалов ``[valid_from, valid_to)``. История задаётся
литералами моментов в прошлом (март 2026) прямыми вставками: писатель истории (``GroupService``)
пишет только моментом ``now()`` базы, а таблице нужен день с известными границами. Выборка по
``now()`` вместо ``at`` дала бы на каждой строке до 18:00 действующие сейчас группу и
коэффициент, а не записанные в строке.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import asyncpg
import pytest
from app.domain import multiplier
from app.domain.multiplier import NoBillingAssignmentError, Resolved, billable

from tests.e2e._catalog import billing_group, catalog, node

DAY = dt.datetime(2026, 3, 2, tzinfo=dt.UTC)


def at(clock: str, microsecond: int = 0) -> dt.datetime:
    """Момент дня истории по «ЧЧ:ММ» (и микросекундам) в UTC."""
    hours, minutes = map(int, clock.split(":"))
    return DAY + dt.timedelta(hours=hours, minutes=minutes, microseconds=microsecond)


# Целые литералы, без вывода из констант модуля: таблица, выведенная из проверяемого кода,
# поехала бы вместе с ним (developer-guidelines §6.3 п. 6).
BILLABLE_CASES = [
    pytest.param(1_000_000_001, 1500, 1_500_000_001, id="tc-unit-03-floor-not-round"),
    pytest.param(100, 2300, 230, id="float-would-give-229"),
    pytest.param(2**53 + 1, 1000, 2**53 + 1, id="float-would-lose-the-last-unit"),
    pytest.param(3, 100, 0, id="floor-below-one-byte"),
    pytest.param(19, 1100, 20, id="floor-of-20.9"),
    pytest.param(1_073_741_824, 0, 0, id="zero-multiplier-bills-nothing"),
    pytest.param(1_073_741_824, 1000, 1_073_741_824, id="default-is-identity"),
    pytest.param(1_073_741_824, 10000, 10_737_418_240, id="ceiling-ten"),
    # raw строки — uplink + downlink (§5.9), до двух INT8_MAX; × 10.0 — шире bigint. Функция
    # считает точно, границу строки держит приём (001.34/001.77).
    pytest.param(
        18_446_744_073_709_551_614,
        10000,
        184_467_440_737_095_516_140,
        id="two-int8-max-times-ten",
    ),
    pytest.param(0, 3000, 0, id="no-traffic"),
]


@pytest.mark.parametrize(("raw", "milli", "expected"), BILLABLE_CASES)
def test_billable_is_integer_floor(raw: int, milli: int, expected: int) -> None:
    """TC-UNIT-03 и критерий «без чисел с плавающей точкой»: ``floor(raw × milli / 1000)`` в
    целых. Случай TC-UNIT-03 сам по себе float не ловит (1 500 000 001.5 представимо точно),
    поэтому в таблице есть входы, на которых float ошибается: ``100 × 2.3`` во float — 229,
    ``2⁵³ + 1`` во float теряет младшую единицу. Строки ``default-is-identity`` и
    ``no-traffic`` — документация границ, а не стражи: их не валит ни одна из посадок."""
    # EXPECTED_FAIL_REASON: AssertionError — заглушка 001.33 возвращает raw без коэффициента
    # (1 000 000 001 вместо 1 500 000 001 и т. д.); строки с milli = 1000 и raw = 0 зелёные.
    result = billable(raw, milli)
    assert type(result) is int
    assert result == expected


def test_multiplier_constants_and_resolved_invariant() -> None:
    """Представление §4.9: тысячные, 1.0 по умолчанию, 0…10.0 с шагом 0.1 — константы сверены с
    литералами (``MILLI_PER_UNIT`` делят списание и перевод дроби панели в ``groups.py``);
    ``Resolved`` держит тип, диапазон и шаг инвариантом."""
    assert (
        multiplier.MILLI_PER_UNIT,
        multiplier.DEFAULT_MULTIPLIER_MILLI,
        multiplier.MULTIPLIER_MILLI_MAX,
        multiplier.MULTIPLIER_STEP_MILLI,
    ) == (1000, 1000, 10000, 100)
    group = uuid.UUID("00000000-0000-7000-8000-0000000000f1")
    for bad in (-100, 10100, 1050):  # вне диапазона, вне диапазона, не кратно шагу
        with pytest.raises(ValueError, match="коэффициент"):
            Resolved(bad, group, "group")
    for not_int in (2000.0, True):  # float протащил бы плавающую точку в billable; bool — не int
        with pytest.raises(TypeError, match="коэффициент"):
            Resolved(not_int, group, "group")  # type: ignore[arg-type]
    assert Resolved(0, group, "node").multiplier_milli == 0
    assert Resolved(10000, group, "node").multiplier_milli == 10000


@dataclass(frozen=True)
class History:
    """Нода с историей назначений и три группы; ``current`` — действующая сейчас группа ноды."""

    conn: asyncpg.Connection
    node_id: uuid.UUID
    asia: uuid.UUID
    empty: uuid.UUID
    current: uuid.UUID


@pytest.fixture
async def history(pg_dsn: str) -> AsyncIterator[History]:
    """История одного дня (UTC), интервалы ``[с, до)``:

    группа asia: 09:00–12:00:00.000500 → 2.0; 12:00:00.000500–13:00 → 0.0; с 13:00 → 2.0
    группа empty: коэффициента нет никогда
    группа current: с 17:30 → 5.0

    нода (ввод в 10:00): 10:00–14:00 asia; 14:00–15:00 asia, переопределение 3.0;
    15:00–16:00 asia, переопределение 0.0; 16:00–17:00 empty; 17:00–18:00 current;
    с 18:00 current, переопределение 7.0

    соседняя нода: с 08:00 empty, переопределение 4.0

    Граница коэффициента лежит внутри секунды: усечение момента до секунды или миллисекунды
    перенесло бы 12:00:00.000500 в прежний интервал. Денормализация ``nodes`` — как её оставил
    бы писатель (``set_billing_group``): current и 7.0. Подстановка её вместо истории ошибается
    на каждой строке с 10:00 до 17:30, запасным значением — на тех из них, где у уровня нет
    своего значения. Соседняя нода покрывает 09:30, когда у ноды назначения ещё нет: выборка
    без условия по ноде отдала бы на этой строке её 4.0. Назначения ноды вставлены в обратном
    порядке: «первое назначение» без сортировки по ``valid_from`` оказалось бы последним."""
    async with catalog(pg_dsn) as conn:
        asia = await billing_group(conn, "asia")
        empty = await billing_group(conn, "empty")
        current = await billing_group(conn, "current")
        node_id = await node(conn, current, 71)
        await conn.execute("update nodes set multiplier_milli = 7000 where id = $1", node_id)
        neighbour = await node(conn, empty, 74)
        await conn.executemany(
            "insert into billing_group_multipliers "
            "(billing_group_id, multiplier_milli, valid_from, valid_to) values ($1, $2, $3, $4)",
            [
                (asia, 2000, at("09:00"), at("12:00", 500)),
                (asia, 0, at("12:00", 500), at("13:00")),
                (asia, 2000, at("13:00"), None),
                (current, 5000, at("17:30"), None),
            ],
        )
        await conn.executemany(
            "insert into node_billing_assignments "
            "(node_id, billing_group_id, multiplier_override_milli, valid_from, valid_to) "
            "values ($1, $2, $3, $4, $5)",
            [
                (node_id, current, 7000, at("18:00"), None),
                (node_id, current, None, at("17:00"), at("18:00")),
                (node_id, empty, None, at("16:00"), at("17:00")),
                (node_id, asia, 0, at("15:00"), at("16:00")),
                (node_id, asia, 3000, at("14:00"), at("15:00")),
                (node_id, asia, None, at("10:00"), at("14:00")),
                (neighbour, empty, 4000, at("08:00"), None),
            ],
        )
        yield History(conn, node_id, asia, empty, current)


# (момент, коэффициент, группа истории, уровень)
RESOLVE_CASES = [
    pytest.param(at("09:30"), 1000, "asia", "default", id="tc-unit-02-before-commissioning"),
    pytest.param(at("10:00"), 2000, "asia", "group", id="commissioning-moment-included"),
    pytest.param(at("12:00", 499), 2000, "asia", "group", id="last-microsecond-before"),
    pytest.param(at("12:00", 500), 0, "asia", "group", id="sub-second-change-zero"),
    pytest.param(at("13:00"), 2000, "asia", "group", id="group-value-again"),
    pytest.param(at("14:00"), 3000, "asia", "node", id="tc-unit-01-override-above-group"),
    pytest.param(at("15:00"), 0, "asia", "node", id="override-zero-is-a-value"),
    pytest.param(at("16:00"), 1000, "empty", "default", id="group-without-multiplier"),
    pytest.param(at("17:00"), 1000, "current", "default", id="moved-before-group-value"),
    pytest.param(at("17:30"), 5000, "current", "group", id="moved-group-value"),
    pytest.param(at("18:00"), 7000, "current", "node", id="current-override"),
]


@pytest.mark.parametrize(("moment", "milli", "group", "source"), RESOLVE_CASES)
async def test_resolve_by_moment(
    history: History, moment: dt.datetime, milli: int, group: str, source: multiplier.Source
) -> None:
    """TC-UNIT-01 (переопределение выше группы), TC-UNIT-02 (до ввода ноды — 1.0 из
    ``default`` и группа первого назначения) и границы интервалов: начало включено, конец нет,
    с точностью до микросекунды; ``0`` — значение, а не его отсутствие, на обоих уровнях;
    группа — та, что была у ноды в момент, а не действующая."""
    # EXPECTED_FAIL_REASON: AssertionError — заглушка возвращает Resolved(1000, STUB_BILLING_
    # GROUP_ID, "default") без чтения базы: группа не совпадает ни в одной строке таблицы.
    resolved = await multiplier.resolve(history.conn, history.node_id, moment)
    assert resolved == Resolved(milli, getattr(history, group), source)


class _NoOffset(dt.tzinfo):
    """Пояс без смещения — ``utcoffset()`` отвечает ``None``."""

    def utcoffset(self, moment: dt.datetime | None) -> dt.timedelta | None:
        return None

    def dst(self, moment: dt.datetime | None) -> dt.timedelta | None:
        return None

    def tzname(self, moment: dt.datetime | None) -> str | None:
        return None


async def test_resolve_takes_an_aware_moment_in_any_zone(history: History) -> None:
    """Тот же миг в поясе +03:00 разрешается так же, как в UTC; наивный момент — ``ValueError``:
    asyncpg толковал бы его в поясе процесса, и ответ зависел бы от окружения."""
    moscow = at("12:00", 500).astimezone(dt.timezone(dt.timedelta(hours=3)))
    assert await multiplier.resolve(history.conn, history.node_id, moscow) == Resolved(
        0, history.asia, "group"
    )
    with pytest.raises(ValueError, match="часовым поясом"):
        await multiplier.resolve(history.conn, history.node_id, at("12:00").replace(tzinfo=None))
    # tzinfo есть, а смещения нет: для Python такой момент наивный (признак — ``utcoffset()``).
    with pytest.raises(ValueError, match="часовым поясом"):
        await multiplier.resolve(
            history.conn, history.node_id, at("12:00").replace(tzinfo=_NoOffset())
        )


async def test_resolve_without_an_assignment_is_an_error(pg_dsn: str) -> None:
    """Назначения на момент нет, а момент не раньше ввода, — ``NoBillingAssignmentError``, не 1.0:
    разрыв истории после ввода (data-model §4.2.2: интервалы без пробелов), нода без истории и
    неизвестная нода. Приём разрешает коэффициент для аутентифицированной ноды, и молчаливое
    значение по умолчанию списало бы трафик не по её коэффициенту. До первого назначения той же
    ноды — 1.0 и его группа. Ноду без истории и неизвестную спрашивают в 12:00, когда чужая
    история (ноды с разрывом) момент покрывает: выборка без условия по ноде вернула бы её
    строку. Назначения вставлены в обратном порядке — «первое» держит только сортировка."""
    # EXPECTED_FAIL_REASON: Failed: DID NOT RAISE — заглушка отвечает 1.0 для любой ноды.
    async with catalog(pg_dsn) as conn:
        asia = await billing_group(conn, "asia")
        gappy = await node(conn, asia, 72)
        bare = await node(conn, asia, 73)
        await conn.executemany(
            "insert into node_billing_assignments "
            "(node_id, billing_group_id, multiplier_override_milli, valid_from, valid_to) "
            "values ($1, $2, $3, $4, $5)",
            [
                (gappy, asia, 9000, at("12:00"), None),
                (gappy, asia, 9000, at("10:00"), at("11:00")),
            ],
        )
        assert await multiplier.resolve(conn, gappy, at("09:00")) == Resolved(1000, asia, "default")
        assert (await multiplier.resolve(conn, gappy, at("12:00"))).multiplier_milli == 9000
        with pytest.raises(NoBillingAssignmentError, match="разрыв"):
            await multiplier.resolve(conn, gappy, at("11:30"))
        with pytest.raises(NoBillingAssignmentError, match="нет истории"):
            await multiplier.resolve(conn, bare, at("12:00"))
        unknown = uuid.UUID("00000000-0000-7000-8000-00000000dead")
        with pytest.raises(NoBillingAssignmentError, match="нет истории"):
            await multiplier.resolve(conn, unknown, at("12:00"))
