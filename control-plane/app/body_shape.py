"""Форма тела запроса по байтам — пределы, выведенные из модели pydantic, и проверка до разбора
JSON (задача 001.33, раунды 5–8 и закрытие).

Путь отказа не дороже пути приёма: pydantic строит по ошибке на каждое лишнее поле, и тело в
64 КиБ из тысяч лишних ключей стоило бы десятки миллисекунд цикла событий до усечения ответа
(без проверки формы heartbeat из ключей минимальной длины стоил 24 мс по p95 стенда в раунде 7,
с проверкой — 2 мс); тело в 1 МиБ из 131 000 ключей — 131 000 ошибок. У любого допустимого
тела число открывающих скобок и запятых ограничено сверху самой моделью: объект — одна скобка
и (число полей − 1) запятых, список — скобка, элементы и (длина − 1) запятых. Тело с дублями
ключей JSON даёт лишние запятые и считается широким — контракт дублей не предусматривает.
Проверка — ``bytes.count`` и один поиск серии цифр, доли миллисекунды на 1 МиБ, и стоит до
разрешения зависимостей (до отказа по версии и identity): широкое тело без identity получает
422, а не 401 — пределы формы публичны (README).

Строки. Символы формы внутри строки JSON считаются наравне с остальными (проверка не разбирает
JSON), поэтому строковое поле входит в предел одним из двух способов: поле с ``pattern``, чей
алфавит доказуемо не содержит ``,``, ``[`` и ``{`` (канонические UUID, версии, base64url, PEM —
доказательство по разбору регулярного выражения при объявлении: только классы символов,
литералы, повторы и якоря с обоих концов), — ничего не прибавляет; поле свободного текста
(без шаблона, с ``max_length``) прибавляет ``max_length`` к обоим пределам — столько символов
формы в нём поместится (текст ошибки исполнения команды, ``CommandResultIn.error``). Строка без
того и другого верхней границы не даёт.

Числа. Предел формы не ограничивал бы длину числового литерала: тело из сотен чисел по
4 300 цифр (столько разбирает jiter) в пределах формы стоило вдвое дороже самой широкой
законной части (закрытие 001.33), потому что каждое такое число превращается в ``int`` до
проверки ``le``. У модели без строковых полей самая длинная допустимая серия цифр — число знаков
самого большого ``le`` среди её целых полей (``bigint`` — 19): более длинная серия в теле —
отказ до разбора. У модели со строковыми полями серия цифр может стоять внутри строки, и
проверка не применяется; её тела не больше 64 КиБ, то есть не больше пятнадцати таких чисел.
Целое без верхней границы (``le``/``lt``) модели Node API недопустимо.

Пределы выводятся из полей модели рекурсивно (``shape_limits``): вложенная модель — её скобка и
запятые, список моделей с ``max_length`` — столько экземпляров, сколько разрешает предел, плюс
скобка списка и запятые между элементами, объединение типов — самая широкая ветвь. Список без
верхней границы, словарь произвольной формы, строка без доказанного алфавита и без
``max_length``, целое без ``le`` верхней границы не дают — такая модель в Node API недопустима
(``ValueError`` при построении маршрута, а не молчаливое отсутствие проверки).
"""

from __future__ import annotations

import re
import types
import typing
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from pydantic.fields import FieldInfo

STRUCTURAL = frozenset(",[{")
# Маска для поиска серии цифр: цифры → «0», остальное → «.»; поиск подстроки из нулей —
# ``bytes.__contains__`` (C, доли миллисекунды на 1 МиБ), а не регулярное выражение с повтором
# (5 мс на самой широкой части — десятая доля бюджета разбора).
_DIGIT_MASK = bytes(ord("0") if chr(b).isdigit() and b < 128 else ord(".") for b in range(256))


