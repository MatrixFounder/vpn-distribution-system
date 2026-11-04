"""Общее для подроутеров панели: зависимости сессии администратора, объявление разрешения
операции (R-35) и контекст журнала аудита.

Разрешение операция объявляет одним вызовом ``permission(name)`` в ``openapi_extra``; проверяет
его ``declared_permission``, подключённая к роутеру панели целиком (001.46): объявленное в
OpenAPI и проверяемое — одно и то же имя."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Request

from app.db.pool import db_pool
from app.security.audit import AuditContext
from app.security.csrf import require_csrf
from app.security.deps import Admin, audit_context
from app.security.rbac import PERMISSIONS

CSRF = [Depends(require_csrf)]


def permission(name: str) -> dict[str, Any]:
    """``openapi_extra`` операции: одно разрешение на операцию (R-35); проверка —
    ``declared_permission`` роутера панели. Имя вне реестра роняет импорт модуля маршрутов:
    опечатка не должна дожить до запроса, где она закрыла бы операцию для всех."""
    if name not in PERMISSIONS:
        raise ValueError(f"разрешение {name!r} не объявлено в app.security.rbac.PERMISSIONS")
    return {"x-permission": name}


async def admin_audit_context(request: Request, admin: Admin) -> AuditContext:
    """Контекст журнала для доменного метода, меняющего данные (``audit.record`` в его
    транзакции, 001.46)."""
    return audit_context(request, admin)


Audit = Annotated[AuditContext, Depends(admin_audit_context)]

__all__ = ["CSRF", "Admin", "Audit", "db_pool", "permission"]
