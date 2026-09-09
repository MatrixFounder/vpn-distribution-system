"""Redeem- и Promo-коды (постановка §4.14, §16.8; UC-09 шаги 4–5, UC-02 шаги 7–8).

Формат кода — ``VPN-XXXX-XXXY``: два блока по четыре символа алфавита из 32 знаков без
похожих друг на друга (``I``, ``O``, ``0``, ``1`` исключены); последний символ — контрольный.
Контрольная сумма — взвешенная сумма индексов семи символов полезной части по модулю 32 с
нечётными весами 1, 3, 5, 7, 9, 11, 13: нечётный вес взаимно прост с 32, поэтому подмена
любого одного символа меняет сумму всегда; перестановка соседних символов меняет её на
удвоенную разность индексов и не ловится только при разности 16 (одна пара из 32). Случайный
код проходит проверку с вероятностью 1/32. Неверный код отклоняется без обращения к базе
(§16.8) — ``checksum_ok`` чистая функция, реализована в 001.18.

Задача 001.18: ``CodeService`` — заглушки с фиксированными значениями (создание, партия,
экспорт, активация); логика — 001.20 (активация: срок, число использований, ограничение
тарифом, бонусы, лимиты §5.12), хранение кода — ``codes.code_hash``/``code_enc`` (§4.2.4).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import random
import re
import uuid
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.errors import ApiError

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 символа без I, O, 0, 1
PREFIX = "VPN"
PAYLOAD_LENGTH = 7  # полезная часть без контрольного символа
WEIGHTS = (1, 3, 5, 7, 9, 11, 13)  # нечётные — взаимно просты с размером алфавита
CODE_PATTERN = re.compile(r"^VPN-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")

CodeKind = Literal["redeem", "promo"]


def normalize_code(raw: object) -> str:
    """Привести ввод к каноническому виду ``VPN-XXXX-XXXX``: регистр и дефисы не важны, краевые
    пробелы обрезаются; не-строка или несовпадение с форматом — ``ValueError``."""
    if not isinstance(raw, str):
        raise ValueError("код должен быть строкой")
    compact = raw.strip().upper().replace("-", "")
    if len(compact) != len(PREFIX) + PAYLOAD_LENGTH + 1 or not compact.startswith(PREFIX):
        raise ValueError("код должен иметь вид VPN-XXXX-XXXX")
    body = compact[len(PREFIX) :]
    if any(ch not in ALPHABET for ch in body):
        raise ValueError("код содержит символы вне алфавита")
    return f"{PREFIX}-{body[:4]}-{body[4:]}"


def checksum_char(code: str) -> str:
    """Контрольный символ для кода (полезная часть — первые семь символов после префикса)."""
    body = code.strip().upper().replace("-", "")
    if body.startswith(PREFIX):
        body = body[len(PREFIX) :]
    payload = body[:PAYLOAD_LENGTH]
    if len(payload) != PAYLOAD_LENGTH or any(ch not in ALPHABET for ch in payload):
        raise ValueError("полезная часть кода — семь символов алфавита")
    total = sum(weight * ALPHABET.index(ch) for weight, ch in zip(WEIGHTS, payload, strict=True))
    return ALPHABET[total % len(ALPHABET)]


def checksum_ok(raw: object) -> bool:
    """Истина, если код имеет верный формат и контрольный символ; любой мусор (в том числе не
    строка) — ложь без исключений и без обращения к базе (§16.8)."""
    try:
        code = normalize_code(raw)
    except ValueError:
        return False
    return code[-1] == checksum_char(code)


def secure_source() -> random.Random:
    """Источник случайности для выдачи кодов — ``random.SystemRandom`` (ОС, не `random`
    модуля: код даёт платную подписку, §7.2)."""
    return random.SystemRandom()


def generate_code(rng: random.Random | None = None) -> str:
    """Случайный код с верной контрольной суммой; ``rng`` — источник только для тестов, без
    него — ``secure_source()``."""
    source = rng if rng is not None else secure_source()
    payload = "".join(source.choice(ALPHABET) for _ in range(PAYLOAD_LENGTH))
    code = f"{PREFIX}-{payload[:4]}-{payload[4:]}"
    return code + checksum_char(code)


EXAMPLE_VALID = "VPN-ABCD-EFG" + checksum_char("VPN-ABCD-EFG")
EXAMPLE_INVALID = "VPN-ABCD-EFG" + next(ch for ch in ALPHABET if ch != EXAMPLE_VALID[-1])


class CodeSpec(BaseModel):
    """Свойства кода (§4.14): срок, число использований, лимит на пользователя, ограничение
    тарифом, бонусы трафика и дней."""

    kind: CodeKind = "redeem"
    plan_id: uuid.UUID | None = None
    expires_at: dt.datetime | None = None
    max_uses: int = Field(default=1, ge=1)
    max_uses_per_user: int = Field(default=1, ge=1)
    traffic_bonus_bytes: int = Field(default=0, ge=0)
    duration_bonus_days: int = Field(default=0, ge=0)


class Code(BaseModel):
    """Созданный код: сам код показывается один раз при создании (в базе — хеш и шифртекст)."""

    id: uuid.UUID
    code: str
    kind: CodeKind
    plan_id: uuid.UUID | None
    expires_at: dt.datetime | None
    max_uses: int
    max_uses_per_user: int
    uses_count: int
    traffic_bonus_bytes: int
    duration_bonus_days: int
    batch_id: uuid.UUID | None
    created_at: dt.datetime


class Redemption(BaseModel):
    id: uuid.UUID
    code_id: uuid.UUID
    user_id: uuid.UUID
    period_id: uuid.UUID
    redeemed_at: dt.datetime


STUB_CODE_ID = uuid.UUID("00000000-0000-7000-8000-0000000000d1")
STUB_BATCH_ID = uuid.UUID("00000000-0000-7000-8000-0000000000d2")
STUB_PLAN_ID = uuid.UUID("00000000-0000-7000-8000-0000000000c1")
STUB_CREATED_AT = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
STUB_EXPIRES_AT = dt.datetime(2026, 12, 31, tzinfo=dt.UTC)
STUB_REDEMPTION = Redemption(
    id=uuid.UUID("00000000-0000-7000-8000-0000000000d3"),
    code_id=STUB_CODE_ID,
    user_id=uuid.UUID("00000000-0000-7000-8000-0000000000e1"),
    period_id=uuid.UUID("00000000-0000-7000-8000-0000000000a1"),
    redeemed_at=dt.datetime(2026, 9, 2, tzinfo=dt.UTC),
)
EXPORT_HEADER = "code,expires_at,plan"


def csv_rows(rows: Iterable[tuple[str, str, str]]) -> list[str]:
    """Строки CSV экспорта (без переводов строк — их ставит ответ): заголовок и по строке на
    код; значения экранируются модулем ``csv`` (запятая или кавычка в имени тарифа не ломают
    разбор), ячейка, начинающаяся с ``= + - @``, табуляции или возврата каретки, получает
    апостроф — защита от формул в электронных таблицах (OWASP CSV injection)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EXPORT_HEADER.split(","))
    for row in rows:
        writer.writerow([_neutralize(cell) for cell in row])
    return buffer.getvalue().splitlines()


