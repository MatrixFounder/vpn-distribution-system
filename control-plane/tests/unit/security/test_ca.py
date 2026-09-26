"""``app.security.ca``: разбор PEM и отпечаток (001.24), выпуск листа ноды внутренним CA и загрузка
пары ключ/сертификат (001.25). TC-UNIT-01 описания 001.25 — отпечаток тестового сертификата."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from app.config import SecretError
from app.security import ca
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.name import _ASN1Type
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID, NameOID

from tests._pki import (
    STUB_CLIENT_CERT_PEM,
    STUB_CLIENT_FINGERPRINT,
    key_usage,
    make_ca,
    make_csr,
    signature_twin,
)

DER = bytes(range(256)) * 3  # произвольные байты «сертификата»
BODY = base64.b64encode(DER).decode()
# Тело длиннее предела и при этом ВАЛИДНЫЙ base64: иначе страж мерил бы испорченный base64, а не
# длину. Размер задан литералом, а не через ``ca.MAX_PEM_CHARS``: вход, вычисленный из
# проверяемой константы, растёт вместе с ней и не краснеет никогда (ревью 001.24, раунд 2).
LONG_BODY = "QUJD" * (16 * 1024)  # 65 536 символов
NODE = uuid.UUID("0199b1a2-0000-7000-8000-00000000c0de")
NOW = dt.datetime(2026, 9, 23, 10, 15, 30, 123456, tzinfo=dt.UTC)


def pem(label: str, body: str = BODY, width: int = 64, eol: str = "\n") -> str:
    lines = [body[i : i + width] for i in range(0, len(body), width)]
    block = f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"
    return block.replace("\n", eol)


@pytest.fixture(scope="module")
def authority() -> tuple[ca.InternalCA, x509.Certificate]:
    key_pem, cert_pem = make_ca()
    return ca.InternalCA.from_pem(key_pem, cert_pem), x509.load_pem_x509_certificate(cert_pem)


def issue(authority: ca.InternalCA, csr: str | None = None) -> x509.Certificate:
    issued = authority.sign_csr(csr or make_csr(), node_id=NODE, now=NOW)
    return x509.load_pem_x509_certificate(issued.pem.encode())


def test_tc_unit_01_fingerprint_is_lowercase_sha256_without_separators() -> None:
    """TC-UNIT-01: отпечаток тестового сертификата — SHA-256 от DER, hex в нижнем регистре без
    двоеточий (так его хранит ``node_identities.cert_fingerprint``, §4.2.3). Ожидание — литерал,
    а не то же вычисление: сверка с ``hashlib`` над тем же DER не заметила бы, что отпечаток
    берётся не от того."""
    assert ca.InternalCA.fingerprint(STUB_CLIENT_CERT_PEM) == STUB_CLIENT_FINGERPRINT
    assert len(STUB_CLIENT_FINGERPRINT) == 64
    assert set(STUB_CLIENT_FINGERPRINT) <= set("0123456789abcdef"), "нижний регистр, без «:»"


def test_fingerprint_is_sha256_of_der_regardless_of_line_wrapping() -> None:
    expected = hashlib.sha256(DER).hexdigest()
    assert ca.InternalCA.fingerprint(pem("CERTIFICATE")) == expected
    assert ca.InternalCA.fingerprint(pem("CERTIFICATE", width=76)) == expected
    assert ca.InternalCA.fingerprint(pem("CERTIFICATE", width=len(BODY))) == expected
    assert ca.InternalCA.fingerprint("  \n" + pem("CERTIFICATE").rstrip("\n")) == expected, (
        "краевые пробелы и отсутствие завершающего перевода строки — не ошибка"
    )
    assert ca.fingerprint_der(DER) == expected


def test_crlf_line_endings_are_accepted_as_rfc_7468_requires(
    authority: tuple[ca.InternalCA, x509.Certificate],
) -> None:
    """RFC 7468 §3: строка PEM оканчивается LF или CRLF. CSR, прочитанный из файла с CRLF,
    даёт тот же DER и тот же отпечаток — агент на любой платформе не получает отказ."""
    expected = hashlib.sha256(DER).hexdigest()
    assert ca.InternalCA.fingerprint(pem("CERTIFICATE", eol="\r\n")) == expected
    assert ca.pem_der(pem("CERTIFICATE REQUEST", eol="\r\n"), "CERTIFICATE REQUEST") == DER
    assert issue(authority[0], make_csr().replace("\n", "\r\n"))


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


def test_the_leaf_carries_the_profile_the_ca_decides(
    authority: tuple[ca.InternalCA, x509.Certificate],
) -> None:
    """Профиль листа (security.md §7.1, 001.33): ``CA:FALSE`` и ``digitalSignature`` —
    critical, единственное назначение — ``clientAuth``; субъект — ровно идентификатор ноды,
    издатель — субъект CA, идентификатор ключа издателя — SKI сертификата CA. Лист подписан CA
    напрямую (глубина 0 у nginx — лист и якорь), открытый ключ — ключ CSR."""
    internal, root = authority
    csr_pem = make_csr()
    leaf = issue(internal, csr_pem)
    leaf.verify_directly_issued_by(root)
    der, spki = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    assert leaf.public_key().public_bytes(der, spki) == csr.public_key().public_bytes(der, spki)
    assert [(a.oid, a.value) for a in leaf.subject] == [(NameOID.COMMON_NAME, str(NODE))]
    assert leaf.issuer == root.subject
    constraints = leaf.extensions.get_extension_for_class(x509.BasicConstraints)
    assert constraints.critical and constraints.value.ca is False
    usage = leaf.extensions.get_extension_for_class(x509.KeyUsage)
    assert usage.critical and usage.value == x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=False,
        crl_sign=False,
        encipher_only=False,
        decipher_only=False,
    ), "назначение ключа — ровно подпись, все прочие биты сняты"
    purposes = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(purposes) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    ski = root.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    aki = leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
    assert aki.key_identifier == ski.digest
    assert {e.oid for e in leaf.extensions} == {
        x509.oid.ExtensionOID.BASIC_CONSTRAINTS,
        x509.oid.ExtensionOID.KEY_USAGE,
        x509.oid.ExtensionOID.EXTENDED_KEY_USAGE,
        x509.oid.ExtensionOID.SUBJECT_KEY_IDENTIFIER,
        x509.oid.ExtensionOID.AUTHORITY_KEY_IDENTIFIER,
    }, "ни одного расширения сверх профиля"


def test_the_leaf_lives_ninety_days_from_the_moment_of_issue(
    authority: tuple[ca.InternalCA, x509.Certificate],
) -> None:
    """Срок — ``CERT_DAYS`` = 90 дней (§7.1) от момента, который передал вызывающий (момент
    операции обмена — ``statement_timestamp()`` после блокировки ноды), с отброшенными
    микросекундами: X.509 хранит секунды, и сроки строки
    identity обязаны совпасть с сертификатом до секунды. Значение срока закреплено литералом."""
    assert ca.CERT_DAYS == 90
    issued = authority[0].sign_csr(make_csr(), node_id=NODE, now=NOW)
    start = dt.datetime(2026, 9, 23, 10, 15, 30, tzinfo=dt.UTC)
    assert (issued.not_before, issued.not_after) == (start, start + dt.timedelta(days=90))
    leaf = x509.load_pem_x509_certificate(issued.pem.encode())
    assert (leaf.not_valid_before_utc, leaf.not_valid_after_utc) == (
        issued.not_before,
        issued.not_after,
    )
    assert issued.fingerprint == ca.InternalCA.fingerprint(issued.pem)
    shifted = authority[0].sign_csr(
        make_csr(), node_id=NODE, now=NOW.astimezone(dt.timezone(dt.timedelta(hours=3)))
    )
    assert shifted.not_before == start, "момент с другим поясом — тот же момент"


def test_what_the_node_proposes_in_the_csr_is_not_copied(
    authority: tuple[ca.InternalCA, x509.Certificate],
) -> None:
    """Субъект и расширения CSR предлагает проверяемая сторона. Лист с ``CA:TRUE`` и
    ``keyCertSign`` из CSR подписывал бы «сиблингов» и множил отпечатки — ключи всех пределов
    прокси; чужой CN в субъекте называл бы ноду другой нодой."""
    greedy = make_csr(
        common_name="00000000-0000-7000-8000-000000000bad",
        extensions=[
            (x509.BasicConstraints(ca=True, path_length=None), True),
            (
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                True,
            ),
            (x509.SubjectAlternativeName([x509.DNSName("control-plane.example")]), False),
        ],
    )
    leaf = issue(authority[0], greedy)
    assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert not leaf.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign
    with pytest.raises(x509.ExtensionNotFound):
        leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert leaf.subject.rfc4514_string() == f"CN={NODE}"


def test_serial_numbers_are_random_and_positive(
    authority: tuple[ca.InternalCA, x509.Certificate],
) -> None:
    """Серийный номер — случайный положительный (RFC 5280 §4.1.2.2): одинаковый вход даёт
    разные листы, и префикс подписываемого сертификата нода не предсказывает."""
    csr = make_csr()
    first, second = issue(authority[0], csr), issue(authority[0], csr)
    assert first.serial_number != second.serial_number
    assert first.serial_number > 0 and first.serial_number.bit_length() > 64


@pytest.mark.parametrize(
    "csr",
    [
        pytest.param(make_csr(ec.SECP384R1()), id="P-384"),
        pytest.param(make_csr(rsa_bits=2048), id="RSA"),
        pytest.param(
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "node")]))
            .sign(ed25519.Ed25519PrivateKey.generate(), None)
            .public_bytes(serialization.Encoding.PEM)
            .decode(),
            id="Ed25519",
        ),
        pytest.param(pem("CERTIFICATE REQUEST"), id="не DER"),
        pytest.param(STUB_CLIENT_CERT_PEM, id="сертификат вместо CSR"),
    ],
)
def test_a_csr_the_ca_does_not_accept_is_rejected_as_such(
    authority: tuple[ca.InternalCA, x509.Certificate], csr: str
) -> None:
    """Ключ ноды — только P-256; не разбираемый CSR и чужая метка — отказ CA
    (``CsrRejectedError``: маршрут отвечает 422 ``invalid_csr``), а не сбой сервера."""
    with pytest.raises(ca.CsrRejectedError):
        authority[0].sign_csr(csr, node_id=NODE, now=NOW)


def test_a_csr_whose_signature_does_not_match_its_key_is_rejected(
    authority: tuple[ca.InternalCA, x509.Certificate],
) -> None:
    """Подпись CSR доказывает владение ключом: CSR, чья подпись не сходится с ключом в его теле
    (здесь — испорченный байт подписи), CA не подписывает, иначе лист выдавался бы под ключ,
    владение которым не доказано."""
    der = bytearray(
        x509.load_pem_x509_csr(make_csr().encode()).public_bytes(serialization.Encoding.DER)
    )
    der[-5] ^= 0x01  # байт подписи
    tampered = x509.load_der_x509_csr(bytes(der)).public_bytes(serialization.Encoding.PEM).decode()
    with pytest.raises(ca.CsrRejectedError, match="подпись"):
        authority[0].sign_csr(tampered, node_id=NODE, now=NOW)


def test_call_errors_are_not_passed_off_as_the_nodes_fault(
    authority: tuple[ca.InternalCA, x509.Certificate],
) -> None:
    """Неположительный срок и момент без часового пояса — ошибка вызывающего кода: обычная
    ``ValueError``, не ``CsrRejectedError`` — маршрут не превратит её в 422 для ноды."""
    for bad in (
        {"now": NOW, "days": 0},
        {"now": NOW.replace(tzinfo=None), "days": 90},
    ):
        with pytest.raises(ValueError) as caught:
            authority[0].sign_csr(make_csr(), node_id=NODE, **bad)  # type: ignore[arg-type]
        assert not isinstance(caught.value, ca.CsrRejectedError), bad


def test_the_ca_refuses_a_key_that_is_not_its_certificates(tmp_path: Path) -> None:
    """Ключ не от сертификата давал бы листы, которые nginx отвергнет у всего парка, — ошибка
    загрузки (``SecretError`` у файлов) с путями и без содержимого ключа."""
    key_pem, cert_pem = make_ca()
    other_key_pem, _ = make_ca()
    with pytest.raises(ValueError, match="не подходит"):
        ca.InternalCA.from_pem(other_key_pem, cert_pem)
    key_file, cert_file = tmp_path / "ca_key", tmp_path / "ca_cert"
    key_file.write_bytes(other_key_pem)
    cert_file.write_bytes(cert_pem)
    with pytest.raises(SecretError, match="негоден") as caught:
        ca.InternalCA.from_files(str(key_file), str(cert_file))
    assert "PRIVATE KEY" not in str(caught.value)
    with pytest.raises(SecretError, match="недоступен"):
        ca.InternalCA.from_files(str(tmp_path / "нет"), str(cert_file))
    key_file.write_bytes(key_pem)
    assert ca.InternalCA.from_files(str(key_file), str(cert_file)).ca_pem == cert_pem.decode()


def test_the_ca_key_is_p256(tmp_path: Path) -> None:
    """Ключ CA другой кривой — отказ загрузки: листы подписываются ключом CA, и его стойкость —
    потолок стойкости всех identity парка (``secrets/README.md``: prime256v1)."""
    key = ec.generate_private_key(ec.SECP384R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "p384 CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW)
        .not_valid_after(NOW + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    with pytest.raises(ValueError, match="P-256"):
        ca.InternalCA(key, cert)


def test_a_ca_without_a_key_identifier_still_names_itself_to_the_leaf() -> None:
    """Сертификат CA без SKI (выпущенный не ``openssl req -x509``): идентификатор ключа
    издателя в листе считается от открытого ключа CA — тем же способом, что у openssl."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bare CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - dt.timedelta(days=1))
        .not_valid_after(NOW + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    leaf = issue(ca.InternalCA(key, cert))
    aki = leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
    expected = x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key())
    assert aki.key_identifier == expected.key_identifier
    leaf.verify_directly_issued_by(cert)


