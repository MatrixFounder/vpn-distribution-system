"""TC-UNIT-01 задачи 001.18: контрольная сумма Redeem/Promo-кода — чистая функция без базы
(§16.8: неверный код отклоняется без обращения к базе). Генератор тестов детерминирован
(``random.Random`` с семенем — не для секретов, поэтому S311 подавлен)."""

from __future__ import annotations

import random
from unittest import mock

import pytest
from app.domain import codes


def test_checksum_accepts_generated_and_rejects_altered() -> None:
    """Сгенерированный код проходит проверку; замена любого одного символа (полезной части или
    контрольного) — всегда ложь; перестановка соседних символов полезной части — ложь, кроме
    пары с разностью индексов 16 (свойство суммы по модулю 32, объявлено в модуле)."""
    rng = random.Random(7)  # noqa: S311 — детерминированные тестовые данные
    prefix = len(codes.PREFIX)
    for _ in range(300):
        code = codes.generate_code(rng)
        assert codes.checksum_ok(code), code
        assert codes.CODE_PATTERN.fullmatch(code), code
        body = code.replace("-", "")
        for position in range(prefix, len(body)):
            for alternative in codes.ALPHABET:
                if alternative == body[position]:
                    continue
                altered = body[:position] + alternative + body[position + 1 :]
                assert not codes.checksum_ok(altered), (code, position, alternative)
        for position in range(prefix, len(body) - 2):  # только полезная часть
            a, b = body[position], body[position + 1]
            if a == b or abs(codes.ALPHABET.index(a) - codes.ALPHABET.index(b)) == 16:
                continue
            swapped = body[:position] + b + a + body[position + 2 :]
            assert not codes.checksum_ok(swapped), (code, position)


def test_checksum_tolerates_case_and_dashes_but_not_garbage() -> None:
    """Ввод пользователя: регистр и дефисы не важны; посторонние символы, лишняя длина, пустая
    строка и символы вне алфавита (I, O, 0, 1) — ложь, без исключений."""
    code = codes.generate_code(random.Random(1))  # noqa: S311
    assert codes.checksum_ok(code.lower())
    assert codes.checksum_ok(code.replace("-", ""))
    assert codes.checksum_ok(f" {code} ")
    assert codes.normalize_code(code.lower().replace("-", "")) == code
    for garbage in (
        "",
        "VPN",
        "VPN-ABCD",
        code + "A",
        code[:-1],
        "VPN-ABC0-EFGH",
        "VPN-ABCI-EFG1",
        "ABC-ABCD-EFGH",
    ):
        assert not codes.checksum_ok(garbage), garbage
    valid, invalid = codes.EXAMPLE_VALID, codes.EXAMPLE_INVALID
    assert codes.checksum_ok(valid) and not codes.checksum_ok(invalid)
    assert valid.startswith("VPN-ABCD-EFG") and invalid.startswith("VPN-ABCD-EFG")


def test_generated_codes_are_distinct_and_from_the_alphabet() -> None:
    rng = random.Random(42)  # noqa: S311
    generated = {codes.generate_code(rng) for _ in range(1000)}
    assert len(generated) == 1000, "коды случайны и не повторяются"
    assert all(set(code.replace("-", "")[3:]) <= set(codes.ALPHABET) for code in generated)
    assert len(codes.ALPHABET) == 32 and not set("IO01") & set(codes.ALPHABET)
    assert all(weight % 2 == 1 for weight in codes.WEIGHTS), "веса взаимно просты с 32"
    with pytest.raises(ValueError):
        codes.checksum_char("VPN-ABCD-EF")  # полезная часть короче семи символов


def test_issued_codes_come_from_the_system_source_not_the_module_generator() -> None:
    """Без явного генератора коды берутся из ``secure_source()`` (``random.SystemRandom``):
    подмена источника на детерминированный делает выдачу предсказуемой — страж ловит и
    подмену класса, и обход ``secure_source`` (ревью раунда 1, C-1)."""
    with mock.patch.object(codes, "secure_source") as source:
        source.return_value = random.Random(0)  # noqa: S311 — намеренно предсказуемый
        first, second = codes.generate_code(), codes.generate_code()
    assert source.call_count == 2, "каждая выдача спрашивает secure_source()"
    replay = random.Random(0)  # noqa: S311
    assert (first, second) == (codes.generate_code(replay), codes.generate_code(replay)), (
        "детерминированный источник даёт детерминированные коды — значит источник используется"
    )
    assert isinstance(codes.secure_source(), random.SystemRandom)
    # Биты у SystemRandom берутся из os.urandom (свой getrandbits); генератор Мерсенна тут
    # не участвует — его getrandbits подменён на ошибку, боевая выдача её не задевает.
    with mock.patch("random.Random.getrandbits", side_effect=AssertionError("mt19937")):
        codes.generate_code()
        with pytest.raises(AssertionError):
            codes.generate_code(random.Random(0))  # noqa: S311
    issued = {codes.generate_code() for _ in range(64)}
    assert len(issued) == 64, "боевая выдача не повторяется"


def test_checksum_ok_never_raises_on_non_strings() -> None:
    for garbage in (
        None,
        42,
        b"VPN-ABCD-EFGM",
        ["VPN"],
        {"code": "x"},
        "\u0000" * 20,
        "V" * 10_000,
    ):
        assert codes.checksum_ok(garbage) is False, repr(garbage)[:40]


def test_csv_rows_escape_and_neutralize() -> None:
    """Экспорт — RFC 4180 (кавычки, запятые в имени тарифа) и защита от формул в таблицах."""
    rows = codes.csv_rows(
        [
            ("VPN-ABCD-EFGM", "2026-12-31T00:00:00+00:00", 'Plan "Basic", 30 days'),
            ("VPN-ABCD-EFGM", "2026-12-31T00:00:00+00:00", '=HYPERLINK("x")'),
            ("VPN-ABCD-EFGM", "", "-1"),
            ("VPN-ABCD-EFGM", "", "\t=1+1"),
        ]
    )
    assert rows[0] == "code,expires_at,plan"
    assert rows[1] == 'VPN-ABCD-EFGM,2026-12-31T00:00:00+00:00,"Plan ""Basic"", 30 days"'
    assert rows[2] == 'VPN-ABCD-EFGM,2026-12-31T00:00:00+00:00,"\'=HYPERLINK(""x"")"'
    assert rows[3] == "VPN-ABCD-EFGM,,'-1"
    assert rows[4] == "VPN-ABCD-EFGM,,'\t=1+1", "ведущая табуляция — тоже формула"
    assert all("\n" not in row and "\r" not in row for row in rows)
