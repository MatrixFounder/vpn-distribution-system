"""Наблюдатель исключений кода прогона харнесса посадок (``plantings_run.py``, WI-42).

Харнесс копирует этот файл в каталог плагина под именем ``sitecustomize.py`` и кладёт каталог в
``PYTHONPATH``: модуль импортирует каждый процесс Python прогона — сам pytest и подпроцессы тестов
(``python -m app.cli``), если они наследуют окружение.

Зачем: код приложения перехватывает исключения (обработчик HTTP отвечает 500, CLI печатает ошибку и
выходит с кодом 1), и тест падает на проверке, хотя сломана сама посадка — ошибка SQL или
``NameError`` в её замене. Такой провал — не измерение стража (решение заказчика 2026-11-26).

Наблюдатель (``sys.monitoring``, событие ``RAISE``) записывает исключения «ошибки самой посадки» —
``NameError``, ``SyntaxError``, ``ImportError`` и ошибки SQL класса 42 (синтаксис, неизвестный
столбец, неопределённый тип; ``sqlstate`` 42xxx, кроме 42501 — нехватки прав), — поднятые или
прошедшие через кадр кода из ``PLANTINGS_PROBE_ROOT`` вне тестов (каталог ``tests``, файлы
``test_*.py`` и ``conftest.py``). Исключение относится к тесту из ``PLANTINGS_PROBE_TEST`` (его
ставит плагин на время теста; подпроцесс наследует значение). Ключ — класс и место: ``класс @
файл:функция`` (кадр, через который исключение прошло), чтобы та же ошибка, законно перехваченная
кодом и без посадки, не скрывала новую. Итог процесса — ``RAISED``; в подпроцессе он пишется при
выходе в ``PLANTINGS_PROBE_RAISES/<pid>.json``.
"""

from __future__ import annotations

import atexit
import json
import os
import sys
from typing import Any

PLANTINGS_WATCH = True  # признак для плагина: этот sitecustomize — наблюдатель харнесса
ROOT_ENV = "PLANTINGS_PROBE_ROOT"
RAISES_ENV = "PLANTINGS_PROBE_RAISES"
TEST_ENV = "PLANTINGS_PROBE_TEST"
TOOL_IDS = (3, 4)  # 0, 1, 2, 5 — отладчик, покрытие, профилировщик, оптимизатор
SUSPECT = (NameError, SyntaxError, ImportError)
PRIVILEGE = "42501"

ROOT = os.path.realpath(os.environ[ROOT_ENV]) if os.environ.get(ROOT_ENV) else None
RAISED: dict[str, dict[str, list[str]]] = {}  # тест → «класс @ файл:функция» → MRO класса
ACTIVE = False
DUMP = True  # плагин pytest снимает: свой процесс он читает из памяти
_app: dict[str, str | None] = {}


def app_path(filename: str) -> str | None:
    """Путь файла кода прогона вне тестов от ``ROOT``; ``None`` — не код прогона. Псевдоимена
    кода без файла (``<frozen importlib._bootstrap>``, ``<string>``) — не код прогона:
    ``realpath`` разрешил бы их относительно текущего каталога, то есть внутрь копии, и
    исключения машинерии импорта засчитались бы ошибкой посадки."""
    if filename in _app:
        return _app[filename]
    rel = None
    path = "" if filename.startswith("<") else os.path.realpath(filename)
    if ROOT is not None and path.startswith(ROOT + os.sep):
        parts = path[len(ROOT) + 1 :].split(os.sep)
        name = parts[-1]
        if "tests" not in parts[:-1] and not name.startswith("test_") and name != "conftest.py":
            rel = "/".join(parts)
    _app[filename] = rel
    return rel


def suspect(exc: BaseException) -> bool:
    if isinstance(exc, SUSPECT):
        return True
    state = getattr(exc, "sqlstate", None)
    return isinstance(state, str) and state.startswith("42") and state != PRIVILEGE


def _on_raise(code: Any, offset: int, exc: BaseException) -> None:
    if not suspect(exc):
        return
    rel = app_path(code.co_filename)
    if rel is not None:
        mro = [f"{c.__module__}.{c.__qualname__}" for c in type(exc).__mro__]
        key = f"{mro[0]} @ {rel}:{code.co_qualname}"
        RAISED.setdefault(os.environ.get(TEST_ENV, ""), {})[key] = mro


def _dump() -> None:
    folder = os.environ.get(RAISES_ENV)
    if folder and RAISED and DUMP:
        with open(os.path.join(folder, f"{os.getpid()}.json"), "w", encoding="utf-8") as fh:
            json.dump(RAISED, fh, ensure_ascii=False)


def install() -> bool:
    if ROOT is None or not hasattr(sys, "monitoring"):
        return False
    monitoring = sys.monitoring
    for tool in TOOL_IDS:
        try:
            monitoring.use_tool_id(tool, "plantings_watch")
        except ValueError:
            continue
        monitoring.register_callback(tool, monitoring.events.RAISE, _on_raise)
        monitoring.set_events(tool, monitoring.events.RAISE)
        atexit.register(_dump)
        return True
    return False


ACTIVE = install()