def test_get_ca_reads_the_files_named_by_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``get_ca`` — CA процесса из ``CA_KEY_FILE``/``CA_CERT_FILE``; без них — ``SecretError``
    (роль ``api`` без CA не выпустит ни одной identity, и об этом должно быть сказано)."""
    key_pem, cert_pem = make_ca()
    (tmp_path / "key").write_bytes(key_pem)
    (tmp_path / "cert").write_bytes(cert_pem)
    monkeypatch.setenv("CA_KEY_FILE", str(tmp_path / "key"))
    monkeypatch.setenv("CA_CERT_FILE", str(tmp_path / "cert"))
    loaded = ca.get_ca()
    assert loaded.ca_pem == cert_pem.decode()
    assert ca.get_ca() is loaded, "пара читается один раз на процесс"
    monkeypatch.delenv("CA_CERT_FILE")
    with pytest.raises(SecretError, match="CA_CERT_FILE"):
        ca.get_ca()


def _swap(der: bytes, old: str, new: str) -> str:
    """CSR с заменённым OID: подпись не пересчитывается — разбор до неё не доходит."""
    blob = der.replace(bytes.fromhex(old), bytes.fromhex(new))
    assert blob != der, "OID найден и заменён"
    return x509.load_der_x509_csr(blob).public_bytes(serialization.Encoding.PEM).decode()


def test_an_algorithm_cryptography_does_not_know_is_a_rejected_csr(
    authority: tuple[ca.InternalCA, x509.Certificate],
) -> None:
    """Кривая или тип ключа, которых ``cryptography`` не знает, дают ``UnsupportedAlgorithm``, а
    не ``ValueError`` (проверено на 50.0.1: OID кривой 1.2.840.10045.3.1.9 и типа ключа
    1.2.840.10045.2.9). Без перехвата такой CSR ушёл бы в 500 как сбой сервера — это отказ CA."""
    der = x509.load_pem_x509_csr(make_csr().encode()).public_bytes(serialization.Encoding.DER)
    for old, new in (
        ("06082a8648ce3d030107", "06082a8648ce3d030109"),  # prime256v1 → неизвестная кривая
        ("06072a8648ce3d0201", "06072a8648ce3d0209"),  # id-ecPublicKey → неизвестный тип
    ):
        with pytest.raises(ca.CsrRejectedError, match="не разобран"):
            authority[0].sign_csr(_swap(der, old, new), node_id=NODE, now=NOW)


def test_the_key_type_is_checked_before_the_signature(
    authority: tuple[ca.InternalCA, x509.Certificate], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Порядок проверок CSR — от дешёвой к дорогой: ключ не P-256 отвергается раньше, чем
    проверяется подпись. Иначе держатель годного токена платил бы циклу событий проверкой подписи
    RSA-16384 на каждом повторе (отказ CA токен не гасит). Разобранный CSR подменён: его подпись
    обращением к ней валит тест."""

    class RsaCsr:
        def public_key(self) -> object:
            return rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()

        @property
        def is_signature_valid(self) -> bool:
            raise AssertionError("подпись проверена раньше типа ключа")

    monkeypatch.setattr(x509, "load_der_x509_csr", lambda der: RsaCsr())
    with pytest.raises(ca.CsrRejectedError, match="P-256"):
        authority[0].sign_csr(make_csr(), node_id=NODE, now=NOW)


