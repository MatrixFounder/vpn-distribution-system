"""Раздел ``/agent/v1`` на заглушках чужих служб (состав, команды, учёт): заголовки ноды и подмена
поиска ноды по identity (задача 001.25).

С 001.25 ``current_node`` ищет ноду в базе, а тесты заглушек 001.28 и 001.33 базу не трогают
(часовой ``NoDatabase``). ``node_as`` подменяет ровно поиск: версия агента (426) и форма признаков
(401) по-прежнему проверяются настоящими ``supported_agent_version`` и ``presented_identity``, так
что тесты формы заголовков и воспроизведение записанных фикстур идут через боевые правила.
Настоящий поиск по отпечатку, отзыв и сверка токена — ``tests/e2e/test_nodes.py`` на живой базе.

С 001.29 поток состава читает строки ``node_user_state`` из базы. Раскладка фикстур контракта
(четыре строки с номерами 1…4) живёт здесь: герметичные прогоны подставляют её службе состава
(``RecordedComposition``), сквозные тесты заводят ту же раскладку строками живой базы
(``tests/e2e/_composition.py``).
"""

from __future__ import annotations

import ipaddress
import secrets
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from app.agent_api import deps
from app.agent_api.deps import CurrentNode
from app.domain.commands import CommandService
from app.domain.composition import (
    STUB_QUOTA_GRANT_BYTES,
    CompositionService,
    Credentials,
    NodeComposition,
    RowsRead,
    StoredRow,
    UserState,
    credentials_bound,
)
from app.domain.nodes import STUB_NODE_ID, NodeStatus, stub_node
from app.security.crypto import FieldCipher
from fastapi import Depends

from tests._pki import STUB_CLIENT_CERT, STUB_CLIENT_FINGERPRINT

IDENTITY = "stub-identity-token-000000000000000000000000000"
FINGERPRINT = STUB_CLIENT_FINGERPRINT
HEADERS = {
    "X-Agent-Version": "0.1.0",
    "X-Client-Cert": STUB_CLIENT_CERT,
    "X-Node-Identity": IDENTITY,
}


def node_as(
    status: NodeStatus = "active", node_id: uuid.UUID = STUB_NODE_ID
) -> Callable[..., Awaitable[CurrentNode]]:
    """Подмена ``current_node``: признаки разбирает настоящий код, нода — фиксированная карточка
    ``stub_node`` в статусе ``status``."""

    async def override(
        agent_version: Annotated[str, Depends(deps.supported_agent_version)],
        presented: Annotated[deps.Presented, Depends(deps.presented_identity)],
    ) -> CurrentNode:
        return CurrentNode(
            node=stub_node(status=status, node_id=node_id),
            fingerprint=presented.fingerprint,
            identity_token=presented.identity_token,
            agent_version=agent_version,
        )

    return override


# --- раскладка фикстур контракта ----------------------------------------------------------------

LAYOUT_SEQ = 4  # ``nodes.desired_users_seq`` раскладки: номер последнего изменения
USER_ACTIVE = uuid.UUID("00000000-0000-7000-8000-0000000000a1")
USER_SUSPENDED = uuid.UUID("00000000-0000-7000-8000-0000000000a2")
USER_REMOVED = uuid.UUID("00000000-0000-7000-8000-0000000000a3")
USER_LATEST = uuid.UUID("00000000-0000-7000-8000-0000000000a4")
BLOCKED_IP = ipaddress.IPv4Address("203.0.113.77")
# Пространство имён воспроизводимых credentials: в бою ключ и пароль случайны (§7.2), здесь они
# выводятся из пары «нода × пользователь», чтобы фикстуры контракта не менялись от прогона к
# прогону, а у одного пользователя на разных нодах были разными (§11.3).
CREDENTIALS_NS = uuid.UUID("00000000-0000-7000-8000-0000000000cd")
# Строки раскладки: доступ выдан; снят по исчерпанию квоты (с заблокированным адресом R-27);
# пара удалена; выдан ещё одному. Самое свежее изменение (4) доступ **не** снимает: в
# фильтрующем статусе курсор выборки (4) и наибольший номер выданных строк (3) расходятся, иначе
# правило «курсор идёт по выборке до фильтра» не отличалось бы от «курсор равен константе».
LAYOUT: tuple[tuple[uuid.UUID, UserState, int, tuple[str, ...]], ...] = (
    (USER_ACTIVE, "active", 1, ()),
    (USER_SUSPENDED, "suspended_quota", 2, (str(BLOCKED_IP),)),
    (USER_REMOVED, "removed", 3, ()),
    (USER_LATEST, "active", 4, ()),
)


