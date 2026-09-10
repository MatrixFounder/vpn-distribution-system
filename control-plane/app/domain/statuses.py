"""Статусы ноды: матрица переходов и вентиль потоков (постановка §4.6 — единственный источник
правил; interfaces.md §5.2 и постановка §4.11 на неё ссылаются и её не повторяют; UC-01,
UC-07, UC-11).

Задача 001.28 переносит в код две таблицы §4.6 как данные: матрицу переходов (``TRANSITIONS`` и
``StatusService.can_transition``) и матрицу влияния в части потоков и канала команд
(``STREAM_GATE``); столбец «поток конфигурации» берётся из таблицы вентиля interfaces.md §5.2,
которой в матрице влияния нет. Обе — чистые правила без базы, поэтому реализованы здесь: их
единственный экземпляр нужен и выдаче состояния (001.29), и автоматике статусов (001.30).

Автоматика по heartbeat состояния требует: пороги Н-15 считаются по подряд идущим интервалам, а
переход ``provisioning`` → ``active`` ждёт применённой конфигурации. Поэтому ``on_heartbeat`` и
``on_missed_heartbeat`` — заглушки: они лишь отмечают удар и пропуск, статус выбирает 001.30.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.domain.nodes import NodeStatus, Version

# Деление §4.6 «Приоритет статусов»: ручной статус ставит и снимает только администратор, и
# автоматика не выводит из него. ``pending`` не входит ни в один список — из него ведёт ровно
# один переход (подтверждение), см. TRANSITIONS.
MANUAL: frozenset[NodeStatus] = frozenset[NodeStatus](("maintenance", "disabled", "suspended"))
AUTOMATIC: frozenset[NodeStatus] = frozenset[NodeStatus](
    ("provisioning", "active", "degraded", "offline")
)
# Снятие ручного статуса возвращает ноду к автоматике: далее её выбирают правила Н-15
# (§4.6 «Матрица переходов», строка Maintenance | Disabled | Suspended → Active или Offline).

# Матрица переходов §4.6, строка «Из» → допустимые «В». Строка «Любой автоматический →
# Maintenance | Disabled | Suspended» разложена по автоматическим статусам; строка «Любой →
# Удаление» не входит: вывод из эксплуатации — не статус, а ``nodes.decommissioned_at``.
_CLEARED: frozenset[NodeStatus] = frozenset[NodeStatus](("active", "offline"))
TRANSITIONS: dict[NodeStatus, frozenset[NodeStatus]] = {
    "pending": frozenset[NodeStatus](("provisioning",)),
    "provisioning": _CLEARED | MANUAL,
    "active": frozenset[NodeStatus](("degraded", "offline")) | MANUAL,
    "degraded": frozenset[NodeStatus](("active", "offline")) | MANUAL,
    "offline": frozenset[NodeStatus](("active",)) | MANUAL,
    "maintenance": _CLEARED,
    "disabled": _CLEARED,
    "suspended": _CLEARED,
}

# Доступ ноды к потоку состава (§4.6, столбец «Поток состава пользователей»): весь поток,
# только изменения, снимающие доступ («Только отзывы»), или ничего.
Composition = Literal["all", "revocations", "none"]


@dataclass(frozen=True, slots=True)
class StreamGate:
    """Строка вентиля для одного статуса: отказ целиком, поток конфигурации, поток состава,
    канал команд. ``offline`` в §4.6 описан как «да, при первом обмене»: ограничение временное
    и снимается самим обменом, а из статуса ноду выводят два успешных heartbeat подряд (Н-15),
    поэтому в вентиле ``offline`` неотличим от ``active``."""

    refused: bool
    config: bool
    composition: Composition
    commands: bool

    @property
    def filters_composition(self) -> bool:
        """Ответ в этом статусе отфильтрован по составу: часть строк скрыта вентилем, а курсор
        всё равно продвигается по выборке (§5.2). Отсюда — «выставляется ``resync_required``»:
        это правило о том, когда взводить колонку ``nodes.resync_required``, а не о том, что
        отдать ноде. В ответ идёт сохранённый признак ноды; запись и снятие — 001.29."""
        return self.composition == "revocations"


STREAM_GATE: dict[NodeStatus, StreamGate] = {
    "pending": StreamGate(refused=True, config=False, composition="none", commands=False),
    "provisioning": StreamGate(refused=False, config=True, composition="none", commands=True),
    "active": StreamGate(refused=False, config=True, composition="all", commands=True),
    "degraded": StreamGate(refused=False, config=True, composition="all", commands=True),
    "offline": StreamGate(refused=False, config=True, composition="all", commands=True),
    "maintenance": StreamGate(refused=False, config=True, composition="all", commands=True),
    "disabled": StreamGate(refused=False, config=True, composition="revocations", commands=False),
    "suspended": StreamGate(refused=False, config=True, composition="revocations", commands=False),
}


# Верхние границы курсоров и счётчиков — ширина колонок §4.2.3 (``int``, ``bigint``). Нода
# недоверенная (§11.3): без границы её число доходит до параметра запроса и роняет вставку
# ошибкой типа, а не отказом по контракту.
INT4_MAX = 2**31 - 1
INT8_MAX = 2**63 - 1


class HeartbeatIn(BaseModel):
    """Тело ``POST /agent/v1/heartbeat`` (§5.2): версии, которые нода сейчас исполняет, её
    собственное время и применённые курсоры обоих потоков. Время ноды — со смещением: перекос
    часов проверяется приёмом отчётов (§5.9), а наивная метка сравнению не поддаётся."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    agent_version: Version
    xray_version: Version
    node_time: AwareDatetime
    applied_config_version: int = Field(ge=0, le=INT4_MAX)
    applied_users_seq: int = Field(ge=0, le=INT8_MAX)