def _ca_cert(
    key: ec.EllipticCurvePrivateKey,
    *,
    ca: bool | None = True,
    not_before: dt.datetime = dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
    not_after: dt.datetime = dt.datetime(2126, 1, 1, tzinfo=dt.UTC),
) -> x509.Certificate:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ca under test")])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
    )
    if ca is not None:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=ca, path_length=None), critical=True
        )
    return builder.sign(key, hashes.SHA256())


def test_a_certificate_that_is_not_a_ca_is_refused() -> None:
    """Сертификат без ``basicConstraints CA:TRUE`` (или с ``CA:FALSE``) в роли CA — ошибка
    загрузки: подписанные им листы OpenSSL у клиента не примет, и отказ пришёлся бы на весь парк."""
    key = ec.generate_private_key(ec.SECP256R1())
    for flag in (None, False):
        with pytest.raises(ValueError, match="CA:TRUE"):
            ca.InternalCA(key, _ca_cert(key, ca=flag))


def test_a_leaf_does_not_outlive_its_ca_and_an_expired_ca_issues_nothing() -> None:
    """Срок листа — не дальше срока CA: лист, переживший CA, nginx отвергнет, а identity в базе
    (срок которой — срок листа) считалась бы действующей. CA вне своего срока не выпускает ничего —
    это ``ValueError`` конфигурации (500), а не ``CsrRejectedError`` (вина ноды)."""
    key = ec.generate_private_key(ec.SECP256R1())
    ca_end = (NOW + dt.timedelta(days=30)).replace(microsecond=0)  # X.509 хранит секунды
    short = ca.InternalCA(key, _ca_cert(key, not_after=ca_end))
    issued = short.sign_csr(make_csr(), node_id=NODE, now=NOW)
    assert issued.not_after == ca_end, "90 дней обрезаны сроком CA"
    for moment in (ca_end, ca_end + dt.timedelta(days=1)):
        with pytest.raises(ValueError, match="вне своего срока") as caught:
            short.sign_csr(make_csr(), node_id=NODE, now=moment)
        assert not isinstance(caught.value, ca.CsrRejectedError)
    future = ca.InternalCA(key, _ca_cert(key, not_before=NOW + dt.timedelta(days=1)))
    with pytest.raises(ValueError, match="вне своего срока"):
        future.sign_csr(make_csr(), node_id=NODE, now=NOW)


