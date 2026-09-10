"""``POST /agent/v1/commands/{command_id}/result`` — отчёт ноды об исполнении команды
(interfaces.md §5.2; data-model.md §4.2.3 ``commands``; UC-11, UC-12).

Задача 001.28: маршрут и схема на заглушке ``CommandService``. Сверка принадлежности команды
ноде, запись результата, реакция на ``failed`` и аудит — 001.76.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.agent_api.deps import Commands, Served
from app.agent_api.state import AGENT_ERRORS
from app.domain.commands import CommandResultIn

router = APIRouter()


@router.post(
    "/commands/{command_id}/result",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Результат исполнения команды: applied | failed | expired (§5.2)",
    responses=AGENT_ERRORS,
)
async def command_result(
    command_id: uuid.UUID, body: CommandResultIn, agent: Served, commands: Commands
) -> None:
    """Служба получает и ноду (отклонение от сигнатуры задачи): команда принадлежит ноде
    (``commands.node_id`` §4.2.3), и без этого любая нода закрывала бы чужую команду по
    угаданному идентификатору. Сверка принадлежности и запись результата — 001.76."""
    await commands.result(agent.node, command_id, body.status, body.error)