class NodeMetrics(BaseModel):
    """Тело ``POST /agent/v1/metrics`` — строка ``node_metrics`` §4.2.3: доли ресурсов, счётчики
    сети, число адресов онлайн и соединений по данным ОС (§4.7). Пороги §4.7, перевод в
    ``degraded`` и запись в партиционированную таблицу — 001.30."""

    model_config = ConfigDict(extra="forbid")

    ts: AwareDatetime
    cpu_pct: float = Field(ge=0, le=100)
    mem_pct: float = Field(ge=0, le=100)
    disk_pct: float = Field(ge=0, le=100)
    net_rx_bytes: int = Field(ge=0, le=INT8_MAX)
    net_tx_bytes: int = Field(ge=0, le=INT8_MAX)
    online_ips: int = Field(ge=0, le=INT4_MAX)
    connections: int = Field(ge=0, le=INT4_MAX)


class StatusService:
    """Правила статусов §4.6. Заглушка 001.28: правила-таблицы работают, база не читается."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @staticmethod
    def can_transition(current: NodeStatus, target: NodeStatus) -> bool:
        """Допустим ли переход по матрице §4.6. Переход в тот же статус — не переход: False."""
        return target in TRANSITIONS[current]

    async def on_heartbeat(self, node_id: uuid.UUID, at: dt.datetime, beat: HeartbeatIn) -> None:
        """Отметить успешный heartbeat. ``at`` — время Control Plane: пороги Н-15 и колонка
        ``nodes.last_heartbeat_at`` — его измерения, и отдать их часам ноды значило бы отдать
        ноде управление собственной автоматикой (часы вперёд — мёртвая нода числится живой).

        Тело приходит целиком, а не одним полем: кроме ``node_time`` (данные для сверки перекоса
        §5.9, не отметка) в нём версии агента и Xray — единственное место, где нода сообщает их
        после enrollment, и колонки ``nodes.agent_version``/``xray_version`` §4.2.3, которые
        администратор сверяет глазами (UC-01 шаг 6) и по которым идёт обновление парка (UC-11).
        Разобрать тело и выбросить его маршрут не вправе: 204 этого не показывает.

        Счётчики Н-15, возврат из ``offline`` (два успешных подряд), условие ``provisioning`` →
        ``active`` и запись версий — 001.30."""

    async def on_missed_heartbeat(self, node_id: uuid.UUID, at: dt.datetime) -> None:
        """Отметить пропущенный интервал. Перевод в ``offline`` на третьем подряд (Н-15) и
        матрица влияния на выдачу в подписке — 001.30."""

    async def on_metrics(self, node_id: uuid.UUID, metrics: NodeMetrics) -> None:
        """Принять телеметрию ноды. Запись строки ``node_metrics``, сверка с порогами §4.7 и
        переход ``active`` ↔ ``degraded`` — 001.30."""