def test_a_password_protected_ca_key_is_a_secret_error(tmp_path: Path) -> None:
    """Ключ CA под паролем ``cryptography`` отвергает ``TypeError``: загрузка обязана свести его
    к ``SecretError`` с путями, а не пропустить сырой ошибкой в 500 без объяснения."""
    key_pem, cert_pem = make_ca()
    key = serialization.load_pem_private_key(key_pem, password=None)
    locked = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"secret"),
    )
    with pytest.raises(ValueError, match="не читается"):
        ca.InternalCA.from_pem(locked, cert_pem)
    (tmp_path / "key").write_bytes(locked)
    (tmp_path / "cert").write_bytes(cert_pem)
    with pytest.raises(SecretError, match="негоден"):
        ca.InternalCA.from_files(str(tmp_path / "key"), str(tmp_path / "cert"))


def test_every_leaf_has_a_twin_that_differs_only_in_its_bytes(
    authority: tuple[ca.InternalCA, x509.Certificate],
) -> None:
    """Податливость ECDSA (роаст 001.25, раунд 1): подпись ``(r, n − s)`` так же верна, как
    ``(r, s)``, и держатель листа получает без ключа CA второй сертификат, который OpenSSL
    принимает (проверено на 3.0.13 стенда). Тело (TBS) и серийный номер у близнецов общие,
    DER — разный, то есть разные и SHA-1 ``$ssl_client_fingerprint``, и SHA-256 identity.
    Следствия закреплены в других местах: identity близнеца не находит (401), а пределы прокси
    ключуются серийным номером, а не отпечатком (``test_proxy_contract.py``), — иначе держатель
    одного листа имел бы два бюджета."""
    internal, root = authority
    leaf = issue(internal)
    twin = signature_twin(leaf)
    twin.verify_directly_issued_by(root)
    assert twin.signature != leaf.signature
    assert twin.serial_number == leaf.serial_number
    assert twin.tbs_certificate_bytes == leaf.tbs_certificate_bytes
    der = serialization.Encoding.DER
    # SHA-1 — не выбор теста, а то, чем nginx считает $ssl_client_fingerprint.
    nginx_twin = hashlib.sha1(twin.public_bytes(der)).digest()  # noqa: S324
    nginx_leaf = hashlib.sha1(leaf.public_bytes(der)).digest()  # noqa: S324
    assert nginx_twin != nginx_leaf
    assert ca.fingerprint_der(twin.public_bytes(der)) != ca.fingerprint_der(leaf.public_bytes(der))


