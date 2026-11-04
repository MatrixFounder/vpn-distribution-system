"""Раздел ``/api/v1/admin`` — панель администратора (interfaces.md §5.1). Подроутеры:
``plans``, ``groups``, ``codes`` (001.18, заглушки), ``nodes`` (001.24, заглушки) и
``inbounds`` (001.26, заглушки), далее ``users``, ``dashboard``, ``settings``, ``audit``
(001.49). Общие зависимости раздела (``_common``): ``current_admin`` (сессия администратора;
роль, статус и язык — из ``admin_users``, 001.46), ``require_csrf`` на мутациях; разрешение
операции (R-35) объявляется в OpenAPI как ``x-permission`` и с 001.46 проверяется
``declared_permission`` на всём роутере: отказ — 403 ``permission_denied`` и запись ``denied`` в
журнал. Вход администратора ``/admin/auth/*`` — отдельный роутер (``auth.py``) без проверки
разрешения: вход идёт до сессии."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.admin._common import Admin, permission
from app.api.admin.codes import router as codes
from app.api.admin.groups import router as groups
from app.api.admin.nodes import inbounds_router as inbounds
from app.api.admin.nodes import router as nodes
from app.api.admin.plans import router as plans
from app.errors import not_implemented
from app.security.deps import declared_permission

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(declared_permission)])


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
router.include_router(nodes)
router.include_router(inbounds)
