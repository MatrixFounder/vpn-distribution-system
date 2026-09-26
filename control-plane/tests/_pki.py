"""Ключи и сертификаты для тестов enrollment и identity ноды (задача 001.25): одноразовый CA,
CSR ноды на заданной кривой, близнец листа по податливости ECDSA и заголовок ``X-Client-Cert``
в той форме, в какой его передаёт nginx агентского server (``$ssl_client_escaped_cert``).

Экранирование прокси — ``ngx_escape_uri`` в режиме ``NGX_ESCAPE_URI_COMPONENT``: в PEM это
пробел, перевод строки, ``+``, ``/`` и ``=``; дефис остаётся как есть. ``urllib.parse.quote`` с
пустым ``safe`` даёт ту же строку на алфавите PEM (``-_.~`` он не экранирует никогда). Совпадение с
настоящим nginx проверено на стенде (отчёт задачи), а не только здесь.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from urllib.parse import quote

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.x509.oid import NameOID


def make_ca(
    common_name: str = "control-plane test CA",
    *,
    not_before: dt.datetime = dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
    not_after: dt.datetime = dt.datetime(2126, 1, 1, tzinfo=dt.UTC),
    ca: bool = True,
    key_usage: x509.KeyUsage | None = None,
    extended_key_usage: list[x509.ObjectIdentifier] | None = None,
    password: bytes | None = None,
    issued_by: tuple[bytes, bytes] | None = None,
    authority: Callable[[ec.EllipticCurvePrivateKey, x509.Name, int], x509.AuthorityKeyIdentifier]
    | None = None,
    with_authority: bool = True,
    issuer_name: x509.Name | None = None,
    extra_extensions: tuple[tuple[x509.ExtensionType, bool], ...] = (),
    critical_identifiers: tuple[str, ...] = (),
) -> tuple[bytes, bytes]:
    """Ключ P-256 и самоподписанный сертификат CA (``CA:TRUE``, SKI) — PEM, как у
    ``dev-secrets.sh``. Параметры строят негодные пары для стражей старта: срок, ``CA:FALSE``,
    ``keyUsage`` без ``keyCertSign``, ``extendedKeyUsage`` без нужных назначений, ключ под
    паролем, промежуточный CA — подписанный ключом другого CA (``issued_by`` — его PEM ключа и
    сертификата; AKI — от его ключа, как ставят выпускающие CA, если ``with_authority``), AKI по
    выбору теста (``authority`` строит его от собственного ключа, имени издателя и серийного
    номера), имя издателя другой записью (``issuer_name``), дополнительные расширения
    (``extra_extensions`` — пары «расширение, критичность»), SKI и (или) AKI критическими
    (``critical_identifiers`` — ``"ski"``, ``"aki"``)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    signer: ec.EllipticCurvePrivateKey = key
    issuer = name
    if issued_by is not None:
        loaded = serialization.load_pem_private_key(issued_by[0], password=None)
        assert isinstance(loaded, ec.EllipticCurvePrivateKey)
        signer = loaded
        issuer = x509.load_pem_x509_certificate(issued_by[1]).subject
    if issuer_name is not None:
        issuer = issuer_name
    serial = x509.random_serial_number()
    # Срок по умолчанию — фиксированный и широкий, а не от текущего момента: CA отказывает в
    # выпуске вне своего срока, и тест с фиксированным моментом подписи иначе сломался бы сам.
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical="ski" in critical_identifiers,
        )
    )
    if authority is not None:
        builder = builder.add_extension(
            authority(key, issuer, serial), critical="aki" in critical_identifiers
        )
    elif issued_by is not None and with_authority:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()),
            critical=False,
        )
    if key_usage is not None:
        builder = builder.add_extension(key_usage, critical=True)
    for extension, critical in extra_extensions:
        builder = builder.add_extension(extension, critical=critical)
    if extended_key_usage is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage(extended_key_usage), critical=False)
    cert = builder.sign(signer, hashes.SHA256())
    encryption: serialization.KeySerializationEncryption = (
        serialization.BestAvailableEncryption(password)
        if password is not None
        else serialization.NoEncryption()
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
    )
    return key_pem, cert.public_bytes(serialization.Encoding.PEM)


def key_usage(**enabled: bool) -> x509.KeyUsage:
    """``KeyUsage`` с перечисленными назначениями, остальные — выключены."""
    names = (
        "digital_signature",
        "content_commitment",
        "key_encipherment",
        "data_encipherment",
        "key_agreement",
        "key_cert_sign",
        "crl_sign",
    )
    unknown = set(enabled) - set(names)
    if unknown:
        raise ValueError(f"неизвестные назначения keyUsage: {sorted(unknown)}")
    flags = {name: enabled.get(name, False) for name in names}
    return x509.KeyUsage(**flags, encipher_only=False, decipher_only=False)


