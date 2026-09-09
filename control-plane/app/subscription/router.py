"""Subscription-эндпоинт ``/s`` (interfaces.md §5.3) — заглушка 501 задачи 001.10; выдача по
``User-Agent``, ``base64``/``singbox`` и страница подписки — задачи 001.3x."""

from __future__ import annotations

from fastapi import APIRouter

from app.errors import not_implemented

router = APIRouter(prefix="/s", tags=["subscription"])


@router.get("/{token}", summary="Подписка по токену (001.3x)")
async def subscription(token: str) -> dict[str, str]:
    raise not_implemented("subscription.get")
