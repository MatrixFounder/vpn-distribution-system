"""Общее для подроутеров панели: зависимости сессии администратора и объявление разрешения
операции (R-35) в OpenAPI до появления матрицы «роль × операция» (001.46)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends

from app.db.pool import db_pool
from app.security.csrf import require_csrf
from app.security.deps import CurrentAdmin, current_admin

Admin = Annotated[CurrentAdmin, Depends(current_admin)]
CSRF = [Depends(require_csrf)]


def permission(name: str) -> dict[str, Any]:
    """``openapi_extra`` операции: одно разрешение на операцию (R-35). Проверка разрешения —
    ``require_permission`` (001.46); до неё операция защищена сессией администратора."""
    return {"x-permission": name}


__all__ = ["CSRF", "Admin", "db_pool", "permission"]