def make_csr(
    curve: ec.EllipticCurve | None = None,
    *,
    common_name: str = "node",
    extensions: list[tuple[x509.ExtensionType, bool]] | None = None,
    rsa_bits: int | None = None,
) -> str:
    """PEM CSR ноды: по умолчанию P-256; ``rsa_bits`` — ключ RSA вместо EC. Субъект и
    расширения — то, что нода *предлагает*: CA обязан их не копировать."""
    key: ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey
    if rsa_bits is not None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=rsa_bits)
    else:
        key = ec.generate_private_key(curve or ec.SECP256R1())
    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    )
    for extension, critical in extensions or []:
        builder = builder.add_extension(extension, critical=critical)
    return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode()


P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
ECDSA_SHA256 = bytes.fromhex("300a06082a8648ce3d040302")  # AlgorithmIdentifier ecdsa-with-SHA256


def signature_twin(cert: x509.Certificate) -> x509.Certificate:
    """Близнец листа CA на P-256 с подписью ECDSA-SHA256: то же подписанное тело (TBS), подпись
    ``(r, n − s)`` вместо ``(r, s)`` — так же верная, но другие байты DER и другие отпечатки.
    Ключ CA для этого не нужен: сертификат публичен (роаст 001.25, раунд 1)."""
    r, s = decode_dss_signature(cert.signature)
    signature = encode_dss_signature(r, P256_ORDER - s)
    body = cert.tbs_certificate_bytes + ECDSA_SHA256 + _bit_string(signature)
    return x509.load_der_x509_certificate(_sequence(body))


def _length(size: int) -> bytes:
    if size < 0x80:
        return bytes([size])
    body = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _sequence(body: bytes) -> bytes:
    return b"\x30" + _length(len(body)) + body


def _bit_string(body: bytes) -> bytes:
    return b"\x03" + _length(len(body) + 1) + b"\x00" + body


def escaped(cert_pem: str) -> str:
    """Сертификат в форме ``$ssl_client_escaped_cert`` nginx."""
    return quote(cert_pem, safe="")


# Фиксированный лист для тестов раздела ``/agent/v1`` на заглушках чужих служб и для контрактных
# фикстур ``contracts/agent_v1/``: тот самый сертификат, что выдан в ``enroll.json`` по записанному
# там CSR (``InternalCA.sign_csr``, субъект — ``STUB_NODE_ID``) CA фикстур, чей ключ выброшен после
# выпуска; срок — до 2126 года, чтобы фикстуры не протухали. Секрета в нём нет: ключ ноды не
# сохранён, а сертификат публичен по природе. В базе его нет — ноду по нему ищет только настоящая
# ``current_node``, а эти тесты подменяют поиск (``tests/_agent.py::node_as``).
STUB_CLIENT_CERT_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIB1DCCAXqgAwIBAgIUfWAR/fLu9DsE1F3/VFUSZ6ejwLwwCgYIKoZIzj0EAwIw\n"
    "LDEqMCgGA1UEAwwhY29udHJvbC1wbGFuZSBjb250cmFjdCBmaXh0dXJlIENBMCAX\n"
    "DTI2MDkwMTAwMDAwMFoYDzIxMjYwOTAxMDAwMDAwWjAvMS0wKwYDVQQDDCQwMDAw\n"
    "MDAwMC0wMDAwLTcwMDAtODAwMC0wMDAwMDAwMDAwYjEwWTATBgcqhkjOPQIBBggq\n"
    "hkjOPQMBBwNCAAQsO9wx1ddLfFAqjeqj/HwRD3V4XhjSBBFcogKWPm/+AuTQ/qoc\n"
    "Ww/nwLM1S8t2ucicow1F2iubLzO/nafvvWWvo3UwczAMBgNVHRMBAf8EAjAAMA4G\n"
    "A1UdDwEB/wQEAwIHgDATBgNVHSUEDDAKBggrBgEFBQcDAjAdBgNVHQ4EFgQUxTZV\n"
    "oYBhk2ACvOPSqo/ABOSLUxwwHwYDVR0jBBgwFoAUAfH3d/7qBOeAb1rfxzv3BUB2\n"
    "NTswCgYIKoZIzj0EAwIDSAAwRQIhAINW0n5yflaA0inlYNOjY7KS9b/p1uvnUE9e\n"
    "/FZ4fNWJAiBjRmh9tjGFIHt3gTBrg7KHm0zNBHu9xHYej9oij+03VQ==\n"
    "-----END CERTIFICATE-----\n"
)
# Отпечаток — литералом, а не вызовом ``InternalCA.fingerprint``: сверка с тем же вычислением
# не заметила бы, что вычисление сломано.
STUB_CLIENT_FINGERPRINT = "3cc5de2164a77e28808db092325c9e8e147d1442c6ecc5d7857ab93eab17ab6f"
STUB_CLIENT_CERT = escaped(STUB_CLIENT_CERT_PEM)