def layout_credentials(node_id: uuid.UUID, user_id: uuid.UUID) -> Credentials:
    """Credentials пары раскладки: ключ и пароль выводятся из пары «нода × пользователь»."""
    derived = uuid.uuid5(CREDENTIALS_NS, f"{node_id}:{user_id}")
    return Credentials(
        vless_uuid=derived,
        trojan_password=f"stub-trojan-{derived.hex}",  # noqa: S106 — производное значение
        version=1,
    )


def throwaway_cipher() -> FieldCipher:
    """Шифр с одноразовым ключом — для герметичных прогонов без файла секрета."""
    return FieldCipher(secrets.token_bytes(32))


def layout_rows(node_id: uuid.UUID, cipher: FieldCipher) -> list[StoredRow]:
    """Строки раскладки в форме хранения: credentials зашифрованы ``cipher`` у пар, где доступ
    выдан (их создаёт публикация; у снятых пар запись тоже есть, но в ответ она не идёт)."""
    rows = []
    for user_id, state, seq, blocked in LAYOUT:
        keys = layout_credentials(node_id, user_id) if state != "removed" else None
        rows.append(
            StoredRow(
                user_id=user_id,
                state=state,
                quota_grant_bytes=STUB_QUOTA_GRANT_BYTES if state == "active" else 0,
                blocked_ips=[ipaddress.ip_address(ip) for ip in blocked],
                updated_seq=seq,
                vless_uuid_enc=(
                    None
                    if keys is None
                    else cipher.encrypt(
                        keys.vless_uuid.bytes,
                        bound=credentials_bound("vless_uuid_enc", user_id, node_id),
                    )
                ),
                trojan_password_enc=(
                    None
                    if keys is None
                    else cipher.encrypt(
                        keys.trojan_password.encode(),
                        bound=credentials_bound("trojan_password_enc", user_id, node_id),
                    )
                ),
                credentials_version=None if keys is None else keys.version,
            )
        )
    return rows


class RecordedComposition(CompositionService):
    """Служба состава на раскладке фикстур без базы: вентиль, курсор, фильтр и расшифровка —
    боевые, источник строк — ``layout_rows``. Признак ``resync_required`` запоминается в
    экземпляре, как его хранила бы колонка ноды."""

    def __init__(
        self, resync_required: bool = False, commands: CommandService | None = None
    ) -> None:
        cipher = throwaway_cipher()
        super().__init__(pool=None, commands=commands, cipher=cipher)
        self._layout_cipher = cipher
        self.resync_required = resync_required

    async def _read(self, node_id: uuid.UUID, users_seq: int, rows: RowsRead) -> NodeComposition:
        layout = layout_rows(node_id, self._layout_cipher)
        selected = {
            "snapshot": [row for row in layout if row.state != "removed"],
            "delta": [row for row in layout if row.updated_seq > users_seq],
            "none": [],
        }[rows]
        return NodeComposition(
            users_seq=LAYOUT_SEQ, resync_required=self.resync_required, rows=selected
        )

    async def _mark_resync(self, node_id: uuid.UUID) -> None:
        self.resync_required = True


def recorded_streams(
    resync_required: bool = False,
) -> Callable[..., Awaitable[CompositionService]]:
    """Подмена ``get_composition_service``: служба состава на раскладке фикстур с каналом команд
    из графа зависимостей раздела (как у боевой зависимости)."""

    async def override(
        commands: Annotated[CommandService, Depends(deps.get_command_service)],
    ) -> CompositionService:
        return RecordedComposition(resync_required=resync_required, commands=commands)

    return override
