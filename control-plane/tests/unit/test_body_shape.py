"""Пределы формы тела, выведенные из модели (`app/body_shape.py`, задача 001.33, раунд 8 и
закрытие): для каждой модели тела Node API — литералы, для отчёта — равенство с ручной формулой и
со счётчиками самого большого тела; строки — доказанный алфавит или бюджет свободного текста;
числа — предел серии цифр у модели без строк; модель без верхней границы формы — ошибка, а не
тихий пропуск."""

from __future__ import annotations

from typing import Annotated

import pytest
from app.accounting.quota import QuotaRequestIn
from app.accounting.service import (
    BODY_MAX_COMMAS,
    BODY_MAX_OPENERS,
    REPORT_MAX_LINES,
    REPORT_MAX_ONLINE_IPS,
    OnlineIp,
    ReportIn,
    ReportLine,
)
from app.agent_api.enroll import EnrollIn
from app.body_shape import STRUCTURAL, ShapeLimits, _pattern_alphabet_excludes, shape_limits
from app.domain.commands import CommandResultIn
from app.domain.composition import AckIn
from app.domain.statuses import HeartbeatIn, NodeMetrics
from pydantic import BaseModel, Field


def test_every_body_model_of_the_section_has_literal_limits() -> None:
    """Плоская модель — одна скобка и (полей − 1) запятых: heartbeat из пяти полей отвергает
    шестую запятую до разбора. Свободный текст результата команды (`error`, 2 000 символов без
    шаблона) прибавляет свой предел к обоим пределам; модели без строковых полей несут предел
    серии цифр `bigint` (19 знаков), модели со строками (версии, токен, PEM, текст ошибки) —
    нет. Литералы — чтобы новое поле меняло предел осознанно."""
    assert shape_limits(HeartbeatIn) == ShapeLimits(1, 4, None)
    assert shape_limits(NodeMetrics) == ShapeLimits(1, 7, 19)
    assert shape_limits(AckIn) == ShapeLimits(1, 1, 19)
    assert shape_limits(CommandResultIn) == ShapeLimits(1 + 2_000, 1 + 2_000, None)
    assert shape_limits(EnrollIn) == ShapeLimits(1, 3, None)
    assert shape_limits(QuotaRequestIn) == ShapeLimits(1, 1, 19)


def test_the_report_limits_match_the_hand_formula_and_the_literals() -> None:
    """Вывод из модели равен формуле, которой пределы задавались до раунда 8: скобки объекта и
    двух списков плюс по скобке на элемент; запятые между полями отчёта, между полями каждого
    элемента и между элементами списков."""
    openers = 1 + 2 + REPORT_MAX_LINES + REPORT_MAX_ONLINE_IPS
    commas = (
        (len(ReportIn.model_fields) - 1)
        + REPORT_MAX_LINES * (len(ReportLine.model_fields) - 1)
        + REPORT_MAX_ONLINE_IPS * (len(OnlineIp.model_fields) - 1)
        + (REPORT_MAX_LINES - 1)
        + (REPORT_MAX_ONLINE_IPS - 1)
    )
    assert (
        shape_limits(ReportIn) == ShapeLimits(openers, commas, 19) == ShapeLimits(3_003, 9_007, 19)
    )
    assert (BODY_MAX_OPENERS, BODY_MAX_COMMAS) == (3_003, 9_007)


def test_the_problem_names_the_first_exceeded_limit_only() -> None:
    limits = ShapeLimits(2, 3, 19)
    assert limits.problem(b'{"a": [1, 2, 3]}', "этой операции") is None
    assert limits.problem(b"[[[]]]", "этой операции") == (
        "слишком много объектов и массивов для этой операции"
    )
    assert limits.problem(b"[1,2,3,4,5]", "этой операции") == (
        "слишком много членов и элементов для этой операции"
    )
    # Серия цифр ровно в ширину bigint проходит, на знак длиннее — отказ; без предела серии
    # (модель со строками) число любой длины доходит до разбора. Цифры не из ASCII — не цифры.
    assert limits.problem(b'{"a": 9223372036854775807}', "этой операции") is None
    assert limits.problem(b'{"a": 92233720368547758070}', "этой операции") == (
        "слишком длинное число для этой операции"
    )
    assert ShapeLimits(2, 3, None).problem(b'{"a": ' + b"9" * 4_300 + b"}", "x") is None
    assert limits.problem('{"a": "٩٩٩٩٩٩٩٩٩٩٩٩٩٩٩٩٩٩٩٩٩٩"}'.encode(), "x") is None


