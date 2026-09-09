"""Node API ``/agent/v1`` (interfaces.md §5.2) — заглушки 501 задачи 001.10; enrollment, состояние,
отчёты и команды появляются в задачах 001.2x. Аутентификация нод — mTLS на nginx (§7.1),
отпечаток клиента приходит в ``X-Client-Fingerprint``."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.errors import not_implemented

router = APIRouter(prefix="/agent/v1", tags=["agent"])


@router.post("/enroll", summary="Enrollment ноды по bootstrap-токену (001.2x)")
async def enroll() -> dict[str, str]:
    raise not_implemented("agent.enroll")


@router.get("/state", summary="Состояние: дельты, курсоры, команды (001.2x)")
async def state(
    config_version: Annotated[int, Query(ge=0)],
    users_seq: Annotated[int, Query(ge=0)],
    generation: Annotated[int, Query(ge=0)],
) -> dict[str, str]:
    """Курсоры агента (§5.2) — типизированы уже в заглушке: единый формат 422 проверяется здесь.
    Форма ``?full=1`` без курсоров (§5.2, полный снапшот) в заглушке даёт 422 — её вводит 001.2x."""
    raise not_implemented("agent.state")
