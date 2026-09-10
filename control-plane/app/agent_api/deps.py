"""Зависимости раздела ``/agent/v1``: поддерживаемая версия агента и текущая нода (security.md
§7.1; interfaces.md §5.2).

Нода предъявляет два признака: клиентский сертификат внутреннего CA, чей отпечаток передаёт
прокси заголовком ``X-Client-Fingerprint`` (агентский ``server`` nginx переписывает его
безусловно, публичный и enrollment — очищают), и токен identity в ``X-Node-Identity``. Версия
агента — ``X-Agent-Version`` в каждом запросе; неподдерживаемая — 426.

Задача 001.28: обе зависимости объявлены, версия проверяется по-настоящему, нода — фиксированная
карточка заглушки. Поиск ноды по отпечатку в ``node_identities``, сверка токена, отказ отозванной
identity (401, Н-31) и ограничение частоты по identity (§5.12) — 001.25 и 001.72; закреплённые
версии агента и команда ``update_agent`` — 001.31.

Для 001.72: ключ лимита «identity ноды» требует поиска в базе, то есть ограничитель оплатил бы
то, что ограничивает. Дешёвый предфильтр — ``X-Client-Fingerprint``: его ставит прокси, подделать
его нельзя, и он доступен из заголовка за ноль обращений.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header

from app.db.pool import db_pool
from app.domain.commands import CommandService
from app.domain.composition import CompositionService, node_not_approved
from app.domain.nodes import Node, stub_node
from app.domain.statuses import STREAM_GATE, StatusService
from app.errors import ApiError

AGENT_VERSION_HEADER = "X-Agent-Version"
FINGERPRINT_HEADER = "X-Client-Fingerprint"
IDENTITY_HEADER = "X-Node-Identity"

# Отпечаток: hex без разделителей. SHA-1 (40) — то, что сегодня даёт ``$ssl_client_fingerprint``
# nginx, SHA-256 (64) — то, что хранит ``node_identities.cert_fingerprint``; выбор между ними
# делает 001.25 (записано в её примечаниях), а до тех пор принимаются обе длины и только они.
# Классы заданы явно (``[0-9a-f]``, ``[0-9]``), а не через ``\d``: последний в Python матчит
# любые юникодные цифры, и «١.١.٠» разобралось бы как версия 1.1.0. ``\Z``, а не ``$``:
# ``$`` совпадает и перед завершающим переводом строки.
FINGERPRINT_HEX = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
# Токен identity: длина как у токена подписки и bootstrap-токена — 256 бит в base64url и запас.
IDENTITY_MIN_CHARS = 32
IDENTITY_MAX_CHARS = 128
# Алфавит — base64url выданного токена (§7.2, 256 бит): сужаем вход до отказа, а не после
# него. Сравнение самого значения — только ``secrets.compare_digest`` в 001.25.
IDENTITY_TOKEN = re.compile(rf"^[A-Za-z0-9_-]{{{IDENTITY_MIN_CHARS},{IDENTITY_MAX_CHARS}}}\Z")

# Наименьшая версия агента, с которой Control Plane обменивается состоянием. Версии ниже
# получают 426 и обязаны обновиться (interfaces.md §5.2).
MIN_AGENT_VERSION = (0, 1, 0)
# Повтор после 426 бессмысленен до обновления агента администратором (UC-11): час, а не
# секунды fail-closed. Значение ``RETRY_AFTER_SECONDS`` — общий для API минимум (§9.1).
RETRY_AFTER_UPGRADE = 3600
# Длина заголовка версии в символах — как у ``domain.nodes.Version``; длиннее — не версия.
AGENT_VERSION_MAX_CHARS = 64
_VERSION = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)(?:[-+][0-9A-Za-z.-]+)?\Z")


def version_str(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def unsupported_agent() -> ApiError:
    """426: версия агента не поддерживается, отсутствует или не разобрана. Ответ один на все
    четыре случая намеренно — в каждом агент обязан остановиться и обновиться, а не повторять.
    ``Retry-After`` длинный: обновление парка — действие администратора (UC-11), и опрос до
    него бессмысленен."""
    return ApiError(
        "unsupported_agent_version",
        f"версия агента не поддерживается: требуется не ниже {version_str(MIN_AGENT_VERSION)}",
        status=426,
        details={"min_version": version_str(MIN_AGENT_VERSION), "header": AGENT_VERSION_HEADER},
        headers={"Retry-After": str(RETRY_AFTER_UPGRADE)},
    )


def unknown_identity() -> ApiError:
    """401: identity не предъявлена, неизвестна или отозвана (§5.2). Ответ один на все случаи —
    агент в любом из них проходит enrollment заново."""
    return ApiError("unauthenticated", "identity ноды не предъявлена или неизвестна", status=401)


async def supported_agent_version(
    x_agent_version: Annotated[str | None, Header()] = None,
) -> str:
    """Версия агента из ``X-Agent-Version``: три числа, необязательный суффикс предвыпуска
    (сравнение — по числам, порядок предвыпусков задаёт 001.31). Отсутствие, длина сверх
    ``AGENT_VERSION_MAX_CHARS``, неразобранное значение и версия ниже ``MIN_AGENT_VERSION`` —
    426."""
    if not x_agent_version or len(x_agent_version) > AGENT_VERSION_MAX_CHARS:
        raise unsupported_agent()
    match = _VERSION.match(x_agent_version)
    if match is None:
        raise unsupported_agent()
    if tuple(int(part) for part in match.groups()) < MIN_AGENT_VERSION:
        raise unsupported_agent()
    return x_agent_version


@dataclass(frozen=True, slots=True)
class CurrentNode:
    """Нода, от имени которой пришёл запрос: её карточка и признаки, которыми она представилась."""

    node: Node
    fingerprint: str
    identity_token: str
    agent_version: str


async def current_node(
    agent_version: Annotated[str, Depends(supported_agent_version)],
    x_client_fingerprint: Annotated[str | None, Header()] = None,
    x_node_identity: Annotated[str | None, Header()] = None,
) -> CurrentNode:
    """Нода по отпечатку клиентского сертификата и токену identity. Заглушка 001.28 проверяет
    форму обоих признаков и не ищет ноду: сверка отпечатка с ``node_identities``, сравнение
    токена и отказ отозванной identity (Н-31) — 001.25.

    Пустой ``X-Client-Fingerprint`` ставит сам прокси на публичном и enrollment ``server``
    (§7.1). Через нынешний nginx такой запрос до приложения и не доходит — раздел там отдаёт
    404; проверка здесь на случай, когда дойдёт: значению заголовка от клиента верить нельзя ни
    при какой конфигурации прокси."""
    if not x_client_fingerprint or not FINGERPRINT_HEX.match(x_client_fingerprint):
        raise unknown_identity()
    if not x_node_identity or not IDENTITY_TOKEN.match(x_node_identity):
        raise unknown_identity()
    return CurrentNode(
        node=stub_node(),
        fingerprint=x_client_fingerprint,
        identity_token=x_node_identity,
        agent_version=agent_version,
    )


Agent = Annotated[CurrentNode, Depends(current_node)]


# Службы раздела. Пул берётся зависимостью ``Depends(db_pool)``, а не вызовом внутри фабрики:
# так подмена пула в тесте видна всему графу зависимостей — контрактные тесты подставляют
# объект-часовой и любое обращение заглушек к базе провалило бы прогон.
async def get_command_service(pool: Annotated[object, Depends(db_pool)]) -> CommandService:
    return CommandService(pool)


async def get_composition_service(
    pool: Annotated[object, Depends(db_pool)],
    commands: Annotated[CommandService, Depends(get_command_service)],
) -> CompositionService:
    """Служба команд берётся из графа зависимостей, а не создаётся внутри: иначе её подмена в
    тесте до выдачи состояния не доходила бы, а в 001.76 канал команд разошёлся бы молча."""
    return CompositionService(pool, commands)


async def get_status_service(pool: Annotated[object, Depends(db_pool)]) -> StatusService:
    return StatusService(pool)


async def served_node(node: Agent) -> CurrentNode:
    """§4.6: неподтверждённой ноде (``pending``) раздел не выдаёт ничего — ни потоков, ни
    команд, ни приёма подтверждений. Один вентиль на все операции раздела: иначе нода, которой
    состояние отвечает 403, объявляла бы себя применившей конфигурацию через ``POST /ack``."""
    if STREAM_GATE[node.node.status].refused:
        raise node_not_approved()
    return node


Served = Annotated[CurrentNode, Depends(served_node)]
Streams = Annotated[CompositionService, Depends(get_composition_service)]
Commands = Annotated[CommandService, Depends(get_command_service)]
Statuses = Annotated[StatusService, Depends(get_status_service)]
