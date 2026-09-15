"""Ответ 422 единого формата (`app/errors.py`, 001.33): потолок числа ошибок, усечение `loc` и
`msg`. Здесь — обработчик напрямую с искусственными ошибками: через HTTP то же проверяет
`tests/e2e/test_reports.py`, но валидатора, который вставлял бы значение в сообщение, в
приложении нет, и предел на `msg` иначе не измерить."""

from __future__ import annotations

import json

from app.errors import (
    VALIDATION_ERRORS_MAX,
    VALIDATION_LOC_MAX_CHARS,
    VALIDATION_MSG_MAX_CHARS,
    _validation_handler,
)
from fastapi import Request
from fastapi.exceptions import RequestValidationError


def _request() -> Request:
    return Request(
        {"type": "http", "method": "POST", "path": "/x", "headers": [], "query_string": b""}
    )


async def test_the_answer_is_bounded_in_count_loc_and_message() -> None:
    assert (VALIDATION_ERRORS_MAX, VALIDATION_LOC_MAX_CHARS, VALIDATION_MSG_MAX_CHARS) == (
        50,
        64,
        200,
    )
    errors = [
        {"loc": ("body", "k" * 10_000, n), "msg": "m" * 10_000, "type": "extra_forbidden"}
        for n in range(70)
    ]
    response = await _validation_handler(_request(), RequestValidationError(errors))
    assert response.status_code == 422
    details = json.loads(bytes(response.body))["error"]["details"]
    assert len(details["errors"]) == 50 and details["truncated"] == 20
    assert details["errors"][0]["loc"] == ["body", "k" * 64, 0]
    assert len(details["errors"][0]["msg"]) == 200
    assert len(response.body) < 20_000