def test_nested_models_unions_and_bounded_lists_are_counted() -> None:
    class Inner(BaseModel):
        a: int = Field(le=10)
        b: Annotated[int, Field(lt=1_000)] | None = None

    class Outer(BaseModel):
        one: Inner
        many: Annotated[list[Inner], Field(max_length=3)]
        either: Annotated[int, Field(le=5)] | Annotated[str, Field(max_length=4)] = 0
        maybe: Inner | None = None

    # Outer: скобка + 3 запятых; one: 1/1; many: скобка + 3 × (1/1) + 2 запятых; either —
    # самая широкая ветвь, то есть бюджет текста 4/4; maybe: 1/1; целые формы не дают, а `str`
    # в объединении снимает предел серии цифр.
    assert shape_limits(Outer) == ShapeLimits(
        1 + 1 + (1 + 3) + 4 + 1, 3 + 1 + (3 + 2) + 4 + 1, None
    )


def test_a_model_without_an_upper_bound_is_refused_at_declaration() -> None:
    """Список без `max_length` или словарь произвольной формы не дают верхней границы: такую
    модель нельзя поставить на маршрут с проверкой формы, и это отказ при объявлении, а не
    молчаливое отсутствие проверки."""

    class Unbounded(BaseModel):
        items: list[int]

    class Free(BaseModel):
        payload: dict[str, int]

    with pytest.raises(ValueError, match="без max_length"):
        shape_limits(Unbounded)
    with pytest.raises(ValueError, match="словарь"):
        shape_limits(Free)


def test_strings_need_a_proven_alphabet_or_a_text_budget() -> None:
    """Символы формы внутри строки JSON проверка не отличает от настоящих, поэтому строковое
    поле либо доказывает по шаблону, что таких символов не содержит, либо прибавляет свой
    `max_length` к пределам как свободный текст. Доказательство — по разбору выражения: классы
    без отрицания, литералы, повторы, якоря с обоих концов; точка, отрицание, класс не-пробелов,
    незакреплённый
    шаблон и литерал запятой не доказывают ничего — такая строка идёт по бюджету."""

    class Proven(BaseModel):
        code: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]

    class Text(BaseModel):
        note: Annotated[str, Field(max_length=10)]
        dotted: Annotated[str, Field(max_length=5, pattern=r"^.{1,5}$")]

    class Unbounded(BaseModel):
        free: str

    assert shape_limits(Proven) == ShapeLimits(1, 0, None)
    assert shape_limits(Text) == ShapeLimits(1 + 10 + 5, 1 + 10 + 5, None)
    with pytest.raises(ValueError, match="без доказанного алфавита"):
        shape_limits(Unbounded)
    for pattern, proven in (
        (r"^[A-Za-z0-9_-]+$", True),
        (r"^[0-9A-Za-z][0-9A-Za-z.+_-]*$", True),
        (r"^[A-Za-z0-9+/=\s-]+$", True),
        (r"^[0-9a-f]{64}$", True),
        (r"^(?:ab|cd)+$", True),
        (r"\A[a-z]+\Z", True),
        (r"^[!-+]+$", True),
        (r"^[!-,]+$", False),
        (r"^.*$", False),
        (r"^[^x]+$", False),
        (r"^\S+$", False),
        (r"[a-z]+", False),
        (r"^[a-z]+", False),
        (r"[a-z]+$", False),
        (r"^x,y$", False),
        (r"^[a-z\[]+$", False),
        (r"^[a-z{]+$", False),
        (r"^(a)\1$", False),
        (r"^(", False),
    ):
        assert _pattern_alphabet_excludes(pattern, STRUCTURAL) is proven, pattern


def test_integers_need_an_upper_bound_and_give_the_digit_run_limit() -> None:
    """Число знаков самого большого `le` среди целых — предел серии цифр модели без строк
    (`lt` — на единицу меньше); целое без верхней границы — отказ при объявлении, как список
    без `max_length`: иначе тело из тысяч чисел по 4 300 цифр в пределах формы стоило бы вдвое
    дороже самой широкой законной части."""

    class Bounded(BaseModel):
        small: Annotated[int, Field(le=999)]
        big: Annotated[int, Field(lt=10_000)]
        flag: bool = False

    class Open(BaseModel):
        count: Annotated[int, Field(ge=0)]

    assert shape_limits(Bounded) == ShapeLimits(1, 2, 4)
    with pytest.raises(ValueError, match="целое без le"):
        shape_limits(Open)
