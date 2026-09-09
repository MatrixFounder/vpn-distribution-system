"""``/api/v1/admin/groups`` — группы доступа и тарифицируемые группы (§4.8, §4.9; UC-09 шаги
1–2, 6, A2; R-18). Задача 001.18: CRUD ``/access`` и ``/billing`` и новый интервал
коэффициента ``POST /billing/{id}/multiplier`` на заглушке ``GroupService``; проверка A2
(0.0…10.0 с шагом 0.1) — в схеме ``MultiplierIn``."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.admin._common import CSRF, Admin, db_pool, permission
from app.domain.groups import (
    AccessGroup,
    AccessGroupIn,
    AccessGroupPatch,
    BillingGroup,
    BillingGroupIn,
    BillingGroupPatch,
    GroupService,
    Multiplier,
    MultiplierIn,
)

router = APIRouter(prefix="/groups")


async def get_group_service() -> GroupService:
    return GroupService(await db_pool())


Groups = Annotated[GroupService, Depends(get_group_service)]


@router.get(
    "/access",
    response_model=list[AccessGroup],
    summary="Группы доступа",
    openapi_extra=permission("groups.read"),
)
async def list_access_groups(admin: Admin, groups: Groups) -> list[AccessGroup]:
    return await groups.list_access()


@router.post(
    "/access",
    response_model=AccessGroup,
    status_code=status.HTTP_201_CREATED,
    summary="Создать группу доступа (UC-09 шаг 2)",
    dependencies=CSRF,
    openapi_extra=permission("groups.write"),
)
async def create_access_group(body: AccessGroupIn, admin: Admin, groups: Groups) -> AccessGroup:
    return await groups.create_access(body)


@router.patch(
    "/access/{group_id}",
    response_model=AccessGroup,
    summary="Изменить группу доступа",
    dependencies=CSRF,
    openapi_extra=permission("groups.write"),
)
async def update_access_group(
    group_id: uuid.UUID, body: AccessGroupPatch, admin: Admin, groups: Groups
) -> AccessGroup:
    return await groups.update_access(group_id, body)


@router.delete(
    "/access/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить группу доступа",
    dependencies=CSRF,
    openapi_extra=permission("groups.write"),
)
async def delete_access_group(group_id: uuid.UUID, admin: Admin, groups: Groups) -> None:
    await groups.delete_access(group_id)


@router.get(
    "/billing",
    response_model=list[BillingGroup],
    summary="Тарифицируемые группы",
    openapi_extra=permission("groups.read"),
)
async def list_billing_groups(admin: Admin, groups: Groups) -> list[BillingGroup]:
    return await groups.list_billing()


@router.post(
    "/billing",
    response_model=BillingGroup,
    status_code=status.HTTP_201_CREATED,
    summary="Создать тарифицируемую группу (UC-09 шаг 1)",
    dependencies=CSRF,
    openapi_extra=permission("groups.write"),
)
async def create_billing_group(body: BillingGroupIn, admin: Admin, groups: Groups) -> BillingGroup:
    return await groups.create_billing(body)


@router.patch(
    "/billing/{group_id}",
    response_model=BillingGroup,
    summary="Изменить тарифицируемую группу",
    dependencies=CSRF,
    openapi_extra=permission("groups.write"),
)
async def update_billing_group(
    group_id: uuid.UUID, body: BillingGroupPatch, admin: Admin, groups: Groups
) -> BillingGroup:
    return await groups.update_billing(group_id, body)


@router.delete(
    "/billing/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить тарифицируемую группу",
    dependencies=CSRF,
    openapi_extra=permission("groups.write"),
)
async def delete_billing_group(group_id: uuid.UUID, admin: Admin, groups: Groups) -> None:
    await groups.delete_billing(group_id)


@router.post(
    "/billing/{group_id}/multiplier",
    response_model=Multiplier,
    status_code=status.HTTP_201_CREATED,
    summary="Новый интервал коэффициента (§4.9; UC-09 шаг 1, A2)",
    dependencies=CSRF,
    openapi_extra=permission("groups.write"),
)
async def add_multiplier(
    group_id: uuid.UUID, body: MultiplierIn, admin: Admin, groups: Groups
) -> Multiplier:
    return await groups.add_multiplier(group_id, body)
