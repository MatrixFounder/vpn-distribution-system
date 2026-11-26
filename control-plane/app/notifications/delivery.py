"""Типы и очередь задач доставки уведомлений (задача 001.51; interfaces.md §5.4).

Листовой модуль без зависимостей проекта: его импортируют и ``app.notifications.events`` (ставит
задачи), и обработчики ``app.jobs.handlers.notifications`` (регистрируют типы). Направление
импорта одно: обработчики (``app.jobs.handlers``) могут импортировать ``app.notifications``, а
``app.notifications.events`` никогда не импортирует ``app.jobs.handlers`` — пакет обработчиков
грузит все модули обработчиков, а обработчики-источники (001.83, 001.35, 001.30, 001.37d)
импортируют ``emit``: обратное ребро дало бы цикл. Поэтому ``channels_for`` (001.52) живёт в
``app.notifications``, а не в модуле обработчиков.
"""

from __future__ import annotations

QUEUE = "background"
NOTIFY_EMAIL = "notify.email"
NOTIFY_WEBHOOK = "notify.webhook"
