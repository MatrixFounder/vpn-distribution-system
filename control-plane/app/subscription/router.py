"""Subscription-эндпоинт ``/s`` (interfaces.md §5.3) — заглушка 501 задачи 001.10; выдача по
``User-Agent``, ``base64``/``singbox`` и страница подписки — задачи 001.3x."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.errors import not_implemented
from app.security.deps import redis_required

# Лимит частоты по токену (§5.12): без Redis — 503 (fail-closed, §9.1).
router = APIRouter(prefix="/s", tags=["subscription"], dependencies=[Depends(redis_required)])


@router.get("/{token}", summary="Подписка по токену (001.3x)")
async def subscription(token: str) -> dict[str, str]:
    raise not_implemented("subscription.get")
