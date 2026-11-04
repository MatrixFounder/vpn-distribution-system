"""Второй фактор администраторов — TOTP и резервные коды (§7.1, R-36; data-model.md §4.2.1
``admin_users.totp_secret_enc``, ``admin_recovery_codes``).

Задача 001.46: интерфейс ``Totp`` с заглушками. Секрет TOTP шифруется на уровне приложения
(§7.2), резервные коды хранятся хешами и гасятся при использовании — 001.47.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.errors import not_implemented


class Totp:
    """TOTP администраторов поверх пула asyncpg. Заглушка 001.46: база не читается и не
    пишется, каждая операция — 501."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def provision(self, admin_id: uuid.UUID) -> str:
        """URI провижининга (``otpauth://``) нового секрета — 001.47."""
        raise not_implemented("admin.totp.provision")

    async def verify(self, admin_id: uuid.UUID, code: str) -> bool:
        """Код TOTP верен для секрета администратора — 001.47."""
        raise not_implemented("admin.totp.verify")

    async def recovery_codes(self, admin_id: uuid.UUID) -> list[str]:
        """Новый набор резервных кодов (прежние гасятся) — 001.47."""
        raise not_implemented("admin.totp.recovery_codes")
