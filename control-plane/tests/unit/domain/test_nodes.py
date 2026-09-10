"""``app.domain.nodes`` (001.24): свойства bootstrap-токена, которые заглушка обязана
обеспечивать по-настоящему (Н-24, §7.2), и согласованность фиксированных значений."""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from typing import cast
from unittest import mock

from app.api.admin.nodes import set_manual_status
from app.domain import nodes
from app.security.ca import InternalCA
from app.security.deps import CurrentAdmin
from app.security.sessions import Session


def test_bootstrap_token_comes_from_the_os_source_not_the_module_generator() -> None:
    """Токен даёт identity ноды, поэтому берётся из ``secrets`` (``os.urandom``), а не из
    ``random``: подмена источника делает выдачу предсказуемой — страж ловит и обход
    ``secrets.token_urlsafe``, и уменьшение числа байт (ревью 001.24; тот же класс, что C-1
    задачи 001.18 для кодов)."""
    with mock.patch.object(secrets, "token_urlsafe", return_value="предсказуемо") as source:
        assert nodes.generate_bootstrap_token() == "предсказуемо", "выдача идёт через secrets"
    assert source.call_args.args == (nodes.BOOTSTRAP_TOKEN_BYTES,), source.call_args
    assert nodes.BOOTSTRAP_TOKEN_BYTES * 8 == 256, "§7.2: 256 бит"
    # Генератор Мерсенна в выдаче не участвует: его getrandbits подменён на ошибку, боевая
    # выдача её не задевает, а random.choice / randbelow — задели бы.
    with mock.patch("random.Random.getrandbits", side_effect=AssertionError("mt19937")):
        issued = {nodes.generate_bootstrap_token() for _ in range(64)}
    assert len(issued) == 64, "боевая выдача не повторяется"
    assert all(len(token) == 43 for token in issued), "base64url от 32 байт — 43 символа"
    alphabet = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert set().union(*issued) <= alphabet


def test_stub_values_do_not_contradict_the_model() -> None:
    """Фиксированные значения заглушки не смешивают разные сущности: тарифицируемая группа
    (R-18, ровно одна) и группа доступа — разные таблицы §4.2.3, значит и разные идентификаторы;
    карточка и состояние описывают одну ноду с одним статусом."""
    assert nodes.STUB_ACCESS_GROUP_ID != nodes.STUB_BILLING_GROUP_ID
    assert nodes.STUB_NODE_IN.billing_group_id == nodes.STUB_BILLING_GROUP_ID
    assert nodes.STUB_NODE_IN.access_group_ids == [nodes.STUB_ACCESS_GROUP_ID]
    node, state = nodes.stub_node(), nodes.stub_state()
    assert node.status == state.status and node.resync_required == state.resync_required
    assert (node.agent_version, node.xray_version) == (state.agent_version, state.xray_version)
    pending = nodes.stub_node(status="pending")
    assert pending.agent_version is None and pending.last_heartbeat_at is None, (
        "до обмена токена агент ничего не предъявил"
    )


async def test_token_expiry_is_measured_from_the_moment_of_issue() -> None:
    """Н-24: срок жизни токена — не более 60 минут от выдачи."""
    assert nodes.BOOTSTRAP_TOKEN_TTL <= dt.timedelta(minutes=60)
    issued = dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC)
    service = nodes.NodeService(pool=None)
    token = await service.issue_bootstrap_token(nodes.STUB_NODE_ID, nodes.STUB_NODE_ID, now=issued)
    assert token.expires_at == issued + nodes.BOOTSTRAP_TOKEN_TTL
    assert token.node_id == nodes.STUB_NODE_ID


async def test_identity_fingerprint_belongs_to_the_issued_certificate() -> None:
    """Отпечаток identity считается от сертификата, который выдаёт ``enroll``: иначе сверка
    признаков (UC-01 шаг 6) сравнивала бы несравнимое. Отзыв проставляет ``revoked_at``."""
    service = nodes.NodeService(pool=None)
    issued = await service.enroll("token", nodes.STUB_CSR_PEM, "0.1.0", "26.9.1")
    assert issued.cert_fingerprint == InternalCA.fingerprint(issued.client_cert_pem)
    state = await service.state(nodes.STUB_NODE_ID)
    assert state.identity is not None
    assert state.identity.cert_fingerprint == issued.cert_fingerprint
    revoked = await service.revoke_identity(nodes.STUB_NODE_ID, nodes.STUB_NODE_ID)
    assert revoked.identity is not None and revoked.identity.revoked_at is not None


async def test_manual_status_reason_reaches_the_domain() -> None:
    """Причина ручного перевода объявлена контрактом (``ManualStatusIn.reason``) и обязана
    дойти до домена: в ``audit_log`` её пишет 001.48, но маршрут не вправе её выбрасывать."""
    seen: dict[str, object] = {}

    class RecordingNodes(nodes.NodeService):
        async def set_manual_status(
            self,
            node_id: uuid.UUID,
            status: nodes.ManualStatus | None,
            admin_id: uuid.UUID,
            reason: str | None = None,
        ) -> nodes.Node:
            seen.update(node_id=node_id, status=status, admin_id=admin_id, reason=reason)
            return nodes.stub_node(status=status or "active", node_id=node_id)

    node_id, admin_id = uuid.uuid4(), uuid.uuid4()
    admin = CurrentAdmin(id=admin_id, session=cast(Session, None))
    body = nodes.ManualStatusIn(status="maintenance", reason="замена диска")
    answer = await set_manual_status(node_id, body, admin, RecordingNodes(pool=None))
    assert seen == {
        "node_id": node_id,
        "status": "maintenance",
        "admin_id": admin_id,
        "reason": "замена диска",
    }, seen
    assert answer.id == node_id and answer.status == "maintenance"