def _neutralize(cell: str) -> str:
    return "'" + cell if cell and cell[0] in "=+-@\t\r" else cell


class CodeService:
    """Коды поверх пула asyncpg. Заглушка 001.18: база не читается и не пишется, коды
    генерируются настоящие (контрольная сумма), но не сохраняются."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def create(self, spec: CodeSpec, created_by: uuid.UUID) -> Code:
        """Один код по свойствам ``spec``; заглушка — сгенерированный код с фиксированным id."""
        return Code(
            id=STUB_CODE_ID,
            code=generate_code(),
            kind=spec.kind,
            plan_id=spec.plan_id,
            expires_at=spec.expires_at,
            max_uses=spec.max_uses,
            max_uses_per_user=spec.max_uses_per_user,
            uses_count=0,
            traffic_bonus_bytes=spec.traffic_bonus_bytes,
            duration_bonus_days=spec.duration_bonus_days,
            batch_id=None,
            created_at=STUB_CREATED_AT,
        )

    async def create_batch(self, n: int, spec: CodeSpec, created_by: uuid.UUID) -> uuid.UUID:
        """Партия из ``n`` кодов с общими свойствами; заглушка — фиксированный id партии."""
        return STUB_BATCH_ID

    async def export(self, batch_id: uuid.UUID) -> Iterable[str]:
        """Строки CSV партии: заголовок ``code,expires_at,plan`` и по строке на код (UC-09 шаг
        5; экспорт содержит все коды партии). Заглушка — три сгенерированных кода; строки
        собирает ``csv_rows`` (RFC 4180: кавычки и экранирование, без переводов строк)."""
        rows = [(generate_code(), STUB_EXPIRES_AT.isoformat(), str(STUB_PLAN_ID)) for _ in range(3)]
        return csv_rows(rows)

    async def redemptions(self, code_id: uuid.UUID) -> list[Redemption]:
        """Использования кода (UC-09: отслеживание); заглушка — одна запись."""
        return [STUB_REDEMPTION]

    async def redeem(self, user_id: uuid.UUID, code: str) -> Redemption:
        """Активация кода пользователем (UC-02 шаги 7–10; логика — 001.20). Уже сейчас:
        неверная контрольная сумма отклоняется без обращения к базе (§16.8, UC-02 A3)."""
        if not checksum_ok(code):
            raise ApiError("invalid_code", "код не распознан", status=400)
        return STUB_REDEMPTION
