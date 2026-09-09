"""``/api/v1/admin/plans`` — тарифы (§4.10; UC-09 шаг 3, A4; R-30). Задача 001.18: CRUD со
схемами ``PlanIn``/``PlanPatch``/``Plan`` и заглушкой ``PlanService``; удаление — архивирование
(UC-09 A4: тариф с активными подписками не удаляется, а переводится в ``archived``)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.admin._common import CSRF, Admin, db_pool, permission
from app.domain.plans import Plan, PlanIn, PlanPatch, PlanService

router = APIRouter(prefix="/plans")


async def get_plan_service() -> PlanService:
    return PlanService(await db_pool())


Plans = Annotated[PlanService, Depends(get_plan_service)]


@router.get("", response_model=list[Plan], summary="Тарифы", openapi_extra=permission("plans.read"))
async def list_plans(admin: Admin, plans: Plans) -> list[Plan]:
    return await plans.list()


@router.post(
    "",
    response_model=Plan,
    status_code=status.HTTP_201_CREATED,
    summary="Создать тариф (UC-09 шаг 3)",
    dependencies=CSRF,
    openapi_extra=permission("plans.write"),
)
async def create_plan(body: PlanIn, admin: Admin, plans: Plans) -> Plan:
    return await plans.create(body)


@router.patch(
    "/{plan_id}",
    response_model=Plan,
    summary="Изменить тариф",
    dependencies=CSRF,
    openapi_extra=permission("plans.write"),
)
async def update_plan(plan_id: uuid.UUID, body: PlanPatch, admin: Admin, plans: Plans) -> Plan:
    return await plans.update(plan_id, body)


@router.delete(
    "/{plan_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Архивировать тариф (UC-09 A4)",
    dependencies=CSRF,
    openapi_extra=permission("plans.write"),
)
async def archive_plan(plan_id: uuid.UUID, admin: Admin, plans: Plans) -> None:
    await plans.archive(plan_id)
