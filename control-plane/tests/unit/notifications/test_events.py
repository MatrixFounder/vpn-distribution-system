"""Задача 001.51: типы событий §4.17, ключ подавления повторов ``dedup_key``, шаблоны писем,
заглушки отправителей и боевые типы задач доставки.

TC-UNIT-01 — формулы ключа по типам: детерминированная строка; разные переходы статуса ноды,
разные отзывы доступа в одном периоде и разные разрывы непрерывности — разные ключи. TC-UNIT-02 —
файл шаблона есть у каждого пользовательского события на обоих языках, разбирается Jinja2 и нет
у операторских. TC-UNIT-03 — недостающая, лишняя и негодная часть отвергаются, один момент в
разных часовых поясах и один UUID в разной записи дают один ключ, граница версии — 0.
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import inspect
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import jinja2
import pytest
from app.jobs.handlers import HANDLERS
from app.jobs.handlers.notifications import notify_email, notify_webhook
from app.notifications import events
from app.notifications.email import LANGUAGES, TEMPLATES, EmailSender
from app.notifications.events import (
    CONTINUITY_PARTS,
    OPERATOR_EVENTS,
    PARTS,
    USER_EVENTS,
    EventType,
    dedup_key,
)
from app.notifications.webhook import WebhookSender

from tests.e2e._spec import EXPECTED_ENUMS

USER = uuid.UUID("01890000-0000-7000-8000-00000000000a")
NODE = uuid.UUID("01890000-0000-7000-8000-00000000000b")
PERIOD = uuid.UUID("01890000-0000-7000-8000-00000000000c")
AT = dt.datetime(2026, 11, 26, 9, 30, 15, 120000, tzinfo=dt.UTC)
LATER = AT + dt.timedelta(minutes=7)
MSK = dt.timezone(dt.timedelta(hours=3))
DAY = dt.date(2026, 11, 25)

# Части каждого типа: литералы, а не вывод из PARTS — тест сверяет таблицу с формулами задачи.
SAMPLE: dict[EventType, dict[str, Any]] = {
    EventType.SUBSCRIPTION_ACTIVATED: {"user_id": USER, "period_id": PERIOD, "period_end": AT},
    EventType.SUBSCRIPTION_EXPIRING: {"user_id": USER, "period_id": PERIOD, "period_end": AT},
    EventType.SUBSCRIPTION_EXPIRED: {"user_id": USER, "period_id": PERIOD, "period_end": AT},
    EventType.TRAFFIC_80: {"user_id": USER, "period_id": PERIOD},
    EventType.TRAFFIC_95: {"user_id": USER, "period_id": PERIOD},
    EventType.TRAFFIC_EXHAUSTED: {"user_id": USER, "period_id": PERIOD, "exhausted_at": AT},
    EventType.NODE_ADDRESS_CHANGED: {
        "user_id": USER,
        "node_id": NODE,
        "kind": "address",
        "version": 7,
    },
    EventType.NODE_OFFLINE: {"node_id": NODE, "status_changed_at": AT},
    EventType.NODE_RECOVERED: {"node_id": NODE, "status_changed_at": AT},
    EventType.NODE_SUSPENDED_BY_PROVIDER: {"node_id": NODE, "status_changed_at": AT},
    EventType.RECONCILIATION_MISMATCH: {"node_id": NODE, "kind": "cross_source", "day": DAY},
    EventType.NODE_REPORT_BUFFER_FULL: {"node_id": NODE, "period_end": AT},
}
STAMP = "2026-11-26T09:30:15.120000+00:00"
EXPECTED_KEYS: dict[EventType, str] = {
    EventType.SUBSCRIPTION_ACTIVATED: f"subscription_activated:{USER}:{PERIOD}:{STAMP}",
    EventType.SUBSCRIPTION_EXPIRING: f"subscription_expiring:{USER}:{PERIOD}:{STAMP}",
    EventType.SUBSCRIPTION_EXPIRED: f"subscription_expired:{USER}:{PERIOD}:{STAMP}",
    EventType.TRAFFIC_80: f"traffic_80:{USER}:{PERIOD}",
    EventType.TRAFFIC_95: f"traffic_95:{USER}:{PERIOD}",
    EventType.TRAFFIC_EXHAUSTED: f"traffic_exhausted:{USER}:{PERIOD}:{STAMP}",
    EventType.NODE_ADDRESS_CHANGED: f"node_address_changed:{USER}:{NODE}:address:7",
    EventType.NODE_OFFLINE: f"node_offline:{NODE}:{STAMP}",
    EventType.NODE_RECOVERED: f"node_recovered:{NODE}:{STAMP}",
    EventType.NODE_SUSPENDED_BY_PROVIDER: f"node_suspended_by_provider:{NODE}:{STAMP}",
    EventType.RECONCILIATION_MISMATCH: f"reconciliation_mismatch:{NODE}:cross_source:2026-11-25",
    EventType.NODE_REPORT_BUFFER_FULL: f"node_report_buffer_full:{NODE}:{STAMP}",
}


def test_twelve_event_types_follow_the_notifications_table() -> None:
    """12 типов §4.17 в порядке таблицы и меток ``event_type`` (миграция 0001); семь
    пользовательских, пять операторских."""
    assert [e.value for e in EventType] == EXPECTED_ENUMS["event_type"]
    assert len(EventType) == 12
    assert {e.value for e in USER_EVENTS} == {
        "subscription_activated",
        "subscription_expiring",
        "subscription_expired",
        "traffic_80",
        "traffic_95",
        "traffic_exhausted",
        "node_address_changed",
    }
    assert OPERATOR_EVENTS == frozenset(EventType) - USER_EVENTS
    assert set(PARTS) == set(EventType)


def test_the_production_job_types_reach_their_handlers() -> None:
    """Боевые типы задач ``emit`` — на копии модуля без подмены фикстуры ``conftest``: опечатка
    в ``EMAIL_JOB`` или ``WEBHOOK_JOB`` оставила бы задачи доставки стоять в ``pending`` —
    исполнитель выбирает только типы реестра."""
    spec = importlib.util.spec_from_file_location("_events_unpatched", events.__file__)
    assert spec is not None and spec.loader is not None
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert (fresh.EMAIL_JOB, fresh.WEBHOOK_JOB) == ("notify.email", "notify.webhook")
    assert HANDLERS[fresh.EMAIL_JOB] is notify_email
    assert HANDLERS[fresh.WEBHOOK_JOB] is notify_webhook


@pytest.mark.parametrize("event", list(EventType), ids=[e.value for e in EventType])
def test_tc_unit_01_the_key_of_each_type(event: EventType) -> None:
    """TC-UNIT-01: формула ключа по типу — детерминированная строка; тип строкой — тот же ключ."""
    assert dedup_key(event, **SAMPLE[event]) == EXPECTED_KEYS[event]
    assert dedup_key(event.value, **SAMPLE[event]) == EXPECTED_KEYS[event]


def test_tc_unit_01_episodes_and_revocations_get_their_own_keys() -> None:
    """TC-UNIT-01: два перехода статуса ноды — два ключа; два отзыва доступа в одном периоде —
    два ключа (В-7); порог 80 % в периоде — один ключ; смена адреса и смена ключевых пар с тем
    же числом версии — разные ключи (вид в ключе)."""
    offline = [
        dedup_key(EventType.NODE_OFFLINE, node_id=NODE, status_changed_at=at) for at in (AT, LATER)
    ]
    assert offline[0] != offline[1]
    exhausted = [
        dedup_key(EventType.TRAFFIC_EXHAUSTED, user_id=USER, period_id=PERIOD, exhausted_at=at)
        for at in (AT, LATER)
    ]
    assert exhausted[0] != exhausted[1]
    assert dedup_key(EventType.TRAFFIC_80, user_id=USER, period_id=PERIOD) == dedup_key(
        "traffic_80", period_id=PERIOD, user_id=USER
    )
    changes = {
        dedup_key(EventType.NODE_ADDRESS_CHANGED, user_id=USER, node_id=NODE, kind=k, version=7)
        for k in ("address", "params")
    }
    assert len(changes) == 2


def test_tc_unit_01_an_extension_re_arms_the_subscription_events() -> None:
    """Продление или смена тарифа, сдвинувшие конец того же периода, — новое «истекает» перед
    новым концом и новое «активирована» (решение заказчика 2026-11-26): конец периода — эпизод
    ключа событий подписки; тот же конец — тот же ключ."""
    for event in (
        EventType.SUBSCRIPTION_ACTIVATED,
        EventType.SUBSCRIPTION_EXPIRING,
        EventType.SUBSCRIPTION_EXPIRED,
    ):
        keys = [
            dedup_key(event, user_id=USER, period_id=PERIOD, period_end=end)
            for end in (AT, AT + dt.timedelta(days=60), AT)
        ]
        assert keys[0] != keys[1] and keys[0] == keys[2], event


def test_tc_unit_01_each_continuity_gap_is_its_own_event() -> None:
    """Сверка непрерывности (решение заказчика 2026-11-26): эпизод — начало разрыва, а не
    сутки; два разрыва в одни сутки — два ключа; один разрыв — один ключ при повторном проходе.
    Арифметическая и межисточниковая сверки — сутки и вид."""
    assert CONTINUITY_PARTS == ("node_id", "kind", "gap_start")
    gaps = [
        dedup_key(EventType.RECONCILIATION_MISMATCH, node_id=NODE, kind="continuity", gap_start=at)
        for at in (AT, LATER, AT)
    ]
    assert gaps[0] == f"reconciliation_mismatch:{NODE}:continuity:{STAMP}"
    assert gaps[0] != gaps[1] and gaps[0] == gaps[2]
    cross = dedup_key(EventType.RECONCILIATION_MISMATCH, node_id=NODE, kind="cross_source", day=DAY)
    assert cross == f"reconciliation_mismatch:{NODE}:cross_source:2026-11-25"


def test_tc_unit_01_the_arithmetic_check_has_no_node() -> None:
    """Арифметическая сверка — по всей системе за сутки (001.33 ``arithmetic(conn, day)``;
    решение заказчика 2026-11-26): ключ без ноды; нода у неё — лишняя часть."""
    key = dedup_key(EventType.RECONCILIATION_MISMATCH, kind="arithmetic", day=DAY)
    assert key == "reconciliation_mismatch:arithmetic:2026-11-25"
    with pytest.raises(ValueError, match="лишние"):
        dedup_key(EventType.RECONCILIATION_MISMATCH, node_id=NODE, kind="arithmetic", day=DAY)
    with pytest.raises(ValueError, match="нет"):
        dedup_key(EventType.RECONCILIATION_MISMATCH, kind="cross_source", day=DAY)


def test_tc_unit_03_one_moment_or_id_in_any_form_is_one_key() -> None:
    """Момент — UTC с микросекундами: 12:30:15.12 по Москве и 09:30:15.12 UTC — один ключ;
    UUID строкой в любом регистре — тот же ключ, что объектом."""
    moscow = AT.astimezone(MSK)
    assert (
        dedup_key(EventType.NODE_OFFLINE, node_id=NODE, status_changed_at=moscow)
        == (EXPECTED_KEYS[EventType.NODE_OFFLINE])
    )
    whole = AT.replace(microsecond=0)
    assert dedup_key(EventType.NODE_OFFLINE, node_id=NODE, status_changed_at=whole).endswith(
        "T09:30:15.000000+00:00"
    )
    as_text = dedup_key(EventType.TRAFFIC_80, user_id=str(USER).upper(), period_id=str(PERIOD))
    assert as_text == EXPECTED_KEYS[EventType.TRAFFIC_80]


def test_tc_unit_03_version_zero_is_a_version() -> None:
    """Граница версии — 0: «целое не меньше 0»."""
    parts = {**SAMPLE[EventType.NODE_ADDRESS_CHANGED], "version": 0}
    assert dedup_key(EventType.NODE_ADDRESS_CHANGED, **parts).endswith(":address:0")


ADDRESS = SAMPLE[EventType.NODE_ADDRESS_CHANGED]
RECONCILE = SAMPLE[EventType.RECONCILIATION_MISMATCH]


@pytest.mark.parametrize(
    ("event", "parts"),
    [
        (EventType.TRAFFIC_80, {"user_id": USER}),  # нет периода
        (EventType.TRAFFIC_EXHAUSTED, {"user_id": USER, "period_id": PERIOD}),  # нет момента
        (EventType.TRAFFIC_80, {"user_id": USER, "period_id": PERIOD, "node_id": NODE}),
        (EventType.NODE_OFFLINE, {"node_id": NODE, "status_changed_at": None}),
        (EventType.NODE_OFFLINE, {"node_id": NODE, "status_changed_at": AT.replace(tzinfo=None)}),
        (EventType.NODE_OFFLINE, {"node_id": "не-uuid", "status_changed_at": AT}),
        (EventType.NODE_ADDRESS_CHANGED, {**ADDRESS, "kind": "ip"}),
        (EventType.NODE_ADDRESS_CHANGED, {**ADDRESS, "kind": "continuity"}),  # вид другого типа
        (EventType.NODE_ADDRESS_CHANGED, {**ADDRESS, "version": -1}),
        (EventType.NODE_ADDRESS_CHANGED, {**ADDRESS, "version": True}),
        (EventType.RECONCILIATION_MISMATCH, {**RECONCILE, "kind": "daily"}),
        (EventType.RECONCILIATION_MISMATCH, {**RECONCILE, "kind": "params"}),
        (EventType.RECONCILIATION_MISMATCH, {**RECONCILE, "day": AT}),  # момент вместо дня
        (
            EventType.RECONCILIATION_MISMATCH,
            {**RECONCILE, "kind": "continuity"},
        ),  # сутки, не разрыв
        (EventType.RECONCILIATION_MISMATCH, {**RECONCILE, "gap_start": AT}),  # разрыв у суточной
    ],
    ids=[
        "missing-period",
        "missing-moment",
        "extra-node",
        "none-moment",
        "naive-moment",
        "bad-uuid",
        "bad-kind",
        "foreign-kind",
        "negative-version",
        "bool-version",
        "bad-check",
        "foreign-check",
        "datetime-day",
        "continuity-by-day",
        "daily-with-gap",
    ],
)
def test_tc_unit_03_bad_parts_are_refused(event: EventType, parts: dict[str, Any]) -> None:
    """Недостающая, лишняя или негодная часть — ``ValueError``: иначе ключ молча совпал бы с
    ключом другого события или разошёлся бы с ключом того же."""
    with pytest.raises(ValueError, match=event.value):
        dedup_key(event, **parts)


def test_an_unknown_type_is_refused() -> None:
    with pytest.raises(ValueError):
        dedup_key("traffic_100", user_id=USER, period_id=PERIOD)


def test_tc_unit_02_templates_of_user_events_in_both_languages() -> None:
    """TC-UNIT-02: шаблон ``{lang}/{type}.txt`` есть у каждого пользовательского события на RU и
    EN, разбирается Jinja2 и называет владельца текста (001.78); у операторских шаблонов нет
    (почты у них нет, §4.17); других файлов в каталоге нет."""
    assert LANGUAGES == ("ru", "en")
    found = {p.relative_to(TEMPLATES).as_posix() for p in TEMPLATES.rglob("*") if p.is_file()}
    expected = {f"{lang}/{event.value}.txt" for lang in LANGUAGES for event in USER_EVENTS}
    assert found == expected
    environment = jinja2.Environment(autoescape=False)  # noqa: S701 — текст письма, не HTML
    for path in sorted(expected):
        source = (TEMPLATES / path).read_text(encoding="utf-8")
        environment.parse(source)
        assert "001.78" in source, path


def test_the_stub_interfaces_are_the_contract() -> None:
    """Интерфейс — то, что сдаёт задача-заглушка: сопрограммы ``EmailSender.send`` и
    ``WebhookSender.send`` с параметрами и возвратом контракта (001.78 и 001.79 реализуют именно
    их)."""
    assert inspect.iscoroutinefunction(EmailSender.send)
    assert inspect.iscoroutinefunction(WebhookSender.send)
    assert str(inspect.signature(EmailSender.send)) == (
        "(self, to: 'str', template: 'str', lang: 'str', ctx: 'Mapping[str, Any]') -> 'DeliveryId'"
    )
    assert str(inspect.signature(WebhookSender.send)) == (
        "(self, url: 'str', event: 'Mapping[str, Any]') -> 'None'"
    )


def test_events_never_import_the_handler_package() -> None:
    """Направление импорта: ``app.jobs.handlers`` может импортировать ``app.notifications``, а
    ``app.notifications.events`` пакет обработчиков — никогда: пакет грузит все обработчики, а
    обработчики-источники (001.83, 001.35, 001.30, 001.37d) импортируют ``emit`` — обратное
    ребро дало бы цикл. Проверка — в чистом интерпретаторе: любая форма импорта (``from … import
    …``, ``import …``, внутри функции при импорте) видна по ``sys.modules``. Типы задач — в
    листовом ``delivery.py`` без импортов проекта."""
    probe = (
        "import sys, app.notifications.events; "
        "print(sorted(m for m in sys.modules if m.startswith('app.jobs.handlers')))"
    )
    root = Path(events.__file__).resolve().parents[2]
    # Аргументы — фиксированный список без оболочки.
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe], cwd=root, capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "[]", done.stdout
    delivery = ast.parse((root / "app/notifications/delivery.py").read_text(encoding="utf-8"))
    imported = [
        node
        for node in ast.walk(delivery)
        if isinstance(node, ast.Import)
        or (isinstance(node, ast.ImportFrom) and node.module != "__future__")
    ]
    assert imported == []


async def test_the_sender_stubs_refuse() -> None:
    """Заглушки отправителей отказывают: «успешная» отправка, которой не было, засчиталась бы
    за доставленное уведомление (почта — 001.78, webhook — 001.79)."""
    with pytest.raises(NotImplementedError, match="001.78"):
        await EmailSender().send("user@example.test", "traffic_80", "ru", {})
    with pytest.raises(NotImplementedError, match="001.79"):
        await WebhookSender().send("https://hooks.example.test", {"event_id": str(USER)})
