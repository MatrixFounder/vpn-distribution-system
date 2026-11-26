"""Плагин pytest харнесса посадок (``plantings_run.py``, WI-42): пишет в JSON исход каждого теста
и классы исключений провалов. Вывод pytest для этого не годится: строки сводки без терминала
обрезаются до 80 колонок, и класс исключения в них теряется.

Харнесс копирует плагин в свой каталог под именем ``plantings_probe_harness.py`` и подключает
его ключом ``-p plantings_probe_harness`` и каталогом в ``PYTHONPATH``; там же лежит
наблюдатель ``plantings_watch.py`` под именем ``sitecustomize.py``. Файл отчёта — переменная
окружения ``PLANTINGS_PROBE_OUT``. Отчёт::

    {"collected": [nodeid, ...],
     "tests": {nodeid: {"outcome": "passed|failed|skipped",
                        "failures": [{"when": "setup|call|teardown",
                                      "types": [MRO класса исключения, "модуль.имя"],
                                      "message": "..."}],
                        "raised": {"класс @ файл:функция": MRO}}},  # ошибки посадки в коде
     "collect_errors": [{"nodeid": "...", "types": [...], "message": "..."}],
     "modules": {имя пакета верхнего уровня: путь его файла или каталога},
     "rootdir": корень pytest,
     "watch": наблюдатель работал}

На время каждого теста плагин ставит ``PLANTINGS_PROBE_TEST`` — его наследуют подпроцессы теста.
Сторож: если харнесс (``PLANTINGS_PROBE_PARENT``) умер, pytest снимает свою группу процессов —
зависший прогон не переживает харнесс, снятый даже ``SIGKILL``.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from collections.abc import Generator
from typing import Any

import pytest

OUT_ENV = "PLANTINGS_PROBE_OUT"
PARENT_ENV = "PLANTINGS_PROBE_PARENT"
TEST_ENV = "PLANTINGS_PROBE_TEST"
RAISES_ENV = "PLANTINGS_PROBE_RAISES"
_MESSAGE = 500
_WATCHDOG_PERIOD = 0.5

_report: dict[str, Any] = {
    "collected": [],
    "tests": {},
    "collect_errors": [],
    "modules": {},
    "rootdir": None,
    "watch": False,
}


def _watch() -> Any:
    """Наблюдатель этого процесса — ``sitecustomize`` с признаком ``PLANTINGS_WATCH``."""
    module = sys.modules.get("sitecustomize")
    return module if getattr(module, "PLANTINGS_WATCH", False) is True else None


def _types(excinfo: pytest.ExceptionInfo[BaseException] | None) -> list[str]:
    if excinfo is None:
        return []
    return [f"{c.__module__}.{c.__qualname__}" for c in excinfo.type.__mro__]


def _message(excinfo: pytest.ExceptionInfo[BaseException] | None) -> str:
    if excinfo is None:
        return ""
    return excinfo.exconly()[:_MESSAGE]


def _guard(parent: int) -> None:
    while True:
        time.sleep(_WATCHDOG_PERIOD)
        if os.getppid() != parent:
            os.killpg(os.getpgrp(), signal.SIGKILL)


def pytest_configure(config: pytest.Config) -> None:
    _report["rootdir"] = str(config.rootpath)
    watch = _watch()
    if watch is not None:
        watch.DUMP = False
        _report["watch"] = bool(watch.ACTIVE)
    parent = os.environ.get(PARENT_ENV)
    if parent and parent.isdigit():
        threading.Thread(target=_guard, args=(int(parent),), daemon=True).start()


def pytest_collection_finish(session: pytest.Session) -> None:
    _report["collected"] = [item.nodeid for item in session.items]


def pytest_exception_interact(
    node: pytest.Item | pytest.Collector,
    call: pytest.CallInfo[Any],
    report: pytest.CollectReport | pytest.TestReport,
) -> None:
    if isinstance(report, pytest.CollectReport):
        _report["collect_errors"].append(
            {
                "nodeid": report.nodeid,
                "types": _types(call.excinfo),
                "message": _message(call.excinfo),
            }
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None]:
    os.environ[TEST_ENV] = item.nodeid
    try:
        yield
    finally:
        os.environ.pop(TEST_ENV, None)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Generator[None]:
    outcome: Any = yield
    report: pytest.TestReport = outcome.get_result()
    entry = _report["tests"].setdefault(
        item.nodeid, {"outcome": "passed", "failures": [], "raised": {}}
    )
    if report.failed:
        entry["outcome"] = "failed"
        entry["failures"].append(
            {"when": report.when, "types": _types(call.excinfo), "message": _message(call.excinfo)}
        )
    elif report.skipped and entry["outcome"] == "passed":
        entry["outcome"] = "skipped"


def pytest_collectreport(report: pytest.CollectReport) -> None:
    """Провал сбора без исключения в ``pytest_exception_interact`` (например, ошибка
    синтаксиса conftest) — тоже ошибка сбора."""
    if report.failed and not any(e["nodeid"] == report.nodeid for e in _report["collect_errors"]):
        _report["collect_errors"].append(
            {"nodeid": report.nodeid, "types": [], "message": str(report.longrepr)[:_MESSAGE]}
        )


def _raised() -> dict[str, dict[str, list[str]]]:
    """Перехваченное наблюдателем: своё — из памяти, подпроцессов — из их файлов."""
    merged: dict[str, dict[str, list[str]]] = {}
    sources: list[dict[str, dict[str, list[str]]]] = []
    watch = _watch()
    if watch is not None:
        sources.append(watch.RAISED)
    folder = os.environ.get(RAISES_ENV)
    if folder and os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                sources.append(json.load(fh))
    for source in sources:
        for test, kinds in source.items():
            merged.setdefault(test, {}).update(kinds)
    return merged


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    for test, kinds in _raised().items():
        if test in _report["tests"]:
            _report["tests"][test]["raised"].update(kinds)
    for name, module in list(sys.modules.items()):
        if "." not in name and module is not None:
            paths = list(getattr(module, "__path__", None) or [])
            _report["modules"][name] = getattr(module, "__file__", None) or (
                paths[0] if paths else None
            )
    path = os.environ.get(OUT_ENV)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_report, fh, ensure_ascii=False)
