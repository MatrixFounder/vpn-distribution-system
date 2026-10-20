"""Чистые правила приёма отчёта (задача 001.34, ``tdd-strict``; постановка §5.9 «Защита от
аномального отчёта»; контракт ``contracts/agent_v1/README.md`` «Отчёт о трафике»): порог прироста
по полосе и длительности, доля удержанной части в пределах порога, момент и причина закрытия
интервала по времени, граница полосы ноды. Порядок проверок и запись — сквозные тесты
``tests/e2e/test_reports.py`` на живой базе."""

from __future__ import annotations

import datetime as dt

import pytest
from app.accounting.service import (
    REPORT_ACCEPT_WINDOW,
    REPORT_BUFFER_DEPTH,
    REPORT_EPOCH_WINDOW,
    REPORT_INTERVAL_BUCKETS,
    REPORT_INTERVAL_WINDOW,
    REPORT_MAX_INTERVAL_AGE,
    REPORT_MAX_PERIOD,
    REPORT_PARTS_DEADLINE,
    TIME_TOLERANCE,
    Charge,
    anomaly_threshold,
    charge,
    closing,
)
from app.domain.multiplier import MULTIPLIER_MILLI_MAX, billable
from app.domain.nodes import BANDWIDTH_MBPS_MAX, NodeIn, NodePatch
from app.domain.statuses import INT8_MAX
from pydantic import ValidationError

AT = dt.datetime(2026, 10, 19, 12, tzinfo=dt.UTC)


def test_tc_unit_01_the_threshold_is_bandwidth_times_duration() -> None:
    """TC-UNIT-01: 100 Мбит/с за 60 с — 750 МБ (10⁶ байт) на сумму uplink + downlink:
    100·10⁶ бит/с × 60 с / 8. Литералами — порог не выводится из самого себя."""
    assert anomaly_threshold(100, dt.timedelta(seconds=60)) == 750_000_000


@pytest.mark.parametrize(
    ("mbps", "duration", "expected"),
    [
        (1, dt.timedelta(seconds=60), 7_500_000),
        (1000, dt.timedelta(seconds=60), 7_500_000_000),
        (100, dt.timedelta(hours=1), 45_000_000_000),
        (8, dt.timedelta(microseconds=1), 1),  # 8 бит за микросекунду — один байт
        (1, dt.timedelta(microseconds=7), 0),  # дробь байта отбрасывается: порог — целое
        (100, dt.timedelta(seconds=60, microseconds=1), 750_000_012),
    ],
)
def test_the_threshold_counts_microseconds_and_floors(
    mbps: int, duration: dt.timedelta, expected: int
) -> None:
    """Порог — целое число байт от длительности с точностью меток (микросекунда), вниз: дробная
    часть байта физически недостижима."""
    assert anomaly_threshold(mbps, duration) == expected


def test_a_part_within_the_threshold_is_charged_in_full() -> None:
    """Сумма uplink + downlink с прежними частями не больше порога — часть принимается
    целиком; ровно порог — тоже приём (порог — достижимый максимум)."""
    assert charge(100, (0, 0), (40, 60)) == Charge(40, 60, held=False)
    assert charge(100, (30, 20), (30, 20)) == Charge(30, 20, held=False)
    assert charge(100, (0, 0), (0, 0)) == Charge(0, 0, held=False)


def test_a_part_over_the_threshold_is_held_and_charged_up_to_it() -> None:
    """Порог — на сумму направлений (байт пользователя проходит порт дважды; решение заказчика
    2026-10-20): по отдельности каждое направление в пределе, а вместе — сверх, и это удержание.
    К сумме интервала и списанию идёт только остаток до порога, разложенный пропорционально
    части: uplink вниз, downlink — дополнение; сумма равна остатку, и ни одно направление не
    получает больше присланного."""
    assert charge(100, (0, 0), (60, 60)) == Charge(50, 50, held=True)
    assert charge(100, (0, 0), (101, 5)) == Charge(95, 5, held=True)
    assert charge(100, (40, 10), (30, 90)) == Charge(12, 38, held=True)
    assert charge(3, (0, 0), (1, 3)) == Charge(0, 3, held=True)
    assert charge(100, (0, 0), (0, 101)) == Charge(0, 100, held=True)
    assert charge(100, (90, 10), (30, 95)) == Charge(0, 0, held=True)
    assert charge(100, (100, 0), (1, 0)) == Charge(0, 0, held=True)


def test_a_sum_already_over_the_threshold_charges_nothing_more() -> None:
    """Сумма интервала порог не превышает (к ней прибавляется доля в пределах порога), но после
    уменьшения полосы ноды администратором порог может оказаться ниже уже принятого — остаток
    тогда ноль, а не отрицательное списание."""
    assert charge(100, (150, 0), (10, 0)) == Charge(0, 0, held=True)


