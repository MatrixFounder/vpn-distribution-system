"""Гранты квоты пары «пользователь × нода» (постановка §4.11 «Автономный режим ноды»;
interfaces.md §5.2 «Грант»; data-model.md §4.2.5 ``quota_grants``; R-26, Н-17в; функция F-03
«выдача и учёт грантов квоты»).

Задача 001.33: схемы запроса и гранта, ``QuotaService`` с фиксированными значениями и одним
настоящим правилом — грант выдаётся только ноде, которой вентиль §4.6 открывает весь поток
состава (грант — разрешение обслуживать пользователя автономно, а ноде в ``disabled`` или
``suspended`` поток несёт только отзывы). Правило размера ``min(Н-17 в единицах списания,
остаток периода) / N``, замена прежнего гранта (``superseded_at``), подтверждение расхода
отчётами (``consumed_bytes``), ограничение суммы непогашенных грантов Н-17 (AC-08), размер по
умолчанию (ОВ-A7) и проверка «пользователь в составе ноды» — 001.36. Локальное применение
гранта на ноде — 001.57.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import asyncpg
from pydantic import BaseModel, ConfigDict, Field

from app.accounting.canonical import CanonicalUuid
from app.domain.composition import STUB_QUOTA_GRANT_BYTES
from app.domain.nodes import Node, NodeStatus
from app.domain.statuses import INT8_MAX, STREAM_GATE
from app.errors import ApiError

# Размер гранта заглушки (1 ГиБ) объявлен в домене (``domain.composition``, импорт выше): учёт
# зависит от домена, домен от учёта — нет (правило слоёв в ``accounting/.AGENTS.md``); настоящий
# размер и передача гранта в поток состава через граф зависимостей — 001.36 (R-26, ОВ-A7).
# Номер выдачи заглушки — первый: ``issued_seq`` растёт с каждой выдачей паре (§5.2).
STUB_ISSUED_SEQ = 1
# Пауза после отказа в гранте — интервал повторного запроса до перемены статуса: ``disabled`` и
# ``suspended`` снимает администратор (§4.6), ``provisioning`` → ``active`` наступает по
# применённой конфигурации и heartbeat (001.30); в обоих случаях нода живёт по прежнему гранту,
# и опрашивать чаще раза в пять минут незачем (как ``composition.RETRY_AFTER_APPROVAL``).
RETRY_AFTER_GRANT = 300


class QuotaRequestIn(BaseModel):
    """Тело ``POST /agent/v1/quota/request`` (§5.2): пользователь и израсходованный им объём
    текущего гранта по данным ноды. Расход — самоотчёт недоверенной ноды (§11.3): гранты
    погашаются отчётами о трафике, а не этим числом; оно — повод для запроса (50 % гранта,
    §4.11) и сигнал расхождения для 001.36."""

    model_config = ConfigDict(extra="forbid")

    user_id: CanonicalUuid
    consumed_bytes: int = Field(
        strict=True, ge=0, le=INT8_MAX, description="в единицах списания, самоотчёт"
    )


class QuotaGrant(BaseModel):
    """Выданный грант: объём в единицах списания (уже с коэффициентом ноды, §4.11) и номер
    выдачи — по нему нода отличает новый грант от устаревшего: грант заменяет остаток
    прежнего, остатки не суммируются (§5.2)."""

    quota_grant_bytes: int = Field(strict=True, ge=0, le=INT8_MAX)
    issued_seq: int = Field(strict=True, ge=1, le=INT8_MAX)


def grant_unavailable(status: NodeStatus) -> ApiError:
    """403: ноде в этом статусе грант не выдаётся (§4.6, столбец «Поток состава»: только отзывы
    или ничего). Нода продолжает по прежнему гранту до его исчерпания, затем локально переводит
    пользователя в ``suspended_quota`` (§4.11 «Автономный режим ноды»)."""
    return ApiError(
        "grant_unavailable",
        "грант квоты не выдаётся ноде в этом статусе",
        status=403,
        details={"node_status": status},
        headers={"Retry-After": str(RETRY_AFTER_GRANT)},
    )


class QuotaService:
    """Гранты квоты поверх пула asyncpg. Заглушка 001.33: база не читается и не пишется."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def grant(self, node: Node, user_id: uuid.UUID, consumed_bytes: int) -> QuotaGrant:
        """Выдать пользователю новый грант на ноде по запросу агента. Принимает ноду целиком
        (отклонение от сигнатуры задачи ``grant(user_id, node_id) -> int``): вентиль §4.6
        решает и здесь, и правило обязано быть доступно самой службе, а не только маршруту;
        возвращает грант целиком — ``issued_seq`` живёт в строке ``quota_grants`` и известен
        только службе, а маршрут обязан отдать его ноде. ``consumed_bytes`` — заявленный расход,
        принят от маршрута, чтобы тот не выбрасывал объявленное контрактом поле; его назначение
        решает 001.36.

        Там же: пользователь обязан присутствовать в составе этой ноды (иначе любая нода
        выпрашивала бы гранты чужим пользователям), размер по правилу §5.2, ограничение суммы
        Н-17. Заглушка отдаёт ``STUB_QUOTA_GRANT_BYTES`` и ``STUB_ISSUED_SEQ``."""
        if STREAM_GATE[node.status].composition != "all":
            raise grant_unavailable(node.status)
        return QuotaGrant(quota_grant_bytes=STUB_QUOTA_GRANT_BYTES, issued_seq=STUB_ISSUED_SEQ)

    async def on_report(
        self, conn: asyncpg.Connection, node_id: uuid.UUID, billable: Mapping[uuid.UUID, int]
    ) -> None:
        """Подтвердить расход грантов принятым отчётом: ``consumed_bytes`` последнего
        непогашенного гранта каждой пары «пользователь × нода» растёт на списанный объём
        (R-26). Один вызов на отчёт со всеми строками и на подключении приёма (отклонение от
        сигнатуры задачи ``on_report(user_id, node_id, billable)``): доставка отчётов
        at-least-once, и подтверждение расхода обязано лечь в ту же транзакцию, что и ключ
        отчёта, — иначе повтор отчёта ответит ``duplicate`` на несделанную работу; строк в
        отчёте до ``REPORT_MAX_LINES``, и обновление по одной строке — столько же обращений к
        базе внутри открытой транзакции. Вызывает ``AccountingService.accept_report`` (001.77),
        реализация — 001.36; заглушка ничего не делает."""
