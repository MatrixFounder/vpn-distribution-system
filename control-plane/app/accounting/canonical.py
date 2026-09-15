"""Канонические формы значений Node API (README каталога ``contracts/agent_v1/``): UUID — 36
символов с дефисами, метка времени — текст RFC 3339 с разделителем ``T`` и смещением, адрес —
текст без zone id. Другие формы модели не принимают — так у «самого большого допустимого
тела» есть верхняя граница по ширине (страж предела прокси), а вторая сторона обмена не
полагается на то, что сервер примет больше, чем записано в контракте (pydantic принял бы
``urn:uuid:…``, hex без дефисов, секунды эпохи числом и строкой, ``fe80::1%eth0``).

Модуль общий для схем отчёта (``service.py``) и запроса гранта (``quota.py``): одно правило —
одно место, и ``quota`` не импортирует ``service`` (та импортирует ``QuotaService``).
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from typing import Annotated

from pydantic import AwareDatetime, BeforeValidator

# Самая длинная строка грамматики ``TIMESTAMP``: «2026-09-10T12:00:57.123456+00:00» — 19 знаков
# даты и времени, точка и шесть знаков дробной части, смещение из шести знаков. Константа —
# ширина поля для стража самого большого допустимого тела (предел прокси), а не отдельное
# правило: правило — сама грамматика, и согласованность константы с ней закреплена тестом
# (строка на знак длиннее грамматику не проходит).
TIMESTAMP_MAX_CHARS = 19 + 7 + 6
TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)
UUID_CHARS = 36


def _canonical_uuid(value: object) -> object:
    """Только каноническая запись — 36 символов с дефисами. ``urn:uuid:…`` и hex без дефисов
    pydantic принял бы, но ширина тела перестала бы быть ограниченной сверху; дефисы не на
    месте при 36 символах отвергает сам разбор UUID."""
    if not isinstance(value, str) or len(value) != UUID_CHARS:
        raise ValueError("UUID ожидается в канонической записи: 36 символов с дефисами")
    return value


def _timestamp_text(value: object) -> object:
    """Метка времени — текст RFC 3339 с разделителем ``T``, смещением и не более чем шестью
    знаками дробной части (тем самым не длиннее ``TIMESTAMP_MAX_CHARS``). Секунды эпохи — ни
    числом, ни строкой (pydantic принял бы обе формы как UTC)."""
    if not isinstance(value, str) or TIMESTAMP.match(value) is None:
        raise ValueError(
            "метка времени — текст RFC 3339 со смещением, не более шести знаков дробной части"
        )
    return value


def _plain_address(value: object) -> object:
    """Адрес — текст без zone id: ``fe80::1%eth0`` библиотека ``ipaddress`` принимает, а
    ``inet`` PostgreSQL — нет; целые (3232235777) в контракт не входят."""
    if not isinstance(value, str):
        raise ValueError("адрес — текст IPv4 или IPv6, не число")
    if "%" in value:
        raise ValueError("адрес без zone id")
    return value


CanonicalUuid = Annotated[uuid.UUID, BeforeValidator(_canonical_uuid)]
Timestamp = Annotated[AwareDatetime, BeforeValidator(_timestamp_text)]
Address = Annotated[ipaddress.IPv4Address | ipaddress.IPv6Address, BeforeValidator(_plain_address)]