@dataclass(frozen=True)
class ShapeLimits:
    """Верхние границы допустимого тела: число открывающих скобок (``[`` и ``{``), число запятых
    и, у модели без строковых полей, длина самой длинной серии цифр."""

    max_openers: int
    max_commas: int
    max_digit_run: int | None = None

    def problem(self, body: bytes, subject: str) -> str | None:
        """Причина отказа по форме, если тело заведомо шире допустимого, иначе ``None``."""
        if body.count(b"[") + body.count(b"{") > self.max_openers:
            return f"слишком много объектов и массивов для {subject}"
        if body.count(b",") > self.max_commas:
            return f"слишком много членов и элементов для {subject}"
        if self.max_digit_run is not None and b"0" * (self.max_digit_run + 1) in body.translate(
            _DIGIT_MASK
        ):
            return f"слишком длинное число для {subject}"
        return None


@dataclass(frozen=True)
class _Shape:
    openers: int
    commas: int
    digits: int | None  # самый широкий предел целого среди полей (знаков), None — целых нет
    text: bool  # есть строковое поле — серия цифр может стоять внутри строки

    def __add__(self, other: _Shape) -> _Shape:
        digits = max((d for d in (self.digits, other.digits) if d is not None), default=None)
        return _Shape(
            self.openers + other.openers,
            self.commas + other.commas,
            digits,
            self.text or other.text,
        )


_NOTHING = _Shape(0, 0, None, False)


def _metadata_value(metadata: list[Any], name: str) -> Any:
    """Значение ограничения (``max_length``, ``pattern``, ``le``, ``lt``) из метаданных поля:
    у поля модели они лежат в ``field.metadata``, у ``Annotated`` внутри объединения — в
    ``Field(...)`` среди аргументов ``Annotated`` (и там — в его ``metadata``)."""
    for item in metadata:
        if isinstance(item, FieldInfo):
            value = _metadata_value(list(item.metadata), name)
        else:
            value = getattr(item, name, None)
        if value is not None:
            return value
    return None


def _pattern_alphabet_excludes(pattern: str, forbidden: frozenset[str]) -> bool:
    """Доказательство по разбору выражения: шаблон закреплён якорями с обоих концов и состоит
    только из литералов, классов символов без отрицания, категорий ``\\d``/``\\w``/``\\s``,
    групп, ветвей и повторов, и ни литерал, ни диапазон не содержит запрещённого символа.
    Всё, что доказать нельзя (``.``, отрицания, незакреплённый шаблон), — «не доказано»."""
    parser = re._parser  # type: ignore[attr-defined]  # noqa: SLF001 — разбор шаблона re
    consts = re._constants  # type: ignore[attr-defined]  # noqa: SLF001
    codes = {ord(ch) for ch in forbidden}
    allowed_categories = {
        consts.CATEGORY_DIGIT,
        consts.CATEGORY_WORD,
        consts.CATEGORY_SPACE,
        consts.CATEGORY_UNI_DIGIT,
        consts.CATEGORY_UNI_WORD,
        consts.CATEGORY_UNI_SPACE,
    }

    def set_ok(items: list[tuple[Any, Any]]) -> bool:
        for op, arg in items:
            if op is consts.LITERAL:
                if arg in codes:
                    return False
            elif op is consts.RANGE:
                low, high = arg
                if any(low <= code <= high for code in codes):
                    return False
            elif op is consts.CATEGORY:
                if arg not in allowed_categories:
                    return False
            else:  # NEGATE и всё незнакомое
                return False
        return True

    def ok(sub: Any) -> bool:
        for op, arg in sub:
            if op is consts.LITERAL:
                if arg in codes:
                    return False
            elif op is consts.IN:
                if not set_ok(arg):
                    return False
            elif op is consts.CATEGORY:
                if arg not in allowed_categories:
                    return False
            elif op is consts.AT:
                continue
            elif op in (consts.MAX_REPEAT, consts.MIN_REPEAT, consts.POSSESSIVE_REPEAT):
                if not ok(arg[2]):
                    return False
            elif op is consts.SUBPATTERN:
                if not ok(arg[3]):
                    return False
            elif op is consts.ATOMIC_GROUP:
                if not ok(arg):
                    return False
            elif op is consts.BRANCH:
                if not all(ok(branch) for branch in arg[1]):
                    return False
            else:  # ANY, NOT_LITERAL, GROUPREF, ASSERT и прочее — не доказывается
                return False
        return True

    try:
        parsed = parser.parse(pattern)
    except re.error:
        return False
    items = list(parsed)
    if not items:
        return False
    first, last = items[0], items[-1]
    anchored = (
        first[0] is consts.AT
        and first[1] in (consts.AT_BEGINNING, consts.AT_BEGINNING_STRING)
        and last[0] is consts.AT
        and last[1] in (consts.AT_END, consts.AT_END_STRING)
    )
    return anchored and ok(parsed)


