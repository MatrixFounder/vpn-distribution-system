"""Node API ``/agent/v1`` (interfaces.md §5.2): enrollment (``enroll.py``, 001.24), состояние и
подтверждение (``state.py``), heartbeat и телеметрия (``heartbeat.py``), результат команды
(``commands.py``) — заглушки со схемами задачи 001.28; отчёты о трафике и запрос гранта квоты —
001.33.

Аутентификация нод — mTLS на nginx (§7.1): отпечаток клиента приходит в ``X-Client-Fingerprint``,
токен identity — в ``X-Node-Identity``, версия агента — в ``X-Agent-Version`` (``deps.py``).
Enrollment — единственный маршрут раздела без всех трёх заголовков: он обслуживается отдельным
``server`` прокси, а версии агента и Xray несёт его тело.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.agent_api.commands import router as commands
from app.agent_api.enroll import router as enroll
from app.agent_api.heartbeat import router as heartbeat
from app.agent_api.state import router as state

router = APIRouter(prefix="/agent/v1", tags=["agent"])
router.include_router(enroll)
router.include_router(state)
router.include_router(heartbeat)
router.include_router(commands)