def test_the_leaf_serial_is_recorded_as_nginx_prints_it(
    authority: tuple[ca.InternalCA, x509.Certificate],
) -> None:
    """Серийный номер листа записывается в ``node_identities.cert_serial`` в той форме, в какой
    его показывает nginx (``$ssl_client_serial``), — по нему ключуются пределы прокси и будет
    ключоваться карта отказа 001.66 (роаст 001.25, раунд 2). Ожидания — литералы из вывода
    ``openssl x509 -noout -serial`` (OpenSSL 3.0.13 стенда и LibreSSL печатают одинаково), а не то
    же вычисление: ведущий ноль байта сохраняется, регистр — верхний."""
    issued = authority[0].sign_csr(make_csr(), node_id=NODE, now=NOW)
    leaf = x509.load_pem_x509_certificate(issued.pem.encode())
    assert issued.serial == ca.serial_hex(leaf.serial_number)
    fixture = x509.load_pem_x509_certificate(STUB_CLIENT_CERT_PEM.encode())
    assert ca.serial_hex(fixture.serial_number) == "7D6011FDF2EEF43B04D45DFF54551267A7A3C0BC"
    assert ca.serial_hex(0x0A1B2C) == "0A1B2C", "ведущий ноль байта — как у openssl"
    assert ca.serial_hex(0x80) == "80"
    assert ca.serial_hex(0x0100) == "0100"
    with pytest.raises(ValueError, match="положительное"):
        ca.serial_hex(0)


def test_a_ca_whose_key_usage_forbids_signing_certificates_is_refused() -> None:
    """``keyUsage`` необязателен (у dev CA из ``openssl req -x509`` его нет), но если он есть и
    в нём нет ``keyCertSign``, листы этого CA не примет ни nginx, ни агент — такой CA не
    загружается (роаст 001.25, раунд 2)."""
    signing_forbidden = make_ca(key_usage=key_usage(digital_signature=True, crl_sign=True))
    with pytest.raises(ValueError, match="keyCertSign"):
        ca.InternalCA.from_pem(*signing_forbidden)
    ca.InternalCA.from_pem(*make_ca(key_usage=key_usage(key_cert_sign=True, crl_sign=True)))
    ca.InternalCA.from_pem(*make_ca())  # без keyUsage — как dev CA стенда