def test_an_open_interval_closes_at_its_deadline_or_its_age_whichever_comes_first() -> None:
    """Закрытие по времени — момент истечения, а не приёма: срок ``REPORT_PARTS_DEADLINE`` от
    последней принятой части или потолок ``REPORT_MAX_INTERVAL_AGE`` от первой — что раньше;
    при равенстве — возраст (жёсткий потолок)."""
    assert (REPORT_PARTS_DEADLINE, REPORT_MAX_INTERVAL_AGE) == (
        dt.timedelta(hours=25),
        dt.timedelta(hours=48),
    )
    assert closing(AT, AT) == (AT + dt.timedelta(hours=25), "deadline")
    assert closing(AT, AT + dt.timedelta(hours=30)) == (AT + dt.timedelta(hours=48), "age")
    assert closing(AT, AT + dt.timedelta(hours=23)) == (AT + dt.timedelta(hours=48), "age")
    assert closing(AT, AT + dt.timedelta(hours=22, minutes=59)) == (
        AT + dt.timedelta(hours=47, minutes=59),
        "deadline",
    )


def test_the_bandwidth_bound_keeps_a_node_year_inside_bigint() -> None:
    """Граница полосы — литералом (100 Гбит/с) и по свойству: при наибольшем коэффициенте
    10.0 и полном канале год **одной ноды** по порогу §5.9 (на сумму uplink + downlink)
    укладывается в ``bigint`` (строки, часа, суток и периода этой ноды; сумма пользователя по
    многим нодам за период — забота 001.77, передано из 001.23)."""
    assert BANDWIDTH_MBPS_MAX == 100_000
    year = anomaly_threshold(BANDWIDTH_MBPS_MAX, dt.timedelta(days=365))
    assert year == 394_200_000_000_000_000
    assert billable(year, MULTIPLIER_MILLI_MAX) <= INT8_MAX
    assert (
        billable(
            anomaly_threshold(10 * BANDWIDTH_MBPS_MAX, dt.timedelta(days=365)),
            MULTIPLIER_MILLI_MAX,
        )
        > INT8_MAX
    ), "граница не с запасом на порядок: в десять раз шире — уже за bigint"
    assert anomaly_threshold(BANDWIDTH_MBPS_MAX, REPORT_MAX_PERIOD) == 45_000_000_000_000


def test_the_node_record_bounds_the_bandwidth() -> None:
    """Запись ноды держит ту же границу, что ``CHECK`` таблицы (миграция 140): 100 000 —
    принято, 100 001 — 422 модели, а не ошибка вставки; и при создании, и при частичном
    изменении (роаст 001.34, раунд 3, m5)."""
    base = {
        "code": "JP-01",
        "name": "n",
        "country": "JP",
        "city": "Tokyo",
        "provider": "p",
        "public_ipv4": "203.0.113.1",
        "billing_group_id": "00000000-0000-7000-8000-000000000001",
        "max_conn_per_ip": 8,
    }
    assert NodeIn.model_validate({**base, "bandwidth_mbps": 100_000}).bandwidth_mbps == 100_000
    with pytest.raises(ValidationError):
        NodeIn.model_validate({**base, "bandwidth_mbps": 100_001})
    assert NodePatch.model_validate({"bandwidth_mbps": 100_000}).bandwidth_mbps == 100_000
    with pytest.raises(ValidationError):
        NodePatch.model_validate({"bandwidth_mbps": 100_001})


def test_the_clock_tolerance_is_five_seconds() -> None:
    """§5.9: допустимое отклонение часов ноды — 5 с."""
    assert dt.timedelta(seconds=5) == TIME_TOLERANCE


def test_the_windows_of_acceptance_are_literal() -> None:
    """Окна приёма — литералами (сквозные тесты берут величины требований, а не константы):
    глубина буфера Н-8 — сутки; окно приёма нового интервала — сутки буфера плюс потолок
    возраста открытого интервала, 72 ч; смены эпохи считаются за скользящие сутки; метрика
    длительности — за последний час, корзины 60/120/300/900/3600 с. Потолки открытых интервалов
    и смен эпохи закреплены в ``test_report_schema.py`` (001.33)."""
    assert dt.timedelta(hours=24) == REPORT_BUFFER_DEPTH
    assert dt.timedelta(hours=72) == REPORT_ACCEPT_WINDOW
    assert dt.timedelta(hours=24) == REPORT_EPOCH_WINDOW
    assert dt.timedelta(hours=1) == REPORT_INTERVAL_WINDOW
    assert REPORT_INTERVAL_BUCKETS == (60, 120, 300, 900, 3600)
