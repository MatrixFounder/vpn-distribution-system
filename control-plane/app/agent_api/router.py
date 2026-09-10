"""Node API ``/agent/v1`` (interfaces.md §5.2): enrollment (``agent_api/enroll.py``, 001.24 —
заглушка со схемами); состояние, отчёты и команды — заглушки 501 задачи 001.10 до задач 001.28,
001.33. Аутентификация нод — mTLS на nginx (§7.1), отпечаток клиента приходит в
``X-Client-Fingerprint``; enrollment — единственный маршрут без него."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.agent_api.enroll import router as enroll
from app.errors import not_implemented

router = APIRouter(prefix="/agent/v1", tags=["agent"])
router.include_router(enroll)


@router.get("/state", summary="Состояние: дельты, курсоры, команды (001.2x)")
async def state(
    config_version: Annotated[int, Query(ge=0)],
    users_seq: Annotated[int, Query(ge=0)],
    generation: Annotated[int, Query(ge=0)],
) -> dict[str, str]:
    """Курсоры агента (§5.2) — типизированы уже в заглушке: единый формат 422 проверяется здесь.
    Форма ``?full=1`` без курсоров (§5.2, полный снапшот) в заглушке даёт 422 — её вводит 001.2x."""
    raise not_implemented("agent.state")
