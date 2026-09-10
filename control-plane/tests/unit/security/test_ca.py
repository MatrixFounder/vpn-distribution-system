"""``app.security.ca`` (001.24): разбор PEM и отпечаток — настоящие, подпись — заглушка."""

from __future__ import annotations

import base64
import hashlib

import pytest
from app.security import ca

DER = bytes(range(256)) * 3  # произвольные байты «сертификата»
BODY = base64.b64encode(DER).decode()
# Тело длиннее предела и при этом ВАЛИДНЫЙ base64: иначе страж мерил бы испорченный base64, а не
# длину. Размер задан литералом, а не через ``ca.MAX_PEM_CHARS``: вход, вычисленный из
# проверяемой константы, растёт вместе с ней и не краснеет никогда (ревью 001.24, раунд 2).
LONG_BODY = "QUJD" * (16 * 1024)  # 65 536 символов


def pem(label: str, body: str = BODY, width: int = 64, eol: str = "\n") -> str:
    lines = [body[i : i + width] for i in range(0, len(body), width)]
    block = f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"
    return block.replace("\n", eol)


def test_fingerprint_is_sha256_of_der_regardless_of_line_wrapping() -> None:
    expected = hashlib.sha256(DER).hexdigest()
    assert ca.InternalCA.fingerprint(pem("CERTIFICATE")) == expected
    assert ca.InternalCA.fingerprint(pem("CERTIFICATE", width=76)) == expected
    assert ca.InternalCA.fingerprint(pem("CERTIFICATE", width=len(BODY))) == expected
    assert ca.InternalCA.fingerprint("  \n" + pem("CERTIFICATE").rstrip("\n")) == expected, (
        "краевые пробелы и отсутствие завершающего перевода строки — не ошибка"
    )
    assert len(expected) == 64 and expected == expected.lower()


def test_crlf_line_endings_are_accepted_as_rfc_7468_requires() -> None:
    """RFC 7468 §3: строка PEM оканчивается LF или CRLF. CSR, прочитанный из файла с CRLF,
    даёт тот же DER и тот же отпечаток — агент на любой платформе не получает отказ."""
    expected = hashlib.sha256(DER).hexdigest()
    assert ca.InternalCA.fingerprint(pem("CERTIFICATE", eol="\r\n")) == expected
    assert ca.pem_der(pem("CERTIFICATE REQUEST", eol="\r\n"), "CERTIFICATE REQUEST") == DER
    assert ca.InternalCA().sign_csr(pem("CERTIFICATE REQUEST", eol="\r\n"))


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not a pem",
        pem("CERTIFICATE REQUEST"),
        pem("PRIVATE KEY"),
        pem("CERTIFICATE").replace("-----END CERTIFICATE-----", "-----END CERT-----"),
        pem("CERTIFICATE", body="!!!!"),
        pem("CERTIFICATE", body=BODY[:-1]),  # битая длина base64
        "junk\n" + pem("CERTIFICATE"),
        pem("CERTIFICATE") + "trailing",
        pem("CERTIFICATE") + pem("CERTIFICATE"),  # два блока — не один сертификат
        "-----BEGIN CERTIFICATE-----\n\n\n-----END CERTIFICATE-----\n",  # рамка без содержимого
        "-----BEGIN CERTIFICATE-----\nQUJD\n\nREVG\n-----END CERTIFICATE-----\n",  # пустая строка
        pem("CERTIFICATE", body=LONG_BODY),  # длиннее MAX_PEM_CHARS, но валидный base64
    ],
)
def test_fingerprint_rejects_anything_but_one_certificate_block(bad: str) -> None:
    with pytest.raises(ValueError):
        ca.InternalCA.fingerprint(bad)


def test_pem_der_names_the_reason_it_refused() -> None:
    """Сообщение отличает «не та метка» от «пустое тело» и «не base64»: без этого агент 001.53
    чинит не то, что сломано."""
    with pytest.raises(ValueError, match="получен CERTIFICATE REQUEST"):
        ca.pem_der(pem("CERTIFICATE REQUEST"), "CERTIFICATE")
    with pytest.raises(ValueError, match="ожидается PEM-блок CERTIFICATE$"):
        ca.pem_der("-----BEGIN CERTIFICATE-----\n\n-----END CERTIFICATE-----\n", "CERTIFICATE")
    with pytest.raises(ValueError, match="не base64"):
        ca.pem_der("-----BEGIN CERTIFICATE-----\nQUJ\n-----END CERTIFICATE-----\n", "CERTIFICATE")
    with pytest.raises(ValueError, match="BEGIN и END"):
        ca.pem_der(
            "-----BEGIN CERTIFICATE-----\nQUJD\n-----END CERTIFICATE REQUEST-----\n", "CERTIFICATE"
        )
    assert ca.MAX_PEM_CHARS == 64 * 1024, "предел разбора PEM — 64 КБ (тело enrollment nginx)"
    with pytest.raises(ValueError, match="длиннее"):
        ca.pem_der(pem("CERTIFICATE", body=LONG_BODY), "CERTIFICATE")
    assert base64.b64decode(LONG_BODY, validate=True), "тело валидно — отказ именно по длине"
    with pytest.raises(ValueError, match="строкой"):
        ca.pem_der(b"-----BEGIN CERTIFICATE-----", "CERTIFICATE")  # type: ignore[arg-type]


def test_stub_sign_csr_requires_a_csr_and_returns_the_fixed_certificate() -> None:
    authority = ca.InternalCA()
    csr = pem("CERTIFICATE REQUEST")
    assert authority.sign_csr(csr) == ca.STUB_CLIENT_CERT_PEM
    assert authority.sign_csr(csr, days=1) == ca.STUB_CLIENT_CERT_PEM
    assert ca.InternalCA.fingerprint(ca.STUB_CLIENT_CERT_PEM) != ca.InternalCA.fingerprint(
        ca.STUB_CA_PEM
    )
    assert authority.ca_pem == ca.STUB_CA_PEM and ca.CERT_DAYS == 90
    with pytest.raises(ValueError, match="CERTIFICATE REQUEST"):
        authority.sign_csr(pem("CERTIFICATE"))
    with pytest.raises(ValueError):
        authority.sign_csr(csr, days=0)
