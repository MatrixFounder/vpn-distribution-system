"""Журнал аудита (§4.16; security.md §7.1; data-model.md §4.2.6 ``audit_log``; R-23, R-36).

Задача 001.46: ``AuditContext`` и ``record`` — заглушка без записи. Запись строки
``audit_log`` (append-only, без адресов почты и паролей в ``old_value``/``new_value``) — 001.48.

Механизм: доменный метод, меняющий данные, принимает ``ctx: AuditContext`` и вызывает ``record``
на подключении своей транзакции — изменение и его след в журнале атомарны. Транзакцию открывает
сам метод, поэтому маршрут после коммита записать журнал в той же транзакции не может. Отказ по
разрешению (``result = "denied"``) пишет проверка разрешения (``app.security.deps``:
``declared_permission`` роутера панели и ``require_permission``) отдельной короткой транзакцией.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

AuditResult = Literal["success", "denied", "error"]
# Перечисление ``actor_type`` (data-model.md §4.2.6).
ActorType = Literal["admin", "user", "system"]


@dataclass(frozen=True, slots=True)
class AuditContext:
    """Кто действует: колонки актора ``audit_log``. Всё, кроме вида актора, необязательно, как
    в таблице: у ``system`` (планировщик, команда хоста ``admin reset-2fa``, 001.47) нет ни
    актора, ни сессии; у пользователя (001.16, 001.17, 001.42) нет роли; у неудачного входа с
    неизвестным адресом нет идентификатора. ``session_id`` — несекретная метка
    (``Session.handle``), не значение cookie. ``impersonated_user_id`` — пользователь, от лица
    которого администратор смотрит карточку (§5.11, В-36; 001.48)."""

    actor_type: ActorType
    actor_id: uuid.UUID | None = None
    actor_role: str | None = None
    session_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    impersonated_user_id: uuid.UUID | None = None


async def record(
    conn: Any,
    ctx: AuditContext,
    action: str,
    entity: tuple[str, str],
    old: Any,
    new: Any,
    result: AuditResult,
) -> None:
    """Строка журнала: ``action`` над ``entity`` (тип и идентификатор), прежнее и новое значение,
    исход. Заглушка 001.46 ничего не пишет и подключение не трогает; запись — 001.48."""
