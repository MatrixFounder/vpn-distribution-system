"""Внутренний CA (security.md §7.1): выпуск клиентских сертификатов нод при enrollment на 90
дней, отпечаток сертификата для ``node_identities.cert_fingerprint``.

Задача 001.24 — интерфейс ``InternalCA`` и заглушка: ``sign_csr`` проверяет только PEM-рамку
CSR и возвращает фиксированный сертификат, ``ca_pem`` — фиксированный сертификат CA. Ключ CA
из ``CA_KEY_FILE`` (секрет ``ca_key``, PEM prime256v1), разбор CSR и подпись через
``cryptography`` — 001.25. ``fingerprint`` — уже настоящий: SHA-256 от DER (тела PEM) в hex
нижнего регистра, как хранит модель данных §4.2.3.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re

CERT_DAYS = 90  # срок клиентского сертификата ноды (§7.1)
MAX_PEM_CHARS = 64 * 1024  # рамка ноды — единицы килобайт; больше не разбираем

# RFC 7468: строки base64 с окончанием LF или CRLF, пустых строк внутри тела нет. Класс тела не
# содержит перевода строки, а разделитель строк — вне класса: разбор однозначен (нет ветвления
# на длинном несовпадающем входе) и пустое тело не проходит — «рамка без содержимого» не PEM.
_PEM = re.compile(
    r"^-----BEGIN (?P<label>[A-Z][A-Z ]*)-----\n"
    r"(?P<body>[A-Za-z0-9+/=]+(?:\n[A-Za-z0-9+/=]+)*)\n"
    r"-----END (?P<end>[A-Z][A-Z ]*)-----$"
)


def pem_der(pem: str, label: str) -> bytes:
    """DER-тело PEM-блока с меткой ``label`` (``CERTIFICATE``, ``CERTIFICATE REQUEST``).
    Окончания строк CRLF допускаются (RFC 7468 §3). ``ValueError``: не строка, иная или
    несогласованная метка, лишний текст вокруг блока, рамка без тела, битый base64."""
    if not isinstance(pem, str):
        raise ValueError("PEM должен быть строкой")
    if len(pem) > MAX_PEM_CHARS:
        raise ValueError(f"PEM длиннее {MAX_PEM_CHARS} символов")
    text = pem.replace("\r\n", "\n").replace("\r", "\n").strip()
    match = _PEM.match(text)
    if match is None:
        raise ValueError(f"ожидается PEM-блок {label}")
    if match.group("label") != match.group("end"):
        raise ValueError("метки BEGIN и END не совпадают")
    if match.group("label") != label:
        raise ValueError(f"ожидается PEM-блок {label}, получен {match.group('label')}")
    try:
        # Тело — минимум один символ base64 (регулярное выражение), поэтому пустой DER
        # получить нельзя: «рамка без содержимого» отсеивается разбором, а не проверкой ниже.
        return base64.b64decode(match.group("body").replace("\n", ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"PEM-блок {label}: тело не base64") from exc


def _pem(label: str, der: bytes) -> str:
    body = base64.b64encode(der).decode()
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


# Фиксированные значения заглушки: тела — не разбираемые DER, а детерминированные байты;
# рамка PEM и base64 корректны, поэтому ``fingerprint`` работает и на них.
STUB_CA_PEM = _pem("CERTIFICATE", b"control-plane stub CA certificate 001.24".ljust(96, b"\0"))
STUB_CLIENT_CERT_PEM = _pem(
    "CERTIFICATE", b"control-plane stub node client certificate 001.24".ljust(96, b"\0")
)


class InternalCA:
    """CA нод. Заглушка 001.24: ключ не читается, сертификат фиксированный."""

    def __init__(self, ca_pem: str = STUB_CA_PEM) -> None:
        self.ca_pem = ca_pem

    def sign_csr(self, csr_pem: str, days: int = CERT_DAYS) -> str:
        """Клиентский сертификат по CSR на ``days`` дней. Заглушка: CSR обязан быть PEM-блоком
        ``CERTIFICATE REQUEST`` (иначе ``ValueError``), ответ — ``STUB_CLIENT_CERT_PEM``."""
        pem_der(csr_pem, "CERTIFICATE REQUEST")
        if days <= 0:
            raise ValueError("срок сертификата — положительное число дней")
        return STUB_CLIENT_CERT_PEM

    @staticmethod
    def fingerprint(cert_pem: str) -> str:
        """SHA-256 от DER сертификата, hex в нижнем регистре (64 символа)."""
        return hashlib.sha256(pem_der(cert_pem, "CERTIFICATE")).hexdigest()