def test_loading_refuses_a_ca_outside_its_validity_and_warns_before_it_clamps(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Загрузка на старте ``api`` (``_load``) сверяет срок CA с текущим моментом: истёкший или
    ещё не начавшийся CA — ``SecretError``, а не здоровый процесс с 500 на каждом обмене (роаст
    001.25, раунд 2). CA, истекающий раньше ``CERT_DAYS``, загружается, но укорачивает каждый
    новый лист — об этом предупреждение."""
    now = dt.datetime.now(dt.UTC)
    cases = {
        "expired": make_ca(
            not_before=now - dt.timedelta(days=400), not_after=now - dt.timedelta(days=1)
        ),
        "future": make_ca(
            not_before=now + dt.timedelta(days=1), not_after=now + dt.timedelta(days=400)
        ),
        "short": make_ca(
            not_before=now - dt.timedelta(days=1), not_after=now + dt.timedelta(days=30)
        ),
    }
    paths = {}
    for name, (key_pem, cert_pem) in cases.items():
        (tmp_path / f"{name}.key").write_bytes(key_pem)
        (tmp_path / f"{name}.crt").write_bytes(cert_pem)
        paths[name] = (str(tmp_path / f"{name}.key"), str(tmp_path / f"{name}.crt"))
    for name in ("expired", "future"):
        with pytest.raises(SecretError, match="вне своего срока"):
            ca._load(*paths[name])
    with caplog.at_level("WARNING", logger="app.security.ca"):
        loaded = ca._load(*paths["short"])
    assert loaded.not_valid_after - now < dt.timedelta(days=ca.CERT_DAYS)
    assert any("укорачиваются" in record.getMessage() for record in caplog.records), caplog.text


def _relabeled(pem: bytes, label: bytes) -> bytes:
    return pem.replace(b"BEGIN CERTIFICATE", b"BEGIN " + label).replace(
        b"END CERTIFICATE", b"END " + label
    )


# Случаи отказов ниже — параметры, а не цикл в одном тесте: случаи держат разные проверки, и в
# цикле первый упавший скрывал бы следующие — посадка, снявшая проверку позднего случая,
# краснела бы на раннем, по чужой причине (S142, S174 — роаст 001.25, раунд 8).
@pytest.mark.parametrize(
    "bundle",
    ["two", "other-first", "trusted-label", "x509-label", "with-key", "none"],
)
def test_a_ca_file_with_more_than_one_certificate_is_refused(tmp_path: Path, bundle: str) -> None:
    """nginx доверяет каждому сертификату ``ca.crt``, а CA подписывает первым: лишний сертификат в
    пачке разделил бы «одну пару» прокси и CA молча (роаст 001.25, раунд 3) — файл сертификата CA
    обязан держать ровно один блок PEM, и это ``CERTIFICATE``. OpenSSL (nginx) читает из файла
    и блоки ``TRUSTED CERTIFICATE`` и ``X509 CERTIFICATE``: второй CA под такой меткой счёт
    одной метки не видел (роаст 001.25, раунд 4)."""
    key_pem, cert_pem = make_ca()
    _, other_cert = make_ca("another CA")
    bundles = {
        "two": cert_pem + other_cert,
        "other-first": other_cert + cert_pem,
        "trusted-label": cert_pem + _relabeled(other_cert, b"TRUSTED CERTIFICATE"),
        "x509-label": cert_pem + _relabeled(other_cert, b"X509 CERTIFICATE"),
        "with-key": cert_pem + key_pem,
        "none": cert_pem.replace(b"-----BEGIN ", b"-----BEGIN-"),
    }
    reason = "ни одного" if bundle == "none" else "ровно один блок PEM CERTIFICATE"
    with pytest.raises(ValueError, match=reason):
        ca.InternalCA.from_pem(key_pem, bundles[bundle])
    (tmp_path / "key").write_bytes(key_pem)
    (tmp_path / "bundle").write_bytes(bundles[bundle])
    with pytest.raises(SecretError, match=reason):
        ca.InternalCA.from_files(str(tmp_path / "key"), str(tmp_path / "bundle"))


def test_a_ca_file_with_one_certificate_is_loaded() -> None:
    """Ровно один блок ``CERTIFICATE`` своей пары — загружается."""
    ca.InternalCA.from_pem(*make_ca())


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("intermediate", "не самоподписанный"),
        ("foreign-signed", "не подписан собственным ключом"),
        ("foreign-signed-without-aki", "не подписан собственным ключом"),
    ],
)
def test_the_ca_is_a_self_signed_anchor(case: str, reason: str) -> None:
    """nginx проверяет лист ноды с глубиной 0 до сертификата ``ca.crt`` и без частичных цепочек:
    промежуточный CA, выпущенный чужим корнем, дал бы здоровый старт ``api`` и 400 всему парку на
    прокси — такой CA не загружается (роаст 001.25, раунд 5). Совпадения имён мало: сертификат с
    собственным именем издателя, подписанный чужим ключом (и с AKI того ключа, как выпускают CA),
    — не корень: OpenSSL цепочку на нём не замкнёт. Подпись собственным ключом загрузчик сверяет и
    у сертификата без AKI — строже OpenSSL, который подпись якоря не проверяет: корень, которым CA
    себя объявляет, обязан им быть; этот случай отвергает только проверка подписи (роаст раунда
    7). Отказ промежуточному CA держат две проверки — сверка издателя с субъектом и сравнение
    имён в ``verify_directly_issued_by``; первая даёт ему точное сообщение (раунд 8)."""
    root = make_ca("root CA")
    pairs = {
        "intermediate": make_ca("intermediate CA", issued_by=root),
        "foreign-signed": make_ca("root CA", issued_by=root),
        "foreign-signed-without-aki": make_ca("root CA", issued_by=root, with_authority=False),
    }
    with pytest.raises(ValueError, match=reason):
        ca.InternalCA.from_pem(*pairs[case])
    ca.InternalCA.from_pem(*root)


def test_the_ca_names_itself_byte_for_byte() -> None:
    """Имя издателя и субъекта сверяются по DER: ``cryptography`` считает равными
    NumericString и UTF8String с одним текстом, а OpenSSL — нет (каноническая форма сохраняет тип
    NumericString), и CA, «самовыпущенный» для загрузчика, якорем для nginx не был бы (роаст
    001.25, раунд 7). Тот же отказ даёт и сравнение имён в ``verify_directly_issued_by`` — исход
    сверка по DER решает у DirectoryName в AKI (тест ниже)."""
    numeric = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "123", _type=_ASN1Type.NumericString)]
    )
    with pytest.raises(ValueError, match="не самоподписанный"):
        ca.InternalCA.from_pem(*make_ca("123", issuer_name=numeric))
    ca.InternalCA.from_pem(*make_ca("123"))


_PRIVATE = x509.UnrecognizedExtension(x509.ObjectIdentifier("1.3.6.1.4.1.55555.1"), b"\x05\x00")
# Netscape Cert Type только с sslServer: без sslCA OpenSSL (check_ssl_ca) не принимает такой
# якорь для проверки клиента.
_NETSCAPE_SERVER = x509.UnrecognizedExtension(
    x509.ObjectIdentifier("2.16.840.1.113730.1.1"), b"\x03\x02\x06\x40"
)
_NODES_ONLY = x509.NameConstraints(
    permitted_subtrees=[
        x509.DirectoryName(x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "nodes")]))
    ],
    excluded_subtrees=None,
)
_EXPLICIT_POLICY = x509.PolicyConstraints(require_explicit_policy=0, inhibit_policy_mapping=None)
_ALT_NAME = x509.SubjectAlternativeName([x509.DNSName("ca.example.test")])


@pytest.mark.parametrize(
    ("extension", "critical", "reason"),
    [
        (_PRIVATE, True, "вне списка разрешённых"),
        (_PRIVATE, False, "вне списка разрешённых"),
        (_NETSCAPE_SERVER, False, "вне списка разрешённых"),
        (_NODES_ONLY, False, "вне списка разрешённых"),
        (_EXPLICIT_POLICY, False, "вне списка разрешённых"),
        (_ALT_NAME, False, "вне списка разрешённых"),
    ],
    ids=[
        "private-critical",
        "private",
        "netscape-cert-type",
        "name-constraints",
        "policy-constraints",
        "subject-alt-name",
    ],
)
def test_the_ca_carries_only_allowlisted_extensions(
    extension: x509.ExtensionType, critical: bool, reason: str
) -> None:
    """Расширения CA — список разрешённых: ``basicConstraints``, ``keyUsage``,
    ``extendedKeyUsage`` (их загрузчик сверяет сам) и некритические SKI и AKI. Любое другое —
    отказ, а не догадка о том, как OpenSSL применит его к якорю: критическое незнакомое —
    «unhandled critical extension» на каждой цепочке, ограничения имён отвергли бы листы нод,
    Netscape Cert Type без sslCA делает якорь негодным для проверки клиента, остальное проверить
    нечем. Здоровый старт ``api`` с таким CA дал бы 400 всему парку на прокси (роаст 001.25,
    раунды 7–8)."""
    with pytest.raises(ValueError, match=reason):
        ca.InternalCA.from_pem(*make_ca(extra_extensions=((extension, critical),)))


@pytest.mark.parametrize(
    "critical", [("ski", "aki"), ("ski",), ("aki",)], ids=["ski-aki", "ski", "aki"]
)
def test_critical_key_identifiers_are_refused(critical: tuple[str, ...]) -> None:
    """SKI и AKI OpenSSL критическими не поддерживает — «unhandled critical extension» на каждой
    цепочке: критический SKI или AKI у CA — отказ, некритические — загружаются. Случай на
    каждое правило: при критических обоих первым отказывает SKI, и правило AKI держит только
    случай ``[aki]`` (роаст 001.25, раунд 9)."""
    root = make_ca("root CA")

    def itself(
        key: ec.EllipticCurvePrivateKey, issuer: x509.Name, serial: int
    ) -> x509.AuthorityKeyIdentifier:
        return x509.AuthorityKeyIdentifier(_own_key_id(key), [x509.DirectoryName(issuer)], serial)

    authority = itself if "aki" in critical else None
    with pytest.raises(ValueError, match="критическое"):
        ca.InternalCA.from_pem(*make_ca(authority=authority, critical_identifiers=critical))
    ca.InternalCA.from_pem(*make_ca(authority=authority))
    ca.InternalCA.from_pem(*root)


def _v2_certificate(cert_pem: bytes) -> bytes:
    """Тот же сертификат с полем версии v2 (значение 1): ``cryptography`` такой не читает."""
    der = x509.load_pem_x509_certificate(cert_pem).public_bytes(serialization.Encoding.DER)
    version = bytes.fromhex("a003020102")
    assert der.count(version) == 1
    body = base64.encodebytes(der.replace(version, bytes.fromhex("a003020101")))
    return b"-----BEGIN CERTIFICATE-----\n" + body + b"-----END CERTIFICATE-----\n"


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("duplicate-extension", "повторено расширение"),
        ("unsupported-name", "UnsupportedGeneralNameType"),
        ("unknown-signature", "не подписан собственным ключом"),
        ("unknown-key", "ключ сертификата CA не читается"),
        ("v2", "InvalidVersion"),
    ],
)
def test_malformed_ca_certificates_are_refused_as_value_errors(case: str, reason: str) -> None:
    """Исключения ``cryptography`` при разборе и проверке сертификата CA, которые не
    ``ValueError``: повторённое расширение (``DuplicateExtension``), имя неподдержанного вида в
    AKI или SAN (``UnsupportedGeneralNameType``), неизвестный алгоритм подписи или ключа
    (``UnsupportedAlgorithm``), версия не v1/v3 (``InvalidVersion``). Без перевода они ушли бы
    мимо ``SecretError`` сырой трассировкой старта (роаст 001.25, раунды 7–8)."""
    key_pem, cert_pem = make_ca()
    if case == "v2":
        with pytest.raises(ValueError, match=reason):
            ca.InternalCA.from_pem(key_pem, _v2_certificate(cert_pem))
        return
    key = serialization.load_pem_private_key(key_pem, password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    real = x509.load_pem_x509_certificate(cert_pem)

    class Broken:
        def __getattr__(self, name: str) -> object:
            return getattr(real, name)

        @property
        def extensions(self) -> x509.Extensions:
            if case == "duplicate-extension":
                raise x509.DuplicateExtension("повтор", ExtensionOID.BASIC_CONSTRAINTS)
            if case == "unsupported-name":
                raise x509.UnsupportedGeneralNameType("x400Address")
            return real.extensions

        def verify_directly_issued_by(self, issuer: x509.Certificate) -> None:
            if case == "unknown-signature":
                raise UnsupportedAlgorithm("неизвестный алгоритм подписи")
            real.verify_directly_issued_by(real)

        def public_key(self) -> object:
            if case == "unknown-key":
                raise UnsupportedAlgorithm("неизвестный алгоритм ключа")
            return real.public_key()

    with pytest.raises(ValueError, match=reason):
        ca.InternalCA(key, cast(x509.Certificate, Broken()))


def _own_key_id(key: ec.EllipticCurvePrivateKey) -> bytes:
    return x509.SubjectKeyIdentifier.from_public_key(key.public_key()).digest


_FOREIGN_KEY = ec.generate_private_key(ec.SECP256R1())
_OTHER = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "other CA")])
# Тот же текст, что у имени CA «123», записью NumericString.
_NUMERIC_SAME = x509.Name(
    [x509.NameAttribute(NameOID.COMMON_NAME, "123", _type=_ASN1Type.NumericString)]
)


def _authority(
    case: str,
) -> Callable[[ec.EllipticCurvePrivateKey, x509.Name, int], x509.AuthorityKeyIdentifier]:
    def build(
        key: ec.EllipticCurvePrivateKey, issuer: x509.Name, serial: int
    ) -> x509.AuthorityKeyIdentifier:
        if case == "foreign-key":
            return x509.AuthorityKeyIdentifier.from_issuer_public_key(_FOREIGN_KEY.public_key())
        if case == "foreign-serial":
            return x509.AuthorityKeyIdentifier(
                _own_key_id(key), [x509.DirectoryName(issuer)], serial ^ 1
            )
        names = {"foreign-issuer": _OTHER, "issuer-by-value-only": _NUMERIC_SAME}
        name = names.get(case, issuer)
        return x509.AuthorityKeyIdentifier(_own_key_id(key), [x509.DirectoryName(name)], serial)

    return build


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("foreign-key", "чужой ключ"),
        ("foreign-serial", "чужой серийный номер"),
        ("foreign-issuer", "чужого издателя"),
        ("issuer-by-value-only", "чужого издателя"),
    ],
)
def test_the_ca_names_itself_in_its_authority_key_identifier(case: str, reason: str) -> None:
    """Самоподписанность для OpenSSL — ещё и AKI (``X509_check_akid``): если он есть, ключ,
    серийный номер и издатель в нём — самого CA. CA, подписанный своим ключом, но с AKI чужого
    ключа, OpenSSL якорем не признаёт — при глубине 0 лист ноды отвергнут («certificate chain too
    long», OpenSSL 3.0.13 стенда), и здоровый старт ``api`` дал бы 400 всему парку (роаст 001.25,
    раунд 6). Издатель в AKI сверяется по DER: тот же текст записью NumericString для
    ``cryptography`` равен издателю, для OpenSSL — нет (здесь сверка по DER и решает исход —
    роаст 001.25, раунд 8)."""
    name = "123" if case == "issuer-by-value-only" else "control-plane test CA"
    with pytest.raises(ValueError, match=reason):
        ca.InternalCA.from_pem(*make_ca(name, authority=_authority(case)))


def test_an_authority_key_identifier_naming_the_ca_itself_is_accepted() -> None:
    """AKI о себе самом и CA без AKI — загружаются."""
    ca.InternalCA.from_pem(*make_ca(authority=_authority("itself")))
    ca.InternalCA.from_pem(*make_ca())


def test_a_ca_whose_extended_key_usage_excludes_node_purposes_is_refused() -> None:
    """``extendedKeyUsage`` у CA необязателен, но если он есть, OpenSSL требует его и от якоря:
    без ``clientAuth`` nginx отверг бы лист каждой ноды — 400 всему парку на прокси вместо отказа
    старта, без ``serverAuth`` агент не примет серверный сертификат агентских портов, выпущенный
    этим же CA; ``anyExtendedKeyUsage`` проверку OpenSSL не проходит (роаст 001.25, раунд 4)."""
    client, server = ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH
    for purposes in ([server], [client], [ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE]):
        with pytest.raises(ValueError, match="clientAuth и serverAuth"):
            ca.InternalCA.from_pem(*make_ca(extended_key_usage=purposes))
    ca.InternalCA.from_pem(*make_ca(extended_key_usage=[client, server]))
    ca.InternalCA.from_pem(*make_ca())  # без extendedKeyUsage — как dev CA стенда
