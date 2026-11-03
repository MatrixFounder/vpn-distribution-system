"""``/api/v1/admin/nodes`` — парк нод (§3.2, §4.6; UC-01 шаги 1–3, 6–7, A1–A2; UC-12 шаг 1;
R-02). Маршруты — 001.24; логика записи ноды, карточки, списка, состояния, bootstrap-токена,
подтверждения и отзыва identity — 001.25 (``NodeService`` на базе). Изменение записи, ручной
статус и вывод из эксплуатации отвечают 501: они не реализованы (ручные статусы — 001.30,
изменение и вывод — владелец не назначен, отчёт 001.25). Команды ноды — 001.28.

Inbound ноды (001.26, заглушки; логика — 001.27): список ``GET /admin/nodes/{id}/inbounds`` и
операции над одним inbound под ``/admin/inbounds/{id}`` — изменение цели с ``server_names`` и
включения, ротация ``short_ids`` и ключевой пары. Inbound принадлежит ноде (постановка §4.4: пара
«нода × профиль»), поэтому и разрешения — раздела ``nodes`` (security.md §7.1): чтение —
``nodes.read``, изменение и ротации — ``nodes.write``. Все четыре отвечают 501; тело изменения
проверяется моделью раньше — 422.

Все операции — под сессией администратора (``Admin``), мутации — под CSRF, одно разрешение на
операцию (R-35): ``nodes.read`` для чтения, ``nodes.write`` для остального.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status

from app.api.admin._common import CSRF, Admin, db_pool, permission
from app.domain.inbounds import Inbound, InboundPatch, InboundService
from app.domain.nodes import (
    ApproveIn,
    BootstrapToken,
    ManualStatusIn,
    Node,
    NodeIn,
    NodePatch,
    NodeService,
    NodeState,
)
from app.errors import not_implemented
from app.security.ca import get_ca_pem

router = APIRouter(prefix="/nodes")
inbounds_router = APIRouter(prefix="/inbounds")


async def get_node_service(anchor_pem: Annotated[str, Depends(get_ca_pem)]) -> NodeService:
    """Служба нод панели с сертификатом CA процесса — якорем доверия bootstrap-команды в ответе
    выдачи токена (security.md §7.1); ключа CA у неё нет — подписывать панели нечего."""
    return NodeService(await db_pool(), anchor_pem=anchor_pem)


Nodes = Annotated[NodeService, Depends(get_node_service)]


async def get_inbound_service() -> InboundService:
    return InboundService(await db_pool())


Inbounds = Annotated[InboundService, Depends(get_inbound_service)]

NOT_IMPLEMENTED: dict[int | str, dict[str, Any]] = {
    501: {"description": "не реализовано — `not_implemented` (отчёт 001.25)"}
}


@router.get("", response_model=list[Node], summary="Ноды", openapi_extra=permission("nodes.read"))
async def list_nodes(admin: Admin, nodes: Nodes) -> list[Node]:
    return await nodes.list()


@router.post(
    "",
    response_model=Node,
    status_code=status.HTTP_201_CREATED,
    summary="Создать запись ноды (UC-01 шаг 1)",
    dependencies=CSRF,
    openapi_extra=permission("nodes.write"),
)
async def create_node(body: NodeIn, admin: Admin, nodes: Nodes) -> Node:
    return await nodes.create(body, admin.id)


@router.get(
    "/{node_id}",
    response_model=Node,
    summary="Карточка ноды",
    openapi_extra=permission("nodes.read"),
)
async def get_node(node_id: uuid.UUID, admin: Admin, nodes: Nodes) -> Node:
    return await nodes.get(node_id)


@router.patch(
    "/{node_id}",
    response_model=Node,
    summary="Изменить запись ноды (не реализовано — 501)",
    dependencies=CSRF,
    responses=NOT_IMPLEMENTED,
    openapi_extra=permission("nodes.write"),
)
async def update_node(node_id: uuid.UUID, body: NodePatch, admin: Admin, nodes: Nodes) -> Node:
    return await nodes.update(node_id, body)


@router.delete(
    "/{node_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Вывести ноду из эксплуатации (§4.6 «Удаление», UC-12 A2; не реализовано — 501)",
    dependencies=CSRF,
    responses=NOT_IMPLEMENTED,
    openapi_extra=permission("nodes.write"),
)
async def decommission_node(node_id: uuid.UUID, admin: Admin, nodes: Nodes) -> None:
    await nodes.decommission(node_id, admin.id)


@router.post(
    "/{node_id}/bootstrap-token",
    response_model=BootstrapToken,
    status_code=status.HTTP_201_CREATED,
    summary="Выпустить одноразовый bootstrap-токен (UC-01 шаги 2–3, A1; Н-24)",
    dependencies=CSRF,
    openapi_extra=permission("nodes.write"),
)
async def issue_bootstrap_token(
    node_id: uuid.UUID, admin: Admin, nodes: Nodes, response: Response
) -> BootstrapToken:
    """Токен в ответе показывается один раз (§7.2) — ни прокси, ни браузер его не кэшируют.
    Рядом — сертификат CA: bootstrap-команда переносит его на VPS вместе с токеном, и агент
    проверяет им enrollment-server до того, как отдать токен."""
    response.headers["Cache-Control"] = "no-store"
    return await nodes.issue_bootstrap_token(node_id, admin.id)


@router.post(
    "/{node_id}/approve",
    response_model=Node,
    summary="Подтвердить ноду: pending → provisioning (UC-01 шаг 7) — сверенную identity",
    dependencies=CSRF,
    responses={
        409: {
            "description": "нода не в pending, выведена, без действующей identity, или "
            "действующая identity — не та, что сверена (`identity_changed`)"
        }
    },
    openapi_extra=permission("nodes.write"),
)
async def approve_node(node_id: uuid.UUID, body: ApproveIn, admin: Admin, nodes: Nodes) -> Node:
    return await nodes.approve(node_id, admin.id, body.cert_fingerprint)


@router.post(
    "/{node_id}/status",
    response_model=Node,
    summary="Ручной статус §4.6 (maintenance | disabled | suspended) или снятие; не реализовано",
    dependencies=CSRF,
    responses=NOT_IMPLEMENTED,
    openapi_extra=permission("nodes.write"),
)
async def set_manual_status(
    node_id: uuid.UUID, body: ManualStatusIn, admin: Admin, nodes: Nodes
) -> Node:
    return await nodes.set_manual_status(node_id, body.status, admin.id, reason=body.reason)


@router.post(
    "/{node_id}/revoke-identity",
    response_model=NodeState,
    summary="Отозвать identity ноды (Н-31; UC-12 шаг 1, UC-01 A2)",
    dependencies=CSRF,
    openapi_extra=permission("nodes.write"),
)
async def revoke_identity(node_id: uuid.UUID, admin: Admin, nodes: Nodes) -> NodeState:
    return await nodes.revoke_identity(node_id, admin.id)


@router.get(
    "/{node_id}/state",
    response_model=NodeState,
    summary="Состояние ноды: статус, курсоры, heartbeat, identity (UC-01 шаг 6)",
    openapi_extra=permission("nodes.read"),
)
async def node_state(node_id: uuid.UUID, admin: Admin, nodes: Nodes) -> NodeState:
    return await nodes.state(node_id)


@router.get(
    "/{node_id}/inbounds",
    response_model=list[Inbound],
    summary="Inbound ноды (не реализовано — 501)",
    responses=NOT_IMPLEMENTED,
    openapi_extra=permission("nodes.read"),
)
async def list_inbounds(node_id: uuid.UUID, admin: Admin) -> list[Inbound]:
    """Список inbound ноды с ``error_state``/``error_since`` — 001.27."""
    raise not_implemented("admin.inbounds.list")


@inbounds_router.patch(
    "/{inbound_id}",
    response_model=Inbound,
    summary="Изменить inbound: цель с server_names, включение (не реализовано — 501)",
    dependencies=CSRF,
    responses=NOT_IMPLEMENTED,
    openapi_extra=permission("nodes.write"),
)
async def update_inbound(inbound_id: uuid.UUID, body: InboundPatch, admin: Admin) -> Inbound:
    """Смена цели (``InboundService.set_target``) и включение (``set_enabled``) — одной
    версией конфигурации на изменение (001.27)."""
    raise not_implemented("admin.inbounds.update")


@inbounds_router.post(
    "/{inbound_id}/rotate-short-ids",
    response_model=Inbound,
    summary="Плановая ротация short_ids с окном перекрытия Н-33 (не реализовано — 501)",
    dependencies=CSRF,
    responses=NOT_IMPLEMENTED,
    openapi_extra=permission("nodes.write"),
)
async def rotate_short_ids(inbound_id: uuid.UUID, admin: Admin, inbounds: Inbounds) -> Inbound:
    return await inbounds.rotate_short_ids(inbound_id)


@inbounds_router.post(
    "/{inbound_id}/rotate-keypair",
    response_model=Inbound,
    summary="Сменить ключевую пару REALITY при компрометации (не реализовано — 501)",
    dependencies=CSRF,
    responses=NOT_IMPLEMENTED,
    openapi_extra=permission("nodes.write"),
)
async def rotate_keypair(inbound_id: uuid.UUID, admin: Admin, inbounds: Inbounds) -> Inbound:
    """Основание смены ключевой пары по §4.4 одно — компрометация или изъятие ноды; тела у
    операции нет."""
    return await inbounds.rotate_keypair(inbound_id, "compromise")
