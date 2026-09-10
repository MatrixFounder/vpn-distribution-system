"""Канал команд ноды (interfaces.md §5.2 «Результат команды»; data-model.md §4.2.3 ``commands``;
постановка §4.6, столбец «Канал команд»; UC-11, UC-12).

Задача 001.28: схемы команды и её результата, ``CommandService`` с фиксированными значениями.
Очередь команд в базе, выдача в ответе состояния, идемпотентность, истечение по ``expires_at`` и
статусы — 001.76; ``update_agent`` с закреплёнными версиями — 001.31.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from app.domain.nodes import Node

# Перечисление ``command_type`` data-model.md §4.2.3.
CommandType = Literal["restart_xray", "rotate_credentials", "collect_diagnostics", "update_agent"]
# Перечисление ``command_status`` data-model.md §4.2.3 целиком.
CommandStatus = Literal["issued", "delivered", "applied", "failed", "expired"]
# Что о команде вправе сообщить сама нода: применена, отказала или получена уже просроченной
# (агент сверяет ``expires_at`` перед исполнением). ``issued`` и ``delivered`` ставит Control
# Plane — выдав команду и отдав её в ответе состояния, поэтому от ноды они не принимаются.
AgentCommandStatus = Literal["applied", "failed", "expired"]

ERROR_MAX_CHARS = 2000  # текст ошибки исполнения в ``commands.result``; больше — не диагностика


class Command(BaseModel):
    """Команда в ответе состояния: что исполнить и до какого момента это имеет смысл. Статус в
    канал не передаётся — для ноды он всегда ``delivered``, а свой ответ она шлёт отдельно."""

    id: uuid.UUID
    type: CommandType
    payload: dict[str, Any] = Field(default_factory=dict)
    issued_at: AwareDatetime
    expires_at: AwareDatetime


class CommandResultIn(BaseModel):
    """Тело ``POST /agent/v1/commands/{id}/result``: чем кончилось исполнение."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: AgentCommandStatus
    error: Annotated[str, StringConstraints(max_length=ERROR_MAX_CHARS)] | None = None


STUB_COMMAND_ID = uuid.UUID("00000000-0000-7000-8000-0000000000c1")
STUB_COMMAND_ISSUED_AT = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
# Срок команды заглушки заведомо дальний, и это не оценка настоящего TTL (его выбирает
# вызывающий, аргумент ``ttl`` у ``issue``). Причина: фикстура контракта неподвижна, а агент
# обязан сверять ``expires_at`` перед исполнением — команда, протухшая от одного лишь течения
# времени, дала бы красный контрактный тест на верной реализации агента (001.53).
STUB_COMMAND_EXPIRES_AT = dt.datetime(2036, 9, 1, tzinfo=dt.UTC)


def stub_command() -> Command:
    """Одна команда в канале: перезапуск Xray без параметров."""
    return Command(
        id=STUB_COMMAND_ID,
        type="restart_xray",
        payload={},
        issued_at=STUB_COMMAND_ISSUED_AT,
        expires_at=STUB_COMMAND_EXPIRES_AT,
    )


class CommandService:
    """Команды поверх пула asyncpg. Заглушка 001.28: база не читается и не пишется."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def issue(
        self,
        node_id: uuid.UUID,
        type: CommandType,  # noqa: A002 — имя колонки ``commands.type`` §4.2.3
        payload: dict[str, Any],
        ttl: dt.timedelta,
        now: dt.datetime | None = None,
    ) -> Command:
        """Поставить команду ноде (UC-11, UC-12). Заглушка считает срок от момента выдачи, но не
        сохраняет команду; запись в ``commands`` и пробуждение канала ``node:{id}`` — 001.76."""
        issued = now or dt.datetime.now(dt.UTC)
        return Command(
            id=STUB_COMMAND_ID,
            type=type,
            payload=payload,
            issued_at=issued,
            expires_at=issued + ttl,
        )

    async def pending(self, node: Node) -> list[Command]:
        """Команды, ожидающие доставки ноде. Заглушка отдаёт одну фиксированную; отбор по
        ``status in (issued, delivered)`` и непросроченным ``expires_at`` — 001.76.

        Заглушка не помнит доставку, поэтому её канал никогда не пуст. Следствие записано в
        ``CompositionService.state_for``: команда едет вместе с ответом, который и так
        отправляется, и не является поводом ответить. В 001.76 обязана: ожидающая команда
        проверяется до решения «изменений нет»."""
        return [stub_command()]

    async def result(
        self,
        node: Node,
        command_id: uuid.UUID,
        status: AgentCommandStatus,
        error: str | None = None,
    ) -> None:
        """Принять отчёт ноды об исполнении. Принимает ноду целиком, а не идентификатор: канал
        команд вентилирует §4.6, и правило обязано быть доступно самой службе, а не только
        маршруту. Сверка ``commands.node_id`` с отчитавшейся нодой (чужую команду закрыть
        нельзя), запись ``commands.status``/``result``, реакция на ``failed`` и аудит — 001.76."""
