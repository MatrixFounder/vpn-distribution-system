"""Сигнатуры и фиксированные значения модулей учёта (задача 001.33, критерий «сигнатуры всех
модулей учёта объявлены; каждая возвращает фиксированное значение»). Сигнатуры закреплены
текстом: логические задачи этапа 5 (001.23, 001.34…001.39) изменяют эти файлы и рассчитывают
на объявленные имена и параметры. Пул и подключение — часовые: заглушка, обратившаяся к базе,
валит тест."""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.accounting import aggregate, reconcile, signals
from app.accounting.device_limit import DeviceLimitService
from app.accounting.limits import LimitsService
from app.accounting.quota import (
    RETRY_AFTER_GRANT,
    STUB_ISSUED_SEQ,
    QuotaGrant,
    QuotaRequestIn,
    QuotaService,
)
from app.accounting.service import (
    RETRY_AFTER_NODE_MISMATCH,
    Accept,
    AccountingService,
    ReportIn,
)
from app.domain import multiplier
from app.domain.composition import STUB_QUOTA_GRANT_BYTES
from app.domain.nodes import STUB_BILLING_GROUP_ID, STUB_NODE_ID, stub_node
from app.domain.statuses import STREAM_GATE
from app.errors import ApiError
from pydantic import ValidationError

NODE = uuid.UUID("00000000-0000-7000-8000-0000000000b9")
USER = uuid.UUID("00000000-0000-7000-8000-0000000000a1")
DAY = dt.date(2026, 9, 10)
AT = dt.datetime(2026, 9, 10, 12, tzinfo=dt.UTC)


