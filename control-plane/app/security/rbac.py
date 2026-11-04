"""Реестр разрешений панели (R-35; security.md §7.1 «Матрица ролей §5.11»; interfaces.md §5.1).

Задача 001.46: полный перечень имён разрешений — тех, что уже объявляют операции панели
(``x-permission``), и тех, что объявят маршруты 001.31, 001.48, 001.49, 001.85, 001.89. Роли у
разрешения — заглушка: все разрешения у ``super_admin``; матрица ролей по security.md — 001.48.
Проверка — ``app.security.deps``: ``declared_permission`` на роутере панели (имя из
``x-permission`` маршрута) и ``require_permission(name)`` для маршрутов вне него; здесь только
данные.

Разрешения на восстановление из копии нет: это процедура на хосте (§17.12, 001.67), а не
операция API.
"""

from __future__ import annotations

from typing import Literal, get_args

# Перечисление ``admin_role`` (data-model.md §4.2.1).
Role = Literal["super_admin", "admin", "operator", "support"]
ROLES: tuple[Role, ...] = get_args(Role)

_STUB: frozenset[Role] = frozenset({"super_admin"})

PERMISSIONS: dict[str, frozenset[Role]] = {
    # Дашборд, тарифы, группы, коды — операции 001.18 и 001.20.
    "dashboard.read": _STUB,
    "plans.read": _STUB,
    "plans.write": _STUB,
    "groups.read": _STUB,
    "groups.write": _STUB,
    "codes.read": _STUB,
    "codes.write": _STUB,
    "codes.export": _STUB,
    # Ноды: запись — создание, изменение и вывод (001.87), bootstrap-токен, подтверждение,
    # изменение и ротации inbound (001.26), обращение провайдера (001.69); чтение — записи,
    # доступность из стран (001.49), inbound, команды и их результаты (001.31).
    "nodes.read": _STUB,
    "nodes.write": _STUB,
    "nodes.revoke_identity": _STUB,
    "nodes.manual_status": _STUB,
    "nodes.update_agent": _STUB,
    "nodes.restart_xray": _STUB,
    "nodes.commands": _STUB,
    # Пользователи и поддержка (001.48, 001.49): поиск и карточка; действия карточки — блокировка
    # и разблокировка, тариф, продление, начисление и корректировка баланса, сброс credentials,
    # ручное подтверждение адреса; просмотр subscription URL; режим «от лица пользователя»;
    # список и подтверждение запросов второго подтверждения.
    "users.read": _STUB,
    "users.write": _STUB,
    "users.view_subscription_url": _STUB,
    "users.impersonate": _STUB,
    "approvals.approve": _STUB,
    # Настройки и окна обслуживания (001.49, 001.85), журнал аудита (001.48).
    "settings.read": _STUB,
    "settings.write": _STUB,
    "audit.read": _STUB,
    # Учётные записи администраторов (001.49, 001.89): раздел и свой профиль.
    "admins.manage": _STUB,
    "admins.self": _STUB,
}


def permissions_of(role: str) -> list[str]:
    """Разрешения роли по реестру, по алфавиту — список для ответа ``GET /admin/auth/session``
    (по нему панель скрывает недоступные действия, 001.64)."""
    return sorted(name for name, roles in PERMISSIONS.items() if role in roles)