def _digits_of_bound(metadata: list[Any]) -> int | None:
    le = _metadata_value(metadata, "le")
    lt = _metadata_value(metadata, "lt")
    bound = None
    if le is not None:
        bound = int(le)
    if lt is not None:
        bound = int(lt) - 1 if bound is None else min(bound, int(lt) - 1)
    if bound is None:
        return None
    return len(str(abs(bound)))


def _shape_of_type(annotation: Any, metadata: list[Any]) -> _Shape:
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        inner, *extra = typing.get_args(annotation)
        return _shape_of_type(inner, list(extra) + metadata)
    if origin in (typing.Union, types.UnionType):
        # ``X | None`` и объединения скаляров: форма — самая широкая из ветвей.
        members = [m for m in typing.get_args(annotation) if m is not type(None)]
        branches = [_shape_of_type(m, metadata) for m in members]
        digits = max((b.digits for b in branches if b.digits is not None), default=None)
        return _Shape(
            max(b.openers for b in branches),
            max(b.commas for b in branches),
            digits,
            any(b.text for b in branches),
        )
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _shape_of_model(annotation)
    if origin in (list, tuple, set, frozenset):
        (item,) = typing.get_args(annotation)[:1]
        length = _metadata_value(metadata, "max_length")
        if length is None:
            raise ValueError(f"список без max_length не даёт верхней границы формы: {annotation!r}")
        length = int(length)
        inner = _shape_of_type(item, [])
        return _Shape(
            1 + length * inner.openers,
            max(length - 1, 0) + length * inner.commas,
            inner.digits,
            inner.text,
        )
    if origin is dict or annotation is dict:
        raise ValueError(f"словарь произвольной формы не даёт верхней границы: {annotation!r}")
    if annotation is str:
        pattern = _metadata_value(metadata, "pattern")
        if pattern is not None and _pattern_alphabet_excludes(str(pattern), STRUCTURAL):
            return _Shape(0, 0, None, True)
        length = _metadata_value(metadata, "max_length")
        if length is None:
            raise ValueError(
                "строка без доказанного алфавита и без max_length не даёт верхней границы формы"
            )
        return _Shape(int(length), int(length), None, True)
    if annotation is bool:
        return _NOTHING
    if annotation is int:
        digits = _digits_of_bound(metadata)
        if digits is None:
            raise ValueError("целое без le не даёт верхней границы длины числа")
        return _Shape(0, 0, digits, False)
    return _NOTHING


def _shape_of_model(model: type[BaseModel]) -> _Shape:
    fields = list(model.model_fields.values())
    total = _Shape(1, max(len(fields) - 1, 0), None, False)
    for field in fields:
        total = total + _shape_of_type(field.annotation, list(field.metadata))
    return total


def shape_limits(model: type[BaseModel]) -> ShapeLimits:
    """Пределы формы тела, разобранного в ``model``: скобка объекта и (полей − 1) запятых плюс
    вложенные модели, списки с ``max_length`` и бюджеты свободного текста; предел серии цифр —
    только у модели без строковых полей (см. докстринг модуля)."""
    shape = _shape_of_model(model)
    return ShapeLimits(
        shape.openers, shape.commas, None if shape.text or shape.digits is None else shape.digits
    )
