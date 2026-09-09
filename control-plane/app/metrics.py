"""Экспозиция Prometheus для ``/metrics`` (§5.4): живость процесса, доступность базы и глубина
очереди задач по (очередь, статус). До задачи 001.68 — без библиотеки клиента: текстовый формат
собирается вручную; ошибка базы не роняет ответ — ``control_plane_db_up 0``."""

from __future__ import annotations

import logging

from app.db.pool import get_pool
from app.jobs import queue as jobs

log = logging.getLogger(__name__)


async def render() -> str:
    lines = [
        "# HELP control_plane_up Процесс Control Plane запущен.",
        "# TYPE control_plane_up gauge",
        "control_plane_up 1",
    ]
    pool = None
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            counts = await jobs.depth(conn)
    except Exception as exc:  # база недоступна — метрики живости всё равно отдаются
        log.warning("метрики: база недоступна: %s: %s", type(exc).__name__, exc)
        if pool is not None:  # устаревшие подключения (кэш типов, разрыв) — обновить
            await pool.expire_connections()
        lines += ["# HELP control_plane_db_up База доступна.", "# TYPE control_plane_db_up gauge"]
        lines.append("control_plane_db_up 0")
        return "\n".join(lines) + "\n"
    lines += ["# HELP control_plane_db_up База доступна.", "# TYPE control_plane_db_up gauge"]
    lines.append("control_plane_db_up 1")
    lines += [
        "# HELP control_plane_jobs Задачи в очереди по очереди и статусу.",
        "# TYPE control_plane_jobs gauge",
    ]
    for (queue, status), count in sorted(counts.items()):
        lines.append(f'control_plane_jobs{{queue="{queue}",status="{status}"}} {count}')
    return "\n".join(lines) + "\n"
