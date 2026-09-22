"""Коэффициент трафика: границы и перевод между представлениями (задача 001.19; §4.9, UC-09 A2;
R-23). Диапазон и шаг стоят ``CHECK`` на четырёх колонках §4.2.2 и §4.2.5, но до базы значение
доходит из запроса администратора, и ответом обязано быть 422 с причиной, а не 500 от нарушения
``CHECK``: проверка здесь — это тот же предел, снятый на слой раньше.

TC-UNIT-01: ``10001``, ``150``, ``2000`` → ошибка, ошибка, успех.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.domain.groups import from_milli, to_milli, validate_multiplier
from app.domain.multiplier import MULTIPLIER_MILLI_MAX, MULTIPLIER_STEP_MILLI
from app.errors import ApiError


def test_tc_unit_01_multiplier_bounds() -> None:
    """TC-UNIT-01 описания задачи, дословно: вне диапазона, не кратно шагу, верное значение."""
    with pytest.raises(ApiError) as out_of_range:
        validate_multiplier(10001)
    assert out_of_range.value.status == 422
    with pytest.raises(ApiError) as off_step:
        validate_multiplier(150)
    assert off_step.value.status == 422
    assert validate_multiplier(2000) == 2000


@pytest.mark.parametrize("milli", [0, 100, 1000, 9900, 10000])
def test_every_value_the_column_accepts_passes(milli: int) -> None:
    """Проверка не должна быть строже колонки: всё, что принимает ``CHECK`` §4.2.2, обязано
    проходить — иначе панель не сможет задать значение, которое база хранит."""
    assert validate_multiplier(milli) == milli


@pytest.mark.parametrize("milli", [-100, -1, 1, 99, 101, 10001, 10100, 20000])
def test_every_value_the_column_rejects_is_refused(milli: int) -> None:
    """И не мягче: всё, что колонка отвергнет, обязано отвергаться здесь — иначе отказ приедет
    500-м от ``CHECK`` вместо 422 с причиной. Границы взяты литералами, а не из констант кода:
    страж, выведенный из проверяемого, поехал бы вместе с ним."""
    with pytest.raises(ApiError) as refused:
        validate_multiplier(milli)
    assert refused.value.status == 422
    assert refused.value.code == "invalid_multiplier"


def test_the_bounds_are_the_ones_the_column_declares() -> None:
    """Литералы стража и константы кода — об одном и том же пределе (§4.2.2: 0…10000, шаг 100)."""
    assert (MULTIPLIER_MILLI_MAX, MULTIPLIER_STEP_MILLI) == (10000, 100)


@pytest.mark.parametrize(
    ("decimal", "milli"),
    [("0", 0), ("0.1", 100), ("1", 1000), ("2.0", 2000), ("9.9", 9900), ("10", 10000)],
)
def test_decimal_and_milli_are_the_same_number(decimal: str, milli: int) -> None:
    """Перевод в обе стороны без потерь: панель присылает десятичную дробь, база хранит
    тысячные (§4.2.2), и §4.9 запрещает плавающую точку — значит перевод точный."""
    assert to_milli(Decimal(decimal)) == milli
    assert from_milli(milli) == Decimal(decimal)


@pytest.mark.parametrize("decimal", ["0.05", "2.05", "0.15", "1.001", "1.0001", "2.00005"])
def test_a_finer_step_than_a_tenth_is_refused_before_the_database(decimal: str) -> None:
    """Дробь мельче 0.1 не округляется молча: 0.05 ушло бы в базу нулём или сотней, и списание
    разошлось бы с тем, что задал администратор. ``1.0001`` — отдельный случай: в тысячных это
    1000.1, и отбрасывание дробной части дало бы **допустимые** 1000, то есть 1.0 вместо
    заданного значения; проверку кратности такой вход проходит, ловит его только требование
    целого."""
    with pytest.raises(ApiError) as refused:
        to_milli(Decimal(decimal))
    assert refused.value.status == 422
