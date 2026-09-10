"""Таблицы статусов ноды (постановка §4.6) как код: матрица переходов и вентиль потоков.

Обе таблицы — единственный источник правил для выдачи состояния (001.29) и автоматики статусов
(001.30), поэтому сверяются с текстом постановки построчно и **равенством**: проверка вхождения
пропустила бы лишний разрешённый переход или лишнюю открытую строку вентиля.
"""

from __future__ import annotations

from typing import get_args

import pytest
from app.domain.nodes import NodeStatus
from app.domain.statuses import AUTOMATIC, MANUAL, STREAM_GATE, TRANSITIONS, StatusService

ALL_STATUSES: list[NodeStatus] = [
    "pending",
    "provisioning",
    "active",
    "degraded",
    "offline",
    "maintenance",
    "disabled",
    "suspended",
]


def test_the_status_list_here_is_the_type_itself() -> None:
    """Список выше — копия перечисления, и копия обязана сверяться с источником: иначе
    девятый статус, добавленный в ``NodeStatus``, не попал бы ни в одну таблицу, а
    ``STREAM_GATE[node.status]`` дал бы KeyError и 500 на живой ноде."""
    assert set(ALL_STATUSES) == set(get_args(NodeStatus))
    assert len(ALL_STATUSES) == len(get_args(NodeStatus)), "без повторов"


def test_both_tables_cover_every_status_and_nothing_else() -> None:
    """Перечисление `node_status` §4.2.3 покрыто целиком обеими таблицами."""
    assert set(TRANSITIONS) == set(ALL_STATUSES)
    assert set(STREAM_GATE) == set(ALL_STATUSES)
    assert MANUAL | AUTOMATIC | {"pending"} == set(ALL_STATUSES)
    assert not MANUAL & AUTOMATIC, "статус либо ручной, либо автоматический"
    assert "pending" not in MANUAL | AUTOMATIC, "§4.6 не относит pending ни к тем, ни к другим"
    for source, targets in TRANSITIONS.items():
        assert source not in targets, f"переход в тот же статус — не переход: {source}"


def test_transition_matrix_matches_the_source_row_by_row() -> None:
    """Матрица переходов §4.6 целиком, равенством: строка «любой автоматический → ручной»
    разложена по автоматическим статусам, строка «любой → удаление» не входит (вывод из
    эксплуатации — не статус, а ``nodes.decommissioned_at``).

    Два чтения записаны здесь явно, потому что таблица их прямо не называет, и 001.30
    подтверждает или меняет их вместе с автоматикой: из `pending` ручной статус не ставится
    (строка «любой автоматический» его не покрывает — `pending` не отнесён ни к автоматическим,
    ни к ручным), и ручной статус не переводится в другой ручной без снятия."""
    expected: dict[NodeStatus, set[NodeStatus]] = {
        "pending": {"provisioning"},
        "provisioning": {"active", "offline", "maintenance", "disabled", "suspended"},
        "active": {"degraded", "offline", "maintenance", "disabled", "suspended"},
        "degraded": {"active", "offline", "maintenance", "disabled", "suspended"},
        "offline": {"active", "maintenance", "disabled", "suspended"},
        "maintenance": {"active", "offline"},
        "disabled": {"active", "offline"},
        "suspended": {"active", "offline"},
    }
    assert {source: set(targets) for source, targets in TRANSITIONS.items()} == expected


@pytest.mark.parametrize(
    ("source", "target", "allowed"),
    [
        ("pending", "provisioning", True),  # подтверждение администратором
        ("pending", "active", False),  # минуя подтверждение — нет
        ("pending", "maintenance", False),  # «любой автоматический» pending не покрывает
        ("provisioning", "active", True),
        ("provisioning", "offline", True),  # нет heartbeat дольше Н-15
        ("provisioning", "pending", False),  # подтверждение администратора не сбрасывается
        ("active", "degraded", True),
        ("degraded", "active", True),
        ("active", "provisioning", False),  # работающая нода не возвращается к настройке
        ("offline", "active", True),
        ("offline", "degraded", False),
        ("offline", "pending", False),
        ("active", "suspended", True),  # любой автоматический → ручной
        ("maintenance", "active", True),  # снятие ручного статуса
        ("maintenance", "disabled", False),  # ручной в ручной — только через снятие
        ("suspended", "degraded", False),  # снятие ведёт в active или offline
    ],
)
def test_can_transition_reads_the_matrix(
    source: NodeStatus, target: NodeStatus, allowed: bool
) -> None:
    """Отдельные пары — на случай, если таблицу перепишут: значение важнее формы записи."""
    assert StatusService.can_transition(source, target) is allowed


def test_manual_statuses_are_only_left_by_hand() -> None:
    """Ручной статус имеет приоритет: автоматика из него не выводит, поэтому из `maintenance`,
    `disabled` и `suspended` ведут ровно два перехода — снятие в `active` или `offline`."""
    for status in sorted(MANUAL):
        assert TRANSITIONS[status] == {"active", "offline"}, status
    for status in sorted(AUTOMATIC):
        assert MANUAL <= TRANSITIONS[status], status


def test_stream_gate_matches_the_influence_matrix() -> None:
    """Матрица влияния §4.6, столбцы «Поток состава пользователей» и «Канал команд», плюс
    столбец «Поток конфигурации» таблицы вентиля interfaces.md §5.2."""
    expected: dict[NodeStatus, tuple[bool, bool, str, bool]] = {
        "pending": (True, False, "none", False),
        "provisioning": (False, True, "none", True),
        "active": (False, True, "all", True),
        "degraded": (False, True, "all", True),
        "offline": (False, True, "all", True),
        "maintenance": (False, True, "all", True),
        "disabled": (False, True, "revocations", False),
        "suspended": (False, True, "revocations", False),
    }
    assert {
        status: (gate.refused, gate.config, gate.composition, gate.commands)
        for status, gate in STREAM_GATE.items()
    } == expected


def test_the_answer_is_filtered_exactly_where_the_gate_hides_rows() -> None:
    """`filters_composition` — правило «когда взводить ``nodes.resync_required``» (§5.2): часть
    строк скрыта, а курсор всё равно продвинулся. Только `disabled` и `suspended`: для
    `provisioning` состав пуст, но и курсор не двигается — добирать нечего."""
    filtered = {status for status, gate in STREAM_GATE.items() if gate.filters_composition}
    assert filtered == {"disabled", "suspended"}
    assert not any(gate.filters_composition for gate in STREAM_GATE.values() if gate.refused)