class Sentinel:
    """Пул или подключение, которого нет: любое обращение — провал."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"заглушка обратилась к базе: {name}")


def report(**overrides: Any) -> ReportIn:
    return ReportIn.model_validate(
        {
            "counter_epoch": "00000000-0000-7000-8000-0000000000e1",
            "report_seq": 5,
            "parts_total": 1,
            "period_start": "2026-09-10T12:00:00Z",
            "period_end": "2026-09-10T12:01:00Z",
            "lines": [],
            "online_ips": [],
            "node_rx_bytes": 0,
            "node_tx_bytes": 0,
            **overrides,
        }
    )


@pytest.mark.parametrize(
    ("function", "signature"),
    [
        (
            AccountingService.accept_report,
            "(self, node: 'Node', report: 'ReportIn') -> 'Accept'",
        ),
        (
            AccountingService.close_hour,
            "(self, conn: 'asyncpg.Connection', node_id: 'uuid.UUID', at: 'dt.datetime') -> 'None'",
        ),
        (
            QuotaService.grant,
            "(self, node: 'Node', user_id: 'uuid.UUID', consumed_bytes: 'int') -> 'QuotaGrant'",
        ),
        (
            QuotaService.on_report,
            "(self, conn: 'asyncpg.Connection', node_id: 'uuid.UUID', "
            "billable: 'Mapping[uuid.UUID, int]') -> 'None'",
        ),
        (
            LimitsService.check,
            "(self, conn: 'asyncpg.Connection', user_ids: 'Collection[uuid.UUID]') -> 'None'",
        ),
        (
            multiplier.resolve,
            "(conn: 'asyncpg.Connection', node_id: 'uuid.UUID', at: 'dt.datetime') -> 'Resolved'",
        ),
        (multiplier.billable, "(raw: 'int', milli: 'int') -> 'int'"),
        (
            reconcile.arithmetic,
            "(conn: 'asyncpg.Connection', day: 'dt.date') -> 'ReconciliationRun'",
        ),
        (
            reconcile.cross_source,
            "(conn: 'asyncpg.Connection', node_id: 'uuid.UUID', day: 'dt.date', "
            "tolerance_pct: 'Decimal') -> 'ReconciliationRun'",
        ),
        (
            reconcile.continuity,
            "(conn: 'asyncpg.Connection', node_id: 'uuid.UUID') -> 'list[TrafficGap]'",
        ),
        (aggregate.aggregate_day, "(conn: 'asyncpg.Connection', day: 'dt.date') -> 'int'"),
        (
            DeviceLimitService.evaluate,
            "(self, conn: 'asyncpg.Connection', node_id: 'uuid.UUID', "
            "user_ids: 'Collection[uuid.UUID]') -> 'Blocked'",
        ),
        (
            signals.resale_signals,
            "(conn: 'asyncpg.Connection', user_id: 'uuid.UUID', days: 'int' = 7) -> 'Signals'",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else value.__qualname__,
)
def test_the_declared_signature_is_the_one_logic_tasks_will_find(
    function: Any, signature: str
) -> None:
    assert str(inspect.signature(function)) == signature


async def test_accounting_stub_accepts_and_checks_the_node() -> None:
    """Приём без состояния (последний принятый — только что принятая часть, не дубликат — так
    фикстура продолжения интервала записывает верный ответ), и одно настоящее правило:
    идентификатор другой ноды в теле — 403 `node_mismatch` с `Retry-After`; свой или
    отсутствующий — приём. Нода намеренно не фиксированная: сверяется запрошенная, а не
    константа."""
    service = AccountingService(Sentinel())
    node = stub_node(node_id=NODE)
    for seq in (1, 5, 2**63 - 1):
        assert await service.accept_report(node, report(report_seq=seq)) == Accept(
            last_accepted_seq=seq, duplicate=False
        )
    own = await service.accept_report(node, report(node_id=str(NODE)))
    assert own.duplicate is False
    with pytest.raises(ApiError) as refused:
        await service.accept_report(node, report(node_id=str(STUB_NODE_ID)))
    assert (refused.value.status, refused.value.code) == (403, "node_mismatch")
    assert refused.value.details == {"node_id": str(NODE)}
    assert RETRY_AFTER_NODE_MISMATCH == 3600
    assert refused.value.headers == {"Retry-After": "3600"}
    await service.close_hour(Sentinel(), NODE, AT)  # заглушка: без исключений и без базы


def test_accounting_keeps_the_collaborators_it_is_given() -> None:
    """Службы, которые приём вызовет в своей транзакции (001.77), приходят из графа
    зависимостей и хранятся как есть; без них создаются свои — над тем же пулом."""
    quotas, limits = QuotaService(Sentinel()), DeviceLimitService(Sentinel())
    injected = AccountingService(Sentinel(), quotas=quotas, device_limits=limits)
    assert injected._quotas is quotas and injected._device_limits is limits  # noqa: SLF001
    own = AccountingService(Sentinel())
    assert isinstance(own._quotas, QuotaService)  # noqa: SLF001
    assert isinstance(own._device_limits, DeviceLimitService)  # noqa: SLF001


async def test_quota_and_limits_stubs() -> None:
    """Фиксированный грант — ноде с полным потоком состава; ноде, которой вентиль §4.6 даёт
    только отзывы или ничего, — 403 `grant_unavailable` с `Retry-After`. Правило — по таблице
    вентиля, а не по перечню статусов в тесте."""
    quotas = QuotaService(Sentinel())
    assert STUB_QUOTA_GRANT_BYTES == 1073741824 and STUB_ISSUED_SEQ == 1
    assert RETRY_AFTER_GRANT == 300
    refused = []
    for status, gate in sorted(STREAM_GATE.items()):
        node = stub_node(status=status, node_id=NODE)
        if gate.composition == "all":
            assert await quotas.grant(node, USER, 0) == QuotaGrant(
                quota_grant_bytes=1073741824, issued_seq=1
            ), status
            continue
        with pytest.raises(ApiError) as failure:
            await quotas.grant(node, USER, 0)
        assert (failure.value.status, failure.value.code) == (403, "grant_unavailable"), status
        assert failure.value.details == {"node_status": status}
        assert failure.value.headers == {"Retry-After": "300"}
        refused.append(status)
    assert refused == ["disabled", "pending", "provisioning", "suspended"]
    await quotas.on_report(Sentinel(), NODE, {USER: 4096})  # заглушки: без исключений и без базы
    await LimitsService(Sentinel()).check(Sentinel(), [USER])


async def test_multiplier_stub_is_the_default_level() -> None:
    """Заглушка разрешения — 1.0 из уровня `default` (§4.9); `billable` заглушки читает только
    `raw` — коэффициент вводит 001.23 вместе с таблицей случаев."""
    assert (
        multiplier.DEFAULT_MULTIPLIER_MILLI,
        multiplier.MULTIPLIER_MILLI_MAX,
        multiplier.MULTIPLIER_STEP_MILLI,
    ) == (1000, 10000, 100)
    resolved = await multiplier.resolve(Sentinel(), NODE, AT)
    assert resolved == multiplier.Resolved(
        multiplier_milli=1000, billing_group_id=STUB_BILLING_GROUP_ID, source="default"
    )
    assert multiplier.billable(1_000_000_001, 1000) == 1_000_000_001
    assert multiplier.billable(5, 3000) == 5, "заглушка: коэффициент не применяется (001.23)"
    for bad in (-100, 10100, 1050):  # вне диапазона, вне диапазона, не кратно шагу
        with pytest.raises(ValueError, match="коэффициент"):
            multiplier.Resolved(bad, STUB_BILLING_GROUP_ID, "group")
    assert multiplier.Resolved(0, STUB_BILLING_GROUP_ID, "node").multiplier_milli == 0
    assert multiplier.Resolved(10000, STUB_BILLING_GROUP_ID, "node").multiplier_milli == 10000


async def test_reconciliation_aggregation_and_observation_stubs() -> None:
    conn = Sentinel()
    run = await reconcile.arithmetic(conn, DAY)
    assert (run.kind, run.scope) == ("arithmetic", {"day": "2026-09-10"})
    assert (run.expected, run.actual, run.delta_pct, run.status) == (0, 0, Decimal("0.000"), "ok")
    cross = await reconcile.cross_source(conn, NODE, DAY, Decimal("5.0"))
    assert cross.kind == "cross_source"
    assert cross.scope == {"node_id": str(NODE), "day": "2026-09-10"}
    assert await reconcile.continuity(conn, NODE) == []
    with pytest.raises(TypeError):
        run.scope["day"] = "x"  # type: ignore[index]  # отображение неизменяемое, как и запись
    assert await aggregate.aggregate_day(conn, DAY) == 0
    assert await DeviceLimitService(Sentinel()).evaluate(conn, NODE, [USER]) == {}
    observed = await signals.resale_signals(conn, USER)
    assert observed == signals.STUB_SIGNALS
    assert 0 <= observed.active_hours_share <= 1
    with pytest.raises(ValidationError):
        observed.volume_gb = 1.0  # общий экземпляр заглушки неизменяем
    with pytest.raises(ValidationError):
        signals.Signals(volume_gb=1.0, new_conn_rate=1.0, active_hours_share=1.5)
    with pytest.raises(ValidationError):
        signals.Signals(volume_gb=-1.0, new_conn_rate=1.0, active_hours_share=0.5)
    with pytest.raises(ValidationError):
        signals.Signals(volume_gb=1.0, new_conn_rate=-1.0, active_hours_share=0.5)


def _is_type_checking(test: ast.expr) -> bool:
    """Ровно `TYPE_CHECKING` или `typing.TYPE_CHECKING`: атрибут с тем же именем у другого
    объекта (`settings.TYPE_CHECKING`) исполняется и не выводит тело ветки из-под правила."""
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute)
        and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name)
        and test.value.id == "typing"
    )


def _dotted(node: ast.expr) -> str | None:
    """Имя цепочки атрибутов `a.b.c` от имени `a`; иное начало (вызов, индекс) — не имя."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _runtime_imports(source: str, package: str) -> list[str]:
    """Абсолютные имена, которых модуль достигает на исполнении: `import a.b`, `from a import
    b`, относительные — разрешённые от пакета файла (`from .. import accounting` в `app.domain`
    — это `app.accounting`) — и обращения по атрибуту `app.accounting.x` (после `import app`
    пакет учёта уже загружен соседями, и ребро на исполнении настоящее). Пропускается только
    тело `if TYPE_CHECKING:` при тесте ровно `TYPE_CHECKING` — импорт только для типов ребра на
    исполнении не создаёт; ветка `else:` и `if not TYPE_CHECKING:` исполняются и считаются.
    `importlib`/`__import__` со строкой разбором не видны — ограничение метода, такие формы в
    проекте не применяются."""
    tree = ast.parse(source)
    typing_only: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for statement in node.body:
                typing_only.update(id(inner) for inner in ast.walk(statement))
    parts = package.split(".")
    found: list[str] = []
    for node in ast.walk(tree):
        if id(node) in typing_only:
            continue
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = parts[: len(parts) - (node.level - 1)] if node.level else []
            module = [node.module] if node.module else []
            found.extend(".".join([*base, *module, alias.name]) for alias in node.names)
        elif isinstance(node, ast.Attribute):
            name = _dotted(node)
            if name is not None and name.startswith("app."):
                found.append(name)
    return found


