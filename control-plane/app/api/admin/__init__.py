"""Раздел ``/api/v1/admin`` — панель администратора (interfaces.md §5.1). Подроутеры:
``plans``, ``groups``, ``codes`` (001.18, заглушки), далее ``users``, ``nodes``, ``dashboard``,
``settings``, ``audit``, ``auth`` (001.24…, 001.46…). Общие зависимости раздела (``_common``):
``current_admin`` (сессия администратора, минимальный вид до 001.47), ``require_csrf`` на
мутациях; разрешение операции (R-35) объявляется в OpenAPI как ``x-permission`` и до 001.46
не проверяется (``require_permission`` — заглушка)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.admin._common import Admin, permission
from app.api.admin.codes import router as codes
from app.api.admin.groups import router as groups
from app.api.admin.plans import router as plans
from app.errors import not_implemented

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get(
    "/dashboard",
    summary="Сводка администратора (001.44)",
    openapi_extra=permission("dashboard.read"),
)
async def dashboard(admin: Admin) -> dict[str, str]:
    raise not_implemented("admin.dashboard")


router.include_router(plans)
router.include_router(groups)
router.include_router(codes)
