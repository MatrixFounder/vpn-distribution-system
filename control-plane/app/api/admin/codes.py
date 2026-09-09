"""``/api/v1/admin/codes`` — Redeem- и Promo-коды (§4.14, §16.8; UC-09 шаги 4–5; R-32, R-33).
Задача 001.18: один код, партия, экспорт партии (CSV ``code,expires_at,plan``), использования —
на заглушке ``CodeService``; контрольная сумма кодов настоящая (``domain.codes``)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from app.api.admin._common import CSRF, Admin, db_pool, permission
from app.domain.codes import Code, CodeService, CodeSpec, Redemption

router = APIRouter(prefix="/codes")


async def get_code_service() -> CodeService:
    return CodeService(await db_pool())


Codes = Annotated[CodeService, Depends(get_code_service)]


class BatchIn(BaseModel):
    """Партия кодов с общими свойствами (UC-09 шаг 4)."""

    count: int = Field(ge=1, le=10000)
    spec: CodeSpec


class BatchOut(BaseModel):
    batch_id: uuid.UUID
    count: int


@router.post(
    "",
    response_model=Code,
    status_code=status.HTTP_201_CREATED,
    summary="Создать один код (§4.14)",
    dependencies=CSRF,
    openapi_extra=permission("codes.write"),
)
async def create_code(body: CodeSpec, admin: Admin, codes: Codes) -> Code:
    return await codes.create(body, admin.id)


@router.post(
    "/batch",
    response_model=BatchOut,
    status_code=status.HTTP_201_CREATED,
    summary="Сгенерировать партию (UC-09 шаг 4)",
    dependencies=CSRF,
    openapi_extra=permission("codes.write"),
)
async def create_batch(body: BatchIn, admin: Admin, codes: Codes) -> BatchOut:
    batch_id = await codes.create_batch(body.count, body.spec, admin.id)
    return BatchOut(batch_id=batch_id, count=body.count)


@router.get(
    "/batch/{batch_id}/export",
    summary="Экспорт партии — CSV (UC-09 шаг 5)",
    response_class=Response,
    responses={200: {"content": {"text/csv": {"schema": {"type": "string"}}}}},
    openapi_extra=permission("codes.read"),
)
async def export_batch(batch_id: uuid.UUID, admin: Admin, codes: Codes) -> Response:
    """Коды партии в открытом виде — вложение, без кэширования (секреты в ответе)."""
    lines = await codes.export(batch_id)
    return Response(
        "\r\n".join(lines) + "\r\n",
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="codes-{batch_id}.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.get(
    "/{code_id}/redemptions",
    response_model=list[Redemption],
    summary="Использования кода",
    openapi_extra=permission("codes.read"),
)
async def list_redemptions(code_id: uuid.UUID, admin: Admin, codes: Codes) -> list[Redemption]:
    return await codes.redemptions(code_id)
