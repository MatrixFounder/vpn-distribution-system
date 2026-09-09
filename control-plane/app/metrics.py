"""Экспозиция Prometheus для ``/metrics`` (§5.4): живость процесса, доступность базы, глубина
очереди задач по (очередь, статус) — ряд ``status="dead"`` и есть DLQ, источник алерта 001.68
(``sum(control_plane_jobs{status="dead"})``), — и наибольшее ожидание задачи по очередям
(``jobs_wait_seconds``: выбранные за 5 минут и ещё не выбранные готовые) рядом с бюджетом
критичного пути (половина p95 Н-13 — крыша, а не строка раскладки §5.4). До задачи 001.68 —
без библиотеки клиента: текстовый формат собирается вручную; ошибка базы не роняет ответ —
``control_plane_db_up 0``."""

from __future__ import annotations

import datetime as dt
import logging

from app.db.pool import get_pool
from app.jobs import queue as jobs
from app.jobs.worker import CRITICAL_WAIT_BUDGET_SECONDS

WAIT_WINDOW = dt.timedelta(minutes=5)

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
            waits = await jobs.wait_seconds(conn, WAIT_WINDOW)
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
    lines += [
        "# HELP control_plane_jobs_wait_seconds Наибольшее ожидание задачи от готовности до "
        "выборки среди выбранных за 5 минут и ещё не выбранных готовых.",
        "# TYPE control_plane_jobs_wait_seconds gauge",
    ]
    for queue in jobs.QUEUES:
        lines.append(f'control_plane_jobs_wait_seconds{{queue="{queue}"}} {waits[queue]:.3f}')
    lines += [
        "# HELP control_plane_jobs_wait_budget_seconds Крыша ожидания критичного пути "
        "(половина p95 Н-13; строка раскладки §5.4 — 1 с).",
        "# TYPE control_plane_jobs_wait_budget_seconds gauge",
        f'control_plane_jobs_wait_budget_seconds{{queue="critical"}} '
        f"{CRITICAL_WAIT_BUDGET_SECONDS:g}",
    ]
    return "\n".join(lines) + "\n"