def _package_of(path: Path, app_root: Path) -> str:
    relative = path.relative_to(app_root).with_suffix("")
    parts = [p for p in relative.parts if p != "__init__"]
    return (
        ".".join(["app", *parts[:-1]]) if path.name != "__init__.py" else ".".join(["app", *parts])
    )


def _imports_accounting(name: str) -> bool:
    return name == "app.accounting" or name.startswith("app.accounting.")


def test_the_domain_never_reaches_accounting_at_runtime() -> None:
    """Правило слоёв (`accounting/.AGENTS.md`): учёт зависит от домена, домен от учёта — нет,
    **на исполнении** (только тело `if TYPE_CHECKING:` вне правила — 001.36 аннотирует так или
    через `Protocol`). Константы, нужные обоим (`STUB_QUOTA_GRANT_BYTES`), живут в домене;
    обратное ребро замкнуло бы `accounting.quota → domain.composition → accounting` в цикл
    импорта. Проверяется разбором дерева (не подстрокой в тексте): импорт в любой форме с
    разрешением относительных от пакета файла — `from app import accounting`, `from .. import
    accounting`, подпакеты домена — и обращение по атрибуту после `import app`."""
    domain = Path(multiplier.__file__).parent
    app_root = domain.parent
    offenders = sorted(
        str(path.relative_to(domain))
        for path in domain.rglob("*.py")
        if any(
            _imports_accounting(name)
            for name in _runtime_imports(
                path.read_text(encoding="utf-8"), _package_of(path, app_root)
            )
        )
    )
    assert offenders == []
    # Сам разбор ловит все формы ребра и не считает ребром только тело `if TYPE_CHECKING:`.
    planted = (
        "from app import accounting\nimport app.accounting.quota\nfrom ..accounting import x\n"
        "from .. import accounting\nfrom . import nodes\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from app.accounting import y\n"
        "else:\n    from app.accounting import z\n"
        "if not TYPE_CHECKING:\n    from app.accounting import w\n"
        "import typing\nif typing.TYPE_CHECKING:\n    from app.accounting import v\n"
        "import settings\nif settings.TYPE_CHECKING:\n    from app.accounting import u\n"
        "import app\n\ndef f() -> int:\n    return app.accounting.quota.STUB_ISSUED_SEQ\n"
    )
    assert sorted(set(_runtime_imports(planted, "app.domain"))) == [
        "app",
        "app.accounting",
        "app.accounting.quota",
        "app.accounting.quota.STUB_ISSUED_SEQ",
        "app.accounting.u",
        "app.accounting.w",
        "app.accounting.x",
        "app.accounting.z",
        "app.domain.nodes",
        "settings",
        "typing",
        "typing.TYPE_CHECKING",
    ]
    assert _package_of(domain / "nodes.py", app_root) == "app.domain"
    assert _package_of(domain / "sub" / "mod.py", app_root) == "app.domain.sub"


def test_the_grant_request_takes_only_canonical_user_ids() -> None:
    """Канонические формы — свойство обеих операций раздела, не только отчёта: `urn:uuid:` и
    hex без дефисов в запросе гранта отвергаются той же формой `CanonicalUuid`."""
    canonical = str(USER)
    QuotaRequestIn.model_validate({"user_id": canonical, "consumed_bytes": 1})
    for wrong in ("urn:uuid:" + canonical, canonical.replace("-", ""), canonical.upper() + "x"):
        with pytest.raises(ValidationError):
            QuotaRequestIn.model_validate({"user_id": wrong, "consumed_bytes": 1})


async def test_the_signals_window_is_bounded() -> None:
    """Окно признаков — от 1 до `SIGNAL_WINDOW_MAX_DAYS` суток: маршрут панели, передавший
    `days` как есть, иначе читал бы историю без верхней границы (001.39)."""
    assert signals.SIGNAL_WINDOW_MAX_DAYS == 90
    assert await signals.resale_signals(Sentinel(), USER, days=90) == signals.STUB_SIGNALS
    for days in (0, 91):
        with pytest.raises(ValueError, match="окно признаков"):
            await signals.resale_signals(Sentinel(), USER, days=days)
