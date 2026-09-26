"""Внутренний CA (security.md §7.1): выпуск клиентских сертификатов нод при enrollment на 90
дней и отпечаток сертификата для ``node_identities.cert_fingerprint``.

Ключ и сертификат CA — файлы секретов роли ``api``: ``CA_KEY_FILE`` (Docker secret ``ca_key``,
PEM, prime256v1) и ``CA_CERT_FILE`` (``ca_cert`` — тот же ``tls/ca.crt``, которым nginx проверяет
клиентов агентского server). Ключ — файлом, а не записью C-01 под ``FieldCipher``, как предлагала
§7.1 (задача 001.25): nginx нужен сертификат CA файлом раньше первого запроса, ключ шифрования
полей — такой же файл секрета на том же хосте, и зашифрованная копия в базе защиты не прибавила бы,
а копию ключа CA в каждой резервной копии базы — прибавила бы. Пара сверяется при загрузке: ключ,
не подходящий к сертификату, давал бы листы, которые nginx отвергнет у всего парка.

Лист ноды решает CA, а не нода: из CSR берётся один открытый ключ P-256 после проверки подписи;
субъект — идентификатор ноды, серийный номер случайный (159 бит), срок — ``CERT_DAYS``, профиль —
``basicConstraints CA:FALSE`` и ``keyUsage digitalSignature`` (оба critical),
``extendedKeyUsage clientAuth``. Субъект и расширения CSR предлагает проверяемая сторона: копируя
их, CA дал бы ноде назваться другой нодой или выпустить себе лист с ``CA:TRUE``, который множил бы
серийные номера — ключи всех пределов прокси (001.33; nginx к тому же держит ``ssl_verify_depth
0`` — только лист, подписанный самим CA: в OpenSSL глубина считает промежуточных CA).

``fingerprint`` — SHA-256 от DER в hex нижнего регистра, как хранит модель данных §4.2.3;
``serial_hex`` — серийный номер в той форме, в какой его показывает nginx (``$ssl_client_serial``):
по нему ключуются пределы прокси и будет ключоваться карта отказа 001.66.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import functools
import hashlib
import logging
import re
import uuid
from typing import NamedTuple

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID, NameOID

from app.config import SecretError, Settings

log = logging.getLogger(__name__)

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

# Ключ ноды — только P-256: тот же размер, что у ключа CA, и то, что генерирует агент (001.53).
# Кривая закрыта, а не «любая EC»: nginx и OpenSSL приняли бы и слабые, и экзотические кривые.
NODE_KEY_CURVE = ec.SECP256R1


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


class CsrRejectedError(ValueError):
    """CSR ноды отвергнут: не PEM, не разбирается, подпись не сходится, ключ не P-256. Отдельный
    класс, а не любая ``ValueError``: маршрут enrollment отвечает на него 422 ``invalid_csr``, а
    прочая ``ValueError`` из обмена (ошибка программы) должна остаться 500, а не стать виной
    клиента."""


class IssuedCert(NamedTuple):
    """Выпущенный лист: PEM для ноды, отпечаток и серийный номер для ``node_identities`` и срок
    действия — те же значения, что записаны в сертификат, чтобы строка identity не считала их
    заново."""

    pem: str
    fingerprint: str
    serial: str
    not_before: dt.datetime
    not_after: dt.datetime


def serial_hex(serial: int) -> str:
    """Серийный номер как его печатают nginx (``$ssl_client_serial``) и ``openssl x509
    -serial`` — ``i2a_ASN1_INTEGER``: байты величины в hex верхнего регистра, по два знака на байт,
    ведущий ноль байта сохраняется (``0A1B2C``). Сверено с OpenSSL 3.0.13 стенда и LibreSSL."""
    if serial <= 0:
        raise ValueError("серийный номер сертификата — положительное число")
    return serial.to_bytes((serial.bit_length() + 7) // 8, "big").hex().upper()


# Расширения CA — список разрешённых (роаст 001.25, раунды 7–8): у каждого известно, что с ним
# делает проверка якоря в OpenSSL, и оно сверено ниже. basicConstraints, keyUsage и
# extendedKeyUsage могут быть критическими (OpenSSL их поддерживает), SKI и AKI — только
# некритическими (критический SKI или AKI — «unhandled critical extension» на каждой цепочке).
# Любое другое — отказ, а не догадка о том, как OpenSSL его применит: ограничения имён — к
# листам нод, Netscape Cert Type без sslCA делает якорь негодным для проверки клиента, а
# незнакомое расширение не проверить вовсе. Здоровый старт api с таким CA дал бы 400 всему
# парку на прокси. Значение — может ли расширение быть критическим.
_CA_EXTENSIONS = {
    ExtensionOID.BASIC_CONSTRAINTS: True,
    ExtensionOID.KEY_USAGE: True,
    ExtensionOID.EXTENDED_KEY_USAGE: True,
    ExtensionOID.SUBJECT_KEY_IDENTIFIER: False,
    ExtensionOID.AUTHORITY_KEY_IDENTIFIER: False,
}
# Исключения cryptography при разборе и проверке сертификата CA, которые не ``ValueError``: имя
# неподдержанного вида (x400Address, ediPartyName) в AKI или SAN, версия не v1/v3, неизвестный
# алгоритм ключа или подписи; повторённое расширение (``DuplicateExtension``) — своим
# сообщением там, где читаются расширения. Все сводятся к ``ValueError`` — иначе ``from_files``
# пропустил бы их мимо ``SecretError`` сырой трассировкой старта (роаст 001.25, раунды 7–8).
_CRYPTOGRAPHY_ERRORS = (
    x509.UnsupportedGeneralNameType,
    x509.InvalidVersion,
    UnsupportedAlgorithm,
)


def _same_name(first: x509.Name, second: x509.Name) -> bool:
    """Имена равны по DER. Равенство ``cryptography`` сравнивает значения без типа строки, а
    OpenSSL — каноническую форму, где, например, NumericString и UTF8String с тем же текстом
    различны: CA, которого ``cryptography`` счёл бы самовыпущенным, OpenSSL якорем не признал бы.
    DER строже обоих — стандартный корень (``openssl req -x509``) копирует имя байт в байт.
    Исход решает сверка DirectoryName из AKI с издателем (``X509_check_akid``); сверку издателя с
    субъектом повторяет и ``verify_directly_issued_by`` (структурно, с типом строки) — здесь она
    даёт отказу точное сообщение (роаст 001.25, раунд 8)."""
    return first.public_bytes() == second.public_bytes()


class InternalCA:
    """CA нод: ключ P-256 и самоподписанный сертификат, которому доверяет nginx агентского
    server. ``ValueError`` при создании — ключ не от этого сертификата или не P-256, у
    сертификата нет ``basicConstraints CA:TRUE`` (лист в роли CA OpenSSL не примет у клиента), он
    не самоподписанный (издатель, подпись, AKI), его ``keyUsage`` не разрешает ``keyCertSign`` или
    ``extendedKeyUsage`` не разрешает ``clientAuth`` и ``serverAuth``, у него расширение вне
    списка разрешённых (или SKI и AKI критическими), повторённое расширение, имя неподдержанного
    вида, неизвестный алгоритм ключа или подписи."""

    def __init__(self, key: ec.EllipticCurvePrivateKey, cert: x509.Certificate) -> None:
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, NODE_KEY_CURVE
        ):
            raise ValueError("ключ CA — EC P-256 (prime256v1)")
        try:
            extensions = cert.extensions
        except x509.DuplicateExtension as exc:
            raise ValueError(
                f"в сертификате CA повторено расширение {exc.oid.dotted_string}"
            ) from exc
        except _CRYPTOGRAPHY_ERRORS as exc:
            raise ValueError(
                f"расширения сертификата CA не разбираются: {type(exc).__name__}: {exc}"
            ) from exc
        for extension in extensions:
            if extension.oid not in _CA_EXTENSIONS:
                raise ValueError(
                    f"расширение CA {extension.oid.dotted_string} вне списка разрешённых "
                    "(basicConstraints, keyUsage, extendedKeyUsage, SKI, AKI)"
                )
            if extension.critical and not _CA_EXTENSIONS[extension.oid]:
                raise ValueError(
                    f"расширение CA {extension.oid.dotted_string} критическое: OpenSSL его не "
                    "поддерживает и отвергнет каждую цепочку"
                )
        try:
            is_ca = extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        except x509.ExtensionNotFound:
            is_ca = False
        if not is_ca:
            raise ValueError("сертификат CA без basicConstraints CA:TRUE")
        # Якорь — самоподписанный: nginx проверяет лист ноды с глубиной 0 до сертификата ca.crt и
        # без частичных цепочек, и промежуточный CA (выпущенный чужим корнем) дал бы здоровый
        # старт api и отказ всему парку на прокси (роаст 001.25, раунд 5).
        if not _same_name(cert.issuer, cert.subject):
            raise ValueError("сертификат CA не самоподписанный: издатель — не он сам")
        try:
            cert.verify_directly_issued_by(cert)
        except (ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm) as exc:
            raise ValueError("сертификат CA не подписан собственным ключом") from exc
        # Самоподписанность для OpenSSL — ещё и AKI: если он есть, ключ, серийный номер и издатель
        # в нём — самого CA (X509_check_akid). CA, подписанный своим ключом, но с AKI чужого
        # ключа, OpenSSL якорем не признаёт: при глубине 0 лист ноды — «certificate chain too
        # long», 400 всему парку при здоровом старте api (стенд, роаст 001.25, раунд 6).
        try:
            authority = extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
        except x509.ExtensionNotFound:
            authority = None
        if authority is not None:
            try:
                own_key: bytes | None = extensions.get_extension_for_class(
                    x509.SubjectKeyIdentifier
                ).value.digest
            except x509.ExtensionNotFound:
                own_key = None
            named_key = authority.key_identifier
            if named_key is not None and own_key is not None and named_key != own_key:
                raise ValueError("AKI сертификата CA называет чужой ключ")
            serial = authority.authority_cert_serial_number
            if serial is not None and serial != cert.serial_number:
                raise ValueError("AKI сертификата CA называет чужой серийный номер")
            names = [
                entry.value
                for entry in authority.authority_cert_issuer or ()
                if isinstance(entry, x509.DirectoryName)
            ]
            if names and not _same_name(names[0], cert.issuer):
                raise ValueError("AKI сертификата CA называет чужого издателя")
        # keyUsage необязателен (dev CA из ``openssl req -x509`` без него, RFC 5280 тогда не
        # ограничивает назначение), но если он есть, без keyCertSign листы CA не проверит никто.
        try:
            usage = extensions.get_extension_for_class(x509.KeyUsage).value
        except x509.ExtensionNotFound:
            usage = None
        if usage is not None and not usage.key_cert_sign:
            raise ValueError("keyUsage сертификата CA без keyCertSign")
        # extendedKeyUsage у CA тоже необязателен, но если он есть, OpenSSL при проверке цепочки
        # требует его и от якоря: без clientAuth nginx отверг бы лист каждой ноды (400 всему парку
        # вместо отказа старта), без serverAuth агент не примет серверный сертификат агентских
        # портов, выпущенный этим же CA (001.66). anyExtendedKeyUsage проверку OpenSSL не проходит
        # (роаст 001.25, раунд 4).
        try:
            purposes = set(extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)
        except x509.ExtensionNotFound:
            purposes = None
        if (
            purposes is not None
            and not {
                ExtendedKeyUsageOID.CLIENT_AUTH,
                ExtendedKeyUsageOID.SERVER_AUTH,
            }
            <= purposes
        ):
            raise ValueError("extendedKeyUsage сертификата CA без clientAuth и serverAuth")
        public = serialization.PublicFormat.SubjectPublicKeyInfo
        der = serialization.Encoding.DER
        try:
            cert_key = cert.public_key().public_bytes(der, public)
        except _CRYPTOGRAPHY_ERRORS as exc:
            raise ValueError(
                f"ключ сертификата CA не читается: {type(exc).__name__}: {exc}"
            ) from exc
        if key.public_key().public_bytes(der, public) != cert_key:
            raise ValueError("ключ CA не подходит к сертификату CA")
        self._key = key
        self._cert = cert
        self.ca_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
        # Идентификатор ключа издателя: из расширения сертификата CA, если оно есть (openssl
        # req -x509 его ставит), иначе — от открытого ключа тем же способом, что и openssl.
        try:
            ski = extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
            self._aki = x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski)
        except x509.ExtensionNotFound:
            self._aki = x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key())

    @property
    def not_valid_before(self) -> dt.datetime:
        """Начало срока сертификата CA (UTC)."""
        return self._cert.not_valid_before_utc

    @property
    def not_valid_after(self) -> dt.datetime:
        """Конец срока сертификата CA (UTC): дальше него лист не выпускается."""
        return self._cert.not_valid_after_utc

    @classmethod
    def from_pem(cls, key_pem: bytes, cert_pem: bytes) -> InternalCA:
        """CA из PEM ключа (без пароля) и PEM сертификата. Ключ под паролем ``cryptography``
        отвергает ``TypeError``, неизвестный алгоритм — ``UnsupportedAlgorithm``, версию
        сертификата не v1/v3 — ``InvalidVersion``: все сводятся к ``ValueError``, как и прочая
        негодность пары, — иначе ``from_files`` пропустил бы их мимо ``SecretError`` сырой
        ошибкой. В файле сертификата — ровно один блок PEM, и это
        ``CERTIFICATE``: nginx доверяет каждому сертификату ``ca.crt`` — OpenSSL читает и блоки
        ``TRUSTED CERTIFICATE`` и ``X509 CERTIFICATE``, — а CA подписывает первым, и лишний
        сертификат в пачке молча разделил бы «одну пару» прокси и CA (роаст 001.25, раунды 3–4)."""
        blocks = [label.decode() for label in re.findall(rb"-----BEGIN ([^-\r\n]*)-----", cert_pem)]
        if blocks != ["CERTIFICATE"]:
            raise ValueError(
                "в файле сертификата CA ожидается ровно один блок PEM CERTIFICATE, найдено: "
                + (", ".join(blocks) or "ни одного")
            )
        try:
            key = serialization.load_pem_private_key(key_pem, password=None)
            cert = x509.load_pem_x509_certificate(cert_pem)
        except (TypeError, *_CRYPTOGRAPHY_ERRORS) as exc:
            raise ValueError(f"ключ или сертификат CA не читается: {type(exc).__name__}") from exc
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("ключ CA — EC P-256 (prime256v1)")
        return cls(key, cert)

    @classmethod
    def from_files(cls, key_path: str, cert_path: str) -> InternalCA:
        """CA из файлов секретов; нечитаемый файл или негодная пара — ``SecretError`` с путями
        (содержимое ключа в сообщение не попадает)."""
        try:
            with open(key_path, "rb") as key_file, open(cert_path, "rb") as cert_file:
                return cls.from_pem(key_file.read(), cert_file.read())
        except OSError as exc:
            raise SecretError(f"CA недоступен: {exc.filename} ({exc.strerror})") from exc
        except ValueError as exc:
            raise SecretError(f"CA негоден: {key_path}, {cert_path}: {exc}") from exc

    def sign_csr(
        self,
        csr_pem: str,
        *,
        node_id: uuid.UUID,
        now: dt.datetime,
        days: int = CERT_DAYS,
    ) -> IssuedCert:
        """Лист ноды ``node_id`` по CSR на ``days`` дней от ``now`` (момент операции обмена —
        ``statement_timestamp()`` после блокировки ноды: один источник времени у сертификата и
        строки identity), но не дольше срока самого CA:
        лист, переживший CA, nginx отвергнет, а identity в базе считалась бы действующей.
        ``CsrRejectedError`` — CSR не PEM, не разбирается, ключ не P-256, подпись CSR не
        сходится; ``ValueError`` — ошибка вызова или конфигурации: срок не положительный, ``now``
        без часового пояса, CA вне своего срока."""
        if days <= 0:
            raise ValueError("срок сертификата — положительное число дней")
        if now.tzinfo is None:
            raise ValueError("момент выпуска — с часовым поясом")
        not_before = now.astimezone(dt.UTC).replace(microsecond=0)
        ca_not_after = self._cert.not_valid_after_utc
        if not self._cert.not_valid_before_utc <= not_before < ca_not_after:
            raise ValueError("CA вне своего срока — выпуск листов невозможен до замены CA")
        # Порядок проверок — от дешёвой к дорогой: тип ключа раньше подписи. Иначе держатель
        # годного токена платил бы циклу событий проверкой подписи RSA-16384 на каждом повторе,
        # а отказ CA токен не гасит. Неизвестный алгоритм ключа или подписи ``cryptography``
        # сообщает ``UnsupportedAlgorithm``, а не ``ValueError``, — без перехвата это был бы 500.
        try:
            csr = x509.load_der_x509_csr(pem_der(csr_pem, "CERTIFICATE REQUEST"))
            node_key = csr.public_key()
            if not isinstance(node_key, ec.EllipticCurvePublicKey) or not isinstance(
                node_key.curve, NODE_KEY_CURVE
            ):
                raise CsrRejectedError("ключ ноды — EC P-256 (prime256v1)")
            signed = csr.is_signature_valid
        except (ValueError, UnsupportedAlgorithm) as exc:
            if isinstance(exc, CsrRejectedError):
                raise
            raise CsrRejectedError(f"CSR не разобран: {exc}") from exc
        if not signed:
            raise CsrRejectedError("подпись CSR не сходится с его ключом")
        not_after = min(not_before + dt.timedelta(days=days), ca_not_after)
        serial = x509.random_serial_number()
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(node_id))]))
            .issuer_name(self._cert.subject)
            .public_key(node_key)
            .serial_number(serial)
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(node_key), critical=False)
            .add_extension(self._aki, critical=False)
            .sign(self._key, hashes.SHA256())
        )
        pem = cert.public_bytes(serialization.Encoding.PEM).decode()
        return IssuedCert(pem, self.fingerprint(pem), serial_hex(serial), not_before, not_after)

    @staticmethod
    def fingerprint(cert_pem: str) -> str:
        """SHA-256 от DER сертификата, hex в нижнем регистре (64 символа)."""
        return fingerprint_der(pem_der(cert_pem, "CERTIFICATE"))


def fingerprint_der(der: bytes) -> str:
    """SHA-256 от DER, hex в нижнем регистре — форма ``node_identities.cert_fingerprint``."""
    return hashlib.sha256(der).hexdigest()


@functools.cache
def _load(key_path: str, cert_path: str) -> InternalCA:
    """Пара из файлов и её пригодность на момент загрузки (старт роли ``api``): CA вне своего
    срока — ``SecretError``, и процесс не стартует (роаст 001.25, раунд 2). CA, истекающий раньше
    ``CERT_DAYS``, пригоден, но укорачивает каждый новый лист до своего конца — об этом
    предупреждение в журнале. Если CA истечёт, пока процесс работает, отказ даст выпуск листа
    (``sign_csr``: ошибка конфигурации, 500 на обмене), а не старт; метрика оставшегося срока CA
    и тревога по ней — 001.68."""
    ca = InternalCA.from_files(key_path, cert_path)
    now = dt.datetime.now(dt.UTC)
    if not ca.not_valid_before <= now < ca.not_valid_after:
        raise SecretError(
            f"CA вне своего срока ({ca.not_valid_before:%Y-%m-%d} — "
            f"{ca.not_valid_after:%Y-%m-%d}): {cert_path}"
        )
    if ca.not_valid_after - now < dt.timedelta(days=CERT_DAYS):
        log.warning(
            "CA истекает %s — раньше %s дней: новые листы укорачиваются до срока CA; "
            "замените CA (перезапуск api и перечитывание nginx вместе)",
            f"{ca.not_valid_after:%Y-%m-%d}",
            CERT_DAYS,
        )
    return ca


def get_ca() -> InternalCA:
    """CA процесса из ``CA_KEY_FILE``/``CA_CERT_FILE``: загружается на старте роли ``api``
    (lifespan, ``app.main``) и запоминается — смена ключа CA требует перезапуска ``api`` (и
    перечитывания nginx). Нет переменной, файла или пары — ``SecretError``: при старте это
    отказ процесса, а не 500 у каждой ноды."""
    settings = Settings.read()
    if not settings.ca_key_path or not settings.ca_cert_path:
        raise SecretError("CA_KEY_FILE и CA_CERT_FILE обязательны для enrollment")
    return _load(settings.ca_key_path, settings.ca_cert_path)


def get_ca_pem() -> str:
    """Только сертификат CA процесса (PEM) — якорь bootstrap-команды для ответа выдачи токена:
    панели ключ CA не нужен, и её служба нод получает строку, а не объект, умеющий подписывать."""
    return get_ca().ca_pem
