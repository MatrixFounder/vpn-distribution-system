"""Раздел ``/agent/v1`` на заглушках чужих служб (состав, команды, учёт): заголовки ноды и подмена
поиска ноды по identity (задача 001.25).

С 001.25 ``current_node`` ищет ноду в базе, а тесты заглушек 001.28 и 001.33 базу не трогают
(часовой ``NoDatabase``). ``node_as`` подменяет ровно поиск: версия агента (426) и форма признаков
(401) по-прежнему проверяются настоящими ``supported_agent_version`` и ``presented_identity``, так
что тесты формы заголовков и воспроизведение записанных фикстур идут через боевые правила.
Настоящий поиск по отпечатку, отзыв и сверка токена — ``tests/e2e/test_nodes.py`` на живой базе.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from app.agent_api import deps
from app.agent_api.deps import CurrentNode
from app.domain.nodes import STUB_NODE_ID, NodeStatus, stub_node
from fastapi import Depends

from tests._pki import STUB_CLIENT_CERT, STUB_CLIENT_FINGERPRINT

IDENTITY = "stub-identity-token-000000000000000000000000000"
FINGERPRINT = STUB_CLIENT_FINGERPRINT
HEADERS = {
    "X-Agent-Version": "0.1.0",
    "X-Client-Cert": STUB_CLIENT_CERT,
    "X-Node-Identity": IDENTITY,
}


def node_as(
    status: NodeStatus = "active", node_id: uuid.UUID = STUB_NODE_ID
) -> Callable[..., Awaitable[CurrentNode]]:
    """Подмена ``current_node``: признаки разбирает настоящий код, нода — фиксированная карточка
    ``stub_node`` в статусе ``status``."""

    async def override(
        agent_version: Annotated[str, Depends(deps.supported_agent_version)],
        presented: Annotated[deps.Presented, Depends(deps.presented_identity)],
    ) -> CurrentNode:
        return CurrentNode(
            node=stub_node(status=status, node_id=node_id),
            fingerprint=presented.fingerprint,
            identity_token=presented.identity_token,
            agent_version=agent_version,
        )

    return override
