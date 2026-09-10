"""``POST /agent/v1/heartbeat`` и ``POST /agent/v1/metrics`` — признаки жизни и телеметрия ноды
(interfaces.md §5.2; постановка §4.6 «Матрица переходов», §4.7 пороги; UC-07, UC-11).

Задача 001.28: маршруты и схемы на заглушке ``StatusService``. Счётчики Н-15 и переходы
``active`` ↔ ``degraded`` ↔ ``offline``, запись ``nodes.last_heartbeat_at`` и строк
``node_metrics`` — 001.30; подтверждение курсоров, которое heartbeat несёт вместе с ними, —
001.75.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, status

from app.agent_api.deps import Served, Statuses, Streams
from app.agent_api.state import AGENT_ERRORS
from app.domain.statuses import HeartbeatIn, NodeMetrics

router = APIRouter()


@router.post(
    "/heartbeat",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Heartbeat ноды: версии, её время, применённые курсоры (§5.2)",
    responses=AGENT_ERRORS,
)
async def heartbeat(body: HeartbeatIn, agent: Served, statuses: Statuses, streams: Streams) -> None:
    """Heartbeat несёт и применённые курсоры (§5.2), поэтому подтверждает их наравне с
    ``POST /ack``: ноде, которой нечего подтверждать отдельно, второй запрос не нужен.

    Отметка — время Control Plane (``dt.datetime.now``), а не то, что прислала нода: пороги
    Н-15 меряет сервер. Присланное время идёт в домен рядом как данные для сверки перекоса
    (§5.9). Два вызова домена по одной строке ``nodes`` — форма заглушки; 001.30 обязана
    свести их в одну транзакцию, иначе крах между ними оставит отметку без курсоров."""
    await statuses.on_heartbeat(agent.node.id, dt.datetime.now(dt.UTC), body)
    await streams.ack(agent.node, body.applied_config_version, body.applied_users_seq)


@router.post(
    "/metrics",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Телеметрия ноды: ресурсы, сеть, адреса, соединения (§4.7)",
    responses=AGENT_ERRORS,
)
async def metrics(body: NodeMetrics, agent: Served, statuses: Statuses) -> None:
    await statuses.on_metrics(agent.node.id, body)
