"""Форма отчёта о трафике (задача 001.33): ограничения модели, которые в заглушке настоящие —
ключи и границы таблиц §4.2.5, порядок и длина периода, канонические формы значений, пределы
размера из бюджета Н-4. Через HTTP те же правила проверяет `tests/e2e/test_reports.py`; здесь —
модель напрямую, в том числе предел адресов целиком."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from app.accounting.canonical import TIMESTAMP, TIMESTAMP_MAX_CHARS, UUID_CHARS
from app.accounting.quota import QuotaGrant
from app.accounting.service import (
    REPORT_MAX_EPOCHS_PER_DAY,
    REPORT_MAX_INTERVAL_AGE,
    REPORT_MAX_LINES,
    REPORT_MAX_ONLINE_IPS,
    REPORT_MAX_OPEN_INTERVALS,
    REPORT_MAX_PARTS,
    REPORT_MAX_PERIOD,
    REPORT_PARTS_DEADLINE,
    Accept,
    ReportIn,
)
from app.domain.statuses import INT8_MAX
from pydantic import ValidationError

from tests._reports import report_with

USER_A = "00000000-0000-7000-8000-0000000000a1"
USER_B = "00000000-0000-7000-8000-0000000000a4"
VALID: dict[str, Any] = {
    "counter_epoch": "00000000-0000-7000-8000-0000000000e1",
    "report_seq": 1,
    "parts_total": 1,
    "period_start": "2026-09-10T12:00:00Z",
    "period_end": "2026-09-10T12:01:00Z",
    "lines": [{"user_id": USER_A, "uplink_bytes": 1, "downlink_bytes": 2}],
    "online_ips": [{"user_id": USER_A, "ip": "198.51.100.23", "last_seen": "2026-09-10T12:00:57Z"}],
    "node_rx_bytes": 3,
    "node_tx_bytes": 4,
}


def errors_of(body: dict[str, Any]) -> list[str]:
    with pytest.raises(ValidationError) as failure:
        ReportIn.model_validate(body)
    return [error["type"] for error in failure.value.errors()]


def test_the_size_limits_are_literal_and_the_limit_itself_is_accepted() -> None:
    """Пределы закреплены литералами (страж, растущий вместе с константой, не заметил бы её
    роста); ровно предел принимается, на один больше — `too_long` и ничего кроме: тело на
    пределе иначе отвергалось бы по другой причине, и страж мерил бы её."""
    assert (REPORT_MAX_LINES, REPORT_MAX_ONLINE_IPS) == (500, 2_500)
    full = ReportIn.model_validate(report_with(500, 2_500))
    assert (len(full.lines), len(full.online_ips)) == (500, 2_500)
    assert errors_of(report_with(501, 0)) == ["too_long"]
    assert errors_of(report_with(1, 2_501)) == ["too_long"]


def test_the_period_is_ordered_and_no_longer_than_an_hour() -> None:
    """Строка отчёта относится к часу по `period_start` (§4.2.5); штатный интервал — 60 с
    (§5.9, обязанность агента, которую Control Plane наблюдает метрикой — 001.34/001.68),
    длиннее — только дельта после перерыва снятия счётчиков, предел — час. Ровно час — принят."""
    assert REPORT_MAX_PERIOD == dt.timedelta(hours=1), "предел — час; штатно 60 с (README)"
    ReportIn.model_validate({**VALID, "period_end": "2026-09-10T13:00:00Z"})
    assert errors_of({**VALID, "period_end": "2026-09-10T13:00:00.000001Z"}) == ["value_error"]
    # Интервал через границу часа принимается и целиком относится к часу начала (§4.2.5):
    # ошибка отнесения ограничена длиной интервала, а не запретом.
    ReportIn.model_validate(
        {**VALID, "period_start": "2026-09-10T12:59:30Z", "period_end": "2026-09-10T13:00:30Z"}
    )


def test_the_parts_count_is_bounded_on_both_sides() -> None:
    """`parts_total` — объявленное число частей интервала (README: растёт только вверх,
    действует последнее принятое — правило 001.34): с единицы, не больше `REPORT_MAX_PARTS`
    (граница нужна, чтобы поле было ограничено с обеих сторон, как остальные), строго целое,
    обязательно в каждой части; номер последней части (`report_seq` + `parts_total` − 1) — в
    ширине `bigint`, иначе интервал нельзя было бы завершить. Числа контракта, которые читает
    001.34 (срок закрытия интервала — из Н-8 плюс час предела интервала, потолок открытых
    интервалов ноды, потолок смен эпохи за скользящие сутки), закреплены литералами."""
    assert REPORT_MAX_PARTS == 1_000
    assert REPORT_PARTS_DEADLINE == dt.timedelta(hours=25)
    assert REPORT_PARTS_DEADLINE == dt.timedelta(hours=24) + REPORT_MAX_PERIOD, "Н-8 плюс предел"
    assert (REPORT_MAX_OPEN_INTERVALS, REPORT_MAX_EPOCHS_PER_DAY) == (8, 48)
    # Потолок возраста открытого интервала — два буфера Н-8, не меньше срока после последней
    # части (иначе срок не применялся бы никогда); закрытие по возрасту — 001.37.
    assert REPORT_MAX_INTERVAL_AGE == dt.timedelta(hours=48) == 2 * dt.timedelta(hours=24)
    assert REPORT_MAX_INTERVAL_AGE >= REPORT_PARTS_DEADLINE
    ReportIn.model_validate({**VALID, "parts_total": 1_000})
    assert errors_of({**VALID, "parts_total": 0}) == ["greater_than_equal"]
    assert errors_of({**VALID, "parts_total": 1_001}) == ["less_than_equal"]
    assert errors_of({**VALID, "parts_total": True}) == ["int_type"]
    assert errors_of({k: v for k, v in VALID.items() if k != "parts_total"}) == ["missing"]
    # Последняя часть ровно на пределе — приём; на один дальше — отказ правилом модели.
    ReportIn.model_validate({**VALID, "report_seq": INT8_MAX - 1, "parts_total": 2})
    ReportIn.model_validate({**VALID, "report_seq": INT8_MAX, "parts_total": 1})
    assert errors_of({**VALID, "report_seq": INT8_MAX, "parts_total": 2}) == ["value_error"]
    assert errors_of({**VALID, "report_seq": INT8_MAX - 998, "parts_total": 1_000}) == [
        "value_error"
    ]


def test_only_canonical_forms_are_accepted() -> None:
    """Самое большое допустимое тело имеет верхнюю границу по ширине только потому, что модель
    не принимает иных форм: UUID — 36 символов с дефисами, метка времени — текст по грамматике,
    самая длинная строка которой — `TIMESTAMP_MAX_CHARS` (константа выведена из грамматики и
    согласована с ней здесь: на знак длиннее грамматика не пропускает), адрес — текст без zone id
    (`inet` PostgreSQL его не хранит) и не число. Каждое правило — `value_error` до разбора
    значения."""
    assert (TIMESTAMP_MAX_CHARS, UUID_CHARS) == (32, 36)
    widest_stamp = "2026-09-10T12:00:57.123456+00:00"
    assert len(widest_stamp) == TIMESTAMP_MAX_CHARS and TIMESTAMP.match(widest_stamp)
    assert TIMESTAMP.match("2026-09-10T12:00:57.1234567+00:00") is None
    address = VALID["online_ips"][0]
    for label, body in (
        ("urn", {**VALID, "counter_epoch": "urn:uuid:00000000-0000-7000-8000-0000000000e1"}),
        ("hex 32", {**VALID, "node_id": USER_A.replace("-", "")}),
        ("end too long", {**VALID, "period_end": "2026-09-10T12:01:00.1234567+00:00"}),
        ("start too long", {**VALID, "period_start": "2026-09-10T12:00:00.0000001+00:00"}),
        # Та же секунда, что в тексте: отказывает правило формы, а не порядок или длина периода.
        ("start as number", {**VALID, "period_start": 1789041600}),
        ("start as digits string", {**VALID, "period_start": "1789041600"}),
        ("start with space separator", {**VALID, "period_start": "2026-09-10 12:00:00Z"}),
        ("start without offset", {**VALID, "period_start": "2026-09-10T12:00:00"}),
        ("end with seven fraction digits", {**VALID, "period_end": "2026-09-10T12:01:00.1234567Z"}),
        ("end with compact offset", {**VALID, "period_end": "2026-09-10T12:01:00+0000"}),
        ("last_seen as number", {**VALID, "online_ips": [{**address, "last_seen": 1789041657}]}),
        ("scope id", {**VALID, "online_ips": [{**address, "ip": "fe80::1%eth0"}]}),
        ("ip as number", {**VALID, "online_ips": [{**address, "ip": 3232235777}]}),
    ):
        assert errors_of(body) == ["value_error"], label
    # Дефисы не на месте — 36 символов, но не UUID: отвергает уже pydantic-core, и это закреплено.
    assert errors_of({**VALID, "counter_epoch": "----" + USER_A.replace("-", "")}) == [
        "uuid_parsing"
    ]
    ReportIn.model_validate({**VALID, "period_end": "2026-09-10T12:01:00.123456+00:00"})
    ReportIn.model_validate({**VALID, "period_end": "2026-09-10T12:01:00.1Z"})
    ReportIn.model_validate({**VALID, "node_id": "00000000-0000-7000-8000-0000000000B1"})
    ReportIn.model_validate({**VALID, "node_id": None})
    ReportIn.model_validate({**VALID, "online_ips": [{**address, "ip": "::ffff:192.0.2.1"}]})


def test_period_end_must_follow_period_start() -> None:
    """`CHECK (period_end > period_start)` таблиц §4.2.5 — отказ по контракту, не ошибкой
    вставки; равенство тоже отказ, микросекунда разницы — приём."""
    assert errors_of({**VALID, "period_end": VALID["period_start"]}) == ["value_error"]
    assert errors_of({**VALID, "period_end": "2026-09-10T11:59:59Z"}) == ["value_error"]
    ReportIn.model_validate({**VALID, "period_end": "2026-09-10T12:00:00.000001Z"})


def test_users_are_unique_in_lines_and_pairs_are_unique_in_addresses() -> None:
    """Первичные ключи `traffic_lines` (пользователь) и `user_online_ips` (пользователь × адрес
    в пределах ноды): дубль — 422. Один адрес у двух пользователей и два адреса у одного — не
    дубли."""
    line = VALID["lines"][0]
    assert errors_of({**VALID, "lines": [line, {**line, "uplink_bytes": 9}]}) == ["value_error"]
    address = VALID["online_ips"][0]
    assert errors_of(
        {**VALID, "online_ips": [address, {**address, "last_seen": "2026-09-10T12:00:58Z"}]}
    ) == ["value_error"]
    ReportIn.model_validate(
        {
            **VALID,
            "online_ips": [
                address,
                {**address, "user_id": USER_B},
                {**address, "ip": "2001:db8::17"},
            ],
        }
    )


def test_ip_equality_is_by_address_not_by_text() -> None:
    """Пара сравнивается по разобранному адресу: `2001:db8::17` и `2001:0db8:0000::0017` —
    один адрес, и таким дублем вставка в `user_online_ips` тоже споткнулась бы. Семейства
    различаются, как у `inet`: `192.0.2.1` и `::192.0.2.1` — разные адреса одного числа."""
    address = VALID["online_ips"][0]
    ReportIn.model_validate(
        {**VALID, "online_ips": [{**address, "ip": "192.0.2.1"}, {**address, "ip": "::192.0.2.1"}]}
    )
    assert errors_of(
        {
            **VALID,
            "online_ips": [
                {**address, "ip": "2001:db8::17"},
                {**address, "ip": "2001:0db8:0000:0000:0000:0000:0000:0017"},
            ],
        }
    ) == ["value_error"]


def test_every_level_of_the_body_is_closed_for_extra_fields() -> None:
    assert errors_of({**VALID, "x": 1}) == ["extra_forbidden"]
    assert errors_of({**VALID, "lines": [{**VALID["lines"][0], "x": 1}]}) == ["extra_forbidden"]
    assert errors_of({**VALID, "online_ips": [{**VALID["online_ips"][0], "x": 1}]}) == [
        "extra_forbidden"
    ]


def test_node_id_is_optional_and_absent_by_default() -> None:
    assert ReportIn.model_validate(VALID).node_id is None
    assert ReportIn.model_validate({**VALID, "node_id": USER_A}).node_id == uuid.UUID(USER_A)


def test_the_accept_answer_is_bounded_like_the_column() -> None:
    """`last_accepted_seq` — `bigint` от нуля («ничего не принято») до ширины колонки."""
    assert Accept(last_accepted_seq=0, duplicate=False).last_accepted_seq == 0
    assert Accept(last_accepted_seq=2**63 - 1, duplicate=True).last_accepted_seq == 2**63 - 1
    with pytest.raises(ValidationError):
        Accept(last_accepted_seq=-1, duplicate=False)
    with pytest.raises(ValidationError):
        Accept(last_accepted_seq=2**63, duplicate=False)


def test_the_grant_answer_is_bounded_like_its_columns() -> None:
    """Ответ гранта ограничен так же, как строка `quota_grants` (§4.2.5): объём — `bigint` от
    нуля, номер выдачи — `bigint` с единицы; строгие целые (`True` — не номер). Ограничения
    ответа — граница для 001.36: значение шире колонки упало бы вставкой, а не отказом."""
    assert QuotaGrant(quota_grant_bytes=0, issued_seq=1).quota_grant_bytes == 0
    assert QuotaGrant(quota_grant_bytes=INT8_MAX, issued_seq=INT8_MAX).issued_seq == INT8_MAX
    for wrong in (
        {"quota_grant_bytes": -1, "issued_seq": 1},
        {"quota_grant_bytes": INT8_MAX + 1, "issued_seq": 1},
        {"quota_grant_bytes": 1, "issued_seq": 0},
        {"quota_grant_bytes": 1, "issued_seq": INT8_MAX + 1},
        {"quota_grant_bytes": True, "issued_seq": 1},
        {"quota_grant_bytes": 1, "issued_seq": "1"},
    ):
        with pytest.raises(ValidationError):
            QuotaGrant.model_validate(wrong)
