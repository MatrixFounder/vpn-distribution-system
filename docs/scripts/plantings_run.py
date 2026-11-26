#!/usr/bin/env python3
"""Харнесс посадок проекта (правила 2 и 3 «Цикла задачи», CLAUDE.md; WI-42).

Посадка — правка кода, которую должен поймать страж: образец ``old`` заменяется на ``new`` в
копии дерева, затем идёт прогон подмножества тестов. Рабочая копия не меняется.

Прогон::

    plantings_run.py dry --plantings P                 # образцы против рабочей копии
    plantings_run.py run --plantings P --log L МЕТКА…  # или --all, или --affected

- Копия — дерево ``snapshot()`` рабочей копии (``plantings_affected.py``), выгруженное блобами
  git как есть (без фильтров, перевода строк и ``export-ignore``): измеряется ровно то дерево,
  которое пишется в журнал. Игнорируемые файлы (``.venv``, кэши) в копию не попадают;
  интерпретатор — ``PYTHON`` рабочей копии.
- Правки посадки применяются по порядку; образец каждой встречается ``count`` раз и в исходном
  файле (так считает ``compare``), и после предыдущих правок — иначе ``dry`` и ``run`` отвечают
  «образец не совпал» до прогонов.
- Базовый прогон: каждое подмножество в начале запуска идёт на копии без посадки. Провал, ошибка
  сбора, ни одного собранного теста, пропущенный или ненайденный ожидаемый тест, корень pytest не
  ``RUN_DIR`` — останов с кодом 3: на таком подмножестве вердикт посадки ничего не значит. База
  живёт один запуск; после ``TIMEOUT`` или ``ERROR`` (финализаторы фикстур могли не выполниться,
  стенд мог остаться грязным) подмножество следующей посадки снимается заново.
- Предел времени — ``timeout`` посадки для подмножества (по умолчанию ``TIMEOUT``) и ``TIMEOUT``
  для полного набора; по пределу группа процессов pytest снимается (TERM, через ``KILL_GRACE`` с
  — KILL), вердикт ``TIMEOUT``. Снятие самого харнесса (SIGTERM, SIGHUP) снимает и pytest; при
  ``SIGKILL`` харнесса pytest снимает свою группу сам (сторож плагина).
- Вердикт (решение заказчика 2026-11-26, WI-42) — по отчёту плагина ``plantings_probe.py`` и
  наблюдателя ``plantings_watch.py``, а не по выводу pytest. Запись ``expect`` (тест, тест с
  параметрами, класс или модуль) красная, если упал хотя бы один её тест, и каждый её провал — в
  теле теста (``call``) на проверке (``AssertionError`` и подклассы, ``pytest.fail``, «DID NOT
  RAISE» — ``_pytest.outcomes.Failed``) или на исключении из ``raises`` посадки, и в коде прогона
  вне тестов за время её упавших тестов не возникло новой, по сравнению с базовым прогоном,
  необъявленной «ошибки самой посадки» (``NameError``, ``SyntaxError``, ``ImportError``, SQL
  класса 42 кроме 42501 — см. ``plantings_watch.py``; в том числе в подпроцессах тестов).

  - ``RED`` — красная каждая запись ``expect``;
  - ``EXCEPTION`` — у записи провал на необъявленном исключении, провал в setup или teardown,
    или ошибка самой посадки в коде прогона (перехваченная кодом или вылетевшая): возможно,
    сломана посадка; объявить ``raises`` или
    исправить посадку;
  - ``PARTIAL`` — часть записей красная, часть зелёная или пропущена;
  - ``COLLECT`` — тест записи не собран из-за ошибки сбора; ``MISSING`` — тестов записи нет;
  - ``OTHER`` — ожидаемые тесты зелёные, упали другие или есть ошибка сбора; ``GREEN`` — не упало
    ничего: по правилу 2 зелёная на подмножестве посадка перемеряется полным набором ``FULL``
    (``--no-full`` отключает), и решает он;
  - ``TIMEOUT``; ``ERROR`` — pytest без отчёта плагина, с кодом ошибки использования или
    внутренней, наблюдатель не работал, корень pytest не ``RUN_DIR``, или модуль каталога прогона
    импортирован не из копии (затенение установкой).

  Имя в ``raises`` без точки — встроенное исключение (``NameError``); прочие — с модулем, полным
  или по суффиксу после точки (``redis.exceptions.ConnectionError``, ``exceptions.RedisError``);
  ищется в MRO. Измерена только ``RED``; ``plantings_affected.py compare`` перемеряет остальное.
- Журнал ``--log`` — только строки прогонов: ``label``, ``verdict``, ``tree``, ``spec`` (их
  читает ``compare``), ``what``, ``scope`` (``subset`` или ``full``), ``args``, ``detail``,
  ``failed``, ``secs``. Вывод pytest — ``<журнал>-out/<метка>/{subset,full}.txt`` и
  ``<журнал>-out/_base/``; итоги хуков — ``<журнал>.hooks.jsonl``.
- Хук посадки ``after`` — имя функции модуля посадок; вызывается после каждого прогона с посадкой
  (подмножество, затем полный набор; и при ``TIMEOUT``) с ``HookContext``: восстановить общий
  стенд, который посадка могла изменить (миграция). Исключение хука — останов с кодом 3.
- ``--affected`` берёт метки из ``compare`` (правило 3) и печатает его вывод для отчёта;
  несовпавший образец — код 1 до прогона. Метки — аргументы процесса, без оболочки.
- ``PYTEST_ADDOPTS`` окружения в прогон не передаётся: правило 2 — без ``-x`` и ``--maxfail``.

Посадки — модуль Python (``PLANTS`` и настройки ``SUBSETS``, ``FULL``, ``COPY``, ``RUN_DIR``,
``PYTHON``, ``TIMEOUT``, функции хуков; исполняется один раз) или JSON (``{"plantings": [...],
"subsets": {...}, "full": [...], "copy": [...], "run_dir": "...", "python": "...", "timeout":
N}``; без хуков). Поля посадки: поля ``plantings_affected.py`` (``label``, ``what``, ``file``,
``old``, ``new``, ``count``, ``expect``, ``inputs``, ``extra``) и ``subset`` (имя из ``SUBSETS``
или список аргументов pytest — пути и идентификаторы тестов от ``RUN_DIR``, без ключей),
``raises``, ``after``, ``timeout``. Метка — буквы, цифры, ``.``, ``_``, ``-``, с буквы или цифры;
метки различаются без учёта регистра. Значения посадки — JSON: ``spec`` в журнале — отпечаток
описания, функция в нём менялась бы от прогона к прогону.

Коды: 0 — все измеренные посадки ``RED``; 1 — есть иные вердикты или образец не совпал; 2 —
негодный вход; 3 — останов (базовый прогон, хук, выгрузка дерева); 128+N — харнесс снят
сигналом N.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import plantings_affected as pa  # noqa: E402

HERE = Path(__file__).resolve().parent
PROBE = HERE / "plantings_probe.py"
# Под этим именем плагин копируется в свой каталог: каталог прогона (docs/scripts) может
# держать свой plantings_probe.py, и -p взял бы его из текущего каталога.
PROBE_MODULE = "plantings_probe_harness"
WATCH = HERE / "plantings_watch.py"
# Имена переменных окружения — те же, что в plantings_probe.py и plantings_watch.py.
OUT_ENV = "PLANTINGS_PROBE_OUT"
PARENT_ENV = "PLANTINGS_PROBE_PARENT"
ROOT_ENV = "PLANTINGS_PROBE_ROOT"
RAISES_ENV = "PLANTINGS_PROBE_RAISES"
TEST_ENV = "PLANTINGS_PROBE_TEST"
# Переменные внешнего прогона (харнесс под харнессом) не переходят во вложенный: сторож сравнил
# бы pid чужого харнесса и снял прогон сразу.
DROPPED_ENV = ("PYTEST_ADDOPTS", TEST_ENV, OUT_ENV, PARENT_ENV, ROOT_ENV, RAISES_ENV)
ACCEPTED = ("builtins.AssertionError", "_pytest.outcomes.Failed")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
KEYS = {
    "label", "what", "file", "old", "new", "count", "expect", "inputs", "extra",
    "subset", "raises", "after", "timeout",
}  # fmt: skip
SETTINGS = {
    "subsets": {},
    "full": ["tests"],
    "copy": ["control-plane", "contracts", "docs", "deploy"],
    "run_dir": "control-plane",
    "python": "control-plane/.venv/bin/python",
    "timeout": 900,
}
KILL_GRACE = 10.0
SHOWN = 20
WORK_PARTS = ("tree", "run", "baseline", "probe")


class StopError(Exception):
    """Останов прогона: базовый прогон не зелёный, хук упал, дерево не выгружено."""


@dataclass
class HookContext:
    """Аргумент хука ``after``."""

    repo: Path
    copy: Path
    label: str
    verdict: str
    env: dict[str, str]


@dataclass
class Spec:
    """Посадка харнесса: описание ``plantings_affected`` и поля прогона."""

    planting: pa.Planting
    args: list[str]
    raises: list[str]
    after: Callable[[HookContext], Any] | None
    after_name: str | None
    timeout: float


@dataclass
class Config:
    specs: list[Spec]
    full: list[str]
    copy: list[str]
    run_dir: str
    python: str
    timeout: float
    hooks: dict[str, Callable[[HookContext], Any]] = field(default_factory=dict)


# --- описание посадок -----------------------------------------------------------------------


def _raw(path: Path) -> tuple[Any, dict[str, Any], Any]:
    """Сырые посадки, настройки и модуль (``None`` для JSON); модуль исполняется один раз."""
    if path.suffix == ".py":
        spec = importlib.util.spec_from_file_location("_plantings_run_harness", path)
        if spec is None or spec.loader is None:
            raise pa.InputError(f"{path}: модуль посадок не загружается")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        settings = {k: getattr(module, k.upper()) for k in SETTINGS if hasattr(module, k.upper())}
        return getattr(module, "PLANTS", None), settings, module
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data, {}, None
    if not isinstance(data, dict):
        raise pa.InputError(f"{path}: нужен список посадок или объект с plantings")
    unknown = set(data) - set(SETTINGS) - {"plantings"}
    if unknown:
        raise pa.InputError(f"{path}: неизвестные ключи {sorted(unknown)}")
    return data.get("plantings"), {k: data[k] for k in SETTINGS if k in data}, None


def _strings(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise pa.InputError(f"{what}: нужен список непустых строк")
    return list(value)


def _pytest_args(value: Any, what: str) -> list[str]:
    args = _strings(value, what)
    if not args:
        raise pa.InputError(f"{what}: пустое подмножество")
    flags = [a for a in args if a.startswith("-")]
    if flags:
        raise pa.InputError(
            f"{what}: ключи pytest {flags} — подмножество только из путей и тестов (правило 2: "
            "без -x и --maxfail)"
        )
    return args


def _timeout(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise pa.InputError(f"{what}: нужен предел времени в секундах больше нуля")
    return float(value)


def _raises(value: Any, what: str) -> list[str]:
    names = _strings(value, what)
    for name in names:
        if "." in name:
            continue
        kind = getattr(builtins, name, None)
        if not (isinstance(kind, type) and issubclass(kind, BaseException)):
            raise pa.InputError(
                f"{what}: {name!r} — не встроенное исключение; прочие — с модулем, например "
                "redis.exceptions.ConnectionError"
            )
    return names


def load(path: Path) -> Config:
    raws, settings, module = _raw(path)
    merged = {**SETTINGS, **settings}
    subsets = merged["subsets"]
    if not isinstance(subsets, dict):
        raise pa.InputError(f"{path}: SUBSETS — словарь имя → аргументы pytest")
    subsets = {str(k): _pytest_args(v, f"{path}: подмножество {k}") for k, v in subsets.items()}
    full = _pytest_args(merged["full"], f"{path}: FULL")
    copy = [pa._relative(p, "COPY") for p in _strings(merged["copy"], f"{path}: COPY")]
    if not copy:
        raise pa.InputError(f"{path}: COPY пуст")
    run_dir = pa._relative(str(merged["run_dir"]), "RUN_DIR")
    if not any(run_dir == c or run_dir.startswith(c + "/") for c in copy):
        raise pa.InputError(f"{path}: RUN_DIR {run_dir} вне каталогов COPY {copy}")
    timeout = _timeout(merged["timeout"], f"{path}: TIMEOUT")
    plantings = {p.label: p for p in pa.parse_plantings(raws, path)}
    specs: list[Spec] = []
    folded: dict[str, str] = {}
    for raw in raws:
        label = str(raw.get("label"))
        where = f"{path}: {label}"
        if not LABEL.match(label):
            raise pa.InputError(f"{where}: метка — буквы, цифры, '.', '_', '-', с буквы или цифры")
        if label.casefold() in folded:
            raise pa.InputError(
                f"{where}: метка совпадает с {folded[label.casefold()]} без регистра"
            )
        folded[label.casefold()] = label
        unknown = set(raw) - KEYS
        if unknown:
            raise pa.InputError(f"{where}: неизвестные поля {sorted(unknown)}")
        try:
            json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise pa.InputError(f"{where}: значения посадки — только JSON ({exc})") from exc
        planting = plantings[label]
        for number, edit in enumerate(planting.edits, 1):
            if not isinstance(edit.new, str):
                raise pa.InputError(f"{where}: правка {number} без замены new")
            if edit.new == edit.old:
                raise pa.InputError(f"{where}: правка {number}: замена совпадает с образцом")
        if not planting.expect:
            raise pa.InputError(f"{where}: пустой expect")
        subset = raw.get("subset")
        if isinstance(subset, str):
            if subset not in subsets:
                raise pa.InputError(f"{where}: подмножества {subset!r} нет в SUBSETS")
            args = subsets[subset]
        else:
            args = _pytest_args(subset, f"{where}: subset")
        after_name = raw.get("after")
        after = None
        if after_name is not None:
            if module is None:
                raise pa.InputError(f"{where}: хук after — только в модуле посадок, не в JSON")
            after = getattr(module, str(after_name), None)
            if not callable(after):
                raise pa.InputError(f"{where}: хука {after_name!r} нет в модуле посадок")
        specs.append(
            Spec(
                planting=planting,
                args=args,
                raises=_raises(raw.get("raises", []), f"{where}: raises"),
                after=after,
                after_name=str(after_name) if after_name is not None else None,
                timeout=_timeout(raw["timeout"], f"{where}: timeout")
                if "timeout" in raw
                else timeout,
            )
        )
    return Config(specs, full, copy, run_dir, str(merged["python"]), timeout)


# --- копия дерева и правки --------------------------------------------------------------------


def export(root: Path, tree: str, paths: list[str], dest: Path) -> None:
    """Каталоги ``paths`` дерева ``tree`` — в ``dest`` блобами git как есть: без фильтров,
    перевода строк и ``export-ignore`` (их применил бы ``git archive``)."""
    try:
        listing = pa._git(root, "ls-tree", "-r", "-z", "--full-tree", tree, "--", *paths)
    except pa.InputError as exc:
        raise StopError(str(exc)) from exc
    entries = [e for e in listing.split(b"\0") if e]
    names = [e.partition(b"\t")[2].decode("utf-8") for e in entries]
    missing = [p for p in paths if not any(n == p or n.startswith(p + "/") for n in names)]
    if missing:
        raise StopError(f"COPY: {missing} нет в дереве {tree[:12]}")
    dest.mkdir(parents=True)
    batch = pa._Batch(root)
    try:
        for entry, rel in zip(entries, names, strict=True):
            mode, kind, sha = entry.partition(b"\t")[0].decode("ascii").split(" ")
            if kind != "blob":
                continue  # подмодуль
            data = batch.blob(sha)
            if data is None:
                raise StopError(f"блоб {sha} ({rel}) не прочитан")
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if mode == "120000":
                os.symlink(data.decode("utf-8"), target)
                continue
            target.write_bytes(data)
            if mode == "100755":
                target.chmod(0o755)
    finally:
        batch.close()


def _reader(root: Path) -> Callable[[str], str | None]:
    def read(rel: str) -> str | None:
        path = root / rel
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8", newline="")
        except UnicodeDecodeError as exc:
            raise pa.InputError(f"{rel}: не UTF-8 ({exc})") from exc

    return read


def apply(planting: pa.Planting, read: Callable[[str], str | None]) -> dict[str, str]:
    """Тексты файлов после правок посадки. Образец каждой правки встречается ``count`` раз в
    исходном файле (так считает ``compare``) и после предыдущих правок того же файла."""
    originals: dict[str, str] = {}
    texts: dict[str, str] = {}
    for number, edit in enumerate(planting.edits, 1):
        where = f"правка {number} ({edit.file})"
        if edit.file not in originals:
            text = read(edit.file)
            if text is None:
                raise pa.InputError(f"{where}: нет файла")
            originals[edit.file] = texts[edit.file] = text
        before = originals[edit.file].count(edit.old)
        now = texts[edit.file].count(edit.old)
        if before != edit.count:
            raise pa.InputError(
                f"{where}: образец {before} раз вместо {edit.count}: {edit.old[:60]!r}"
            )
        if now != edit.count:
            raise pa.InputError(
                f"{where}: после предыдущих правок образец {now} раз вместо {edit.count}: "
                f"{edit.old[:60]!r}"
            )
        assert edit.new is not None  # noqa: S101 — проверено в load()
        texts[edit.file] = texts[edit.file].replace(edit.old, edit.new)
    return texts


def plant(copy: Path, planting: pa.Planting) -> None:
    """Правки посадки в копии; каждая замена доказана: число вхождений до, текст после."""
    try:
        texts = apply(planting, _reader(copy))
    except pa.InputError as exc:
        raise pa.InputError(f"{planting.label}: {exc}") from exc
    for rel, text in texts.items():
        target = copy / rel
        target.write_text(text, encoding="utf-8", newline="")
        if target.read_text(encoding="utf-8", newline="") != text:
            raise StopError(f"{planting.label}: запись {rel} в копии не подтвердилась")


def mismatches(root: Path, specs: Iterable[Spec]) -> list[tuple[str, str]]:
    """Посадки, правки которых к дереву ``root`` не применяются: метка и описание."""
    out: list[tuple[str, str]] = []
    for spec in specs:
        try:
            apply(spec.planting, _reader(root))
        except pa.InputError as exc:
            out.append((spec.planting.label, str(exc)))
    return out


# --- прогон pytest ----------------------------------------------------------------------------


@dataclass
class Result:
    rc: int | None  # None — предел времени
    report: dict[str, Any] | None
    secs: float
    output: Path


def pytest_run(
    python: str, cwd: Path, args: list[str], timeout: float, probe_dir: Path, output: Path
) -> Result:
    report_path = output.with_suffix(".probe.json")
    report_path.unlink(missing_ok=True)
    raises_dir = output.with_suffix(".raises")
    shutil.rmtree(raises_dir, ignore_errors=True)
    raises_dir.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k not in DROPPED_ENV}
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(probe_dir), env.get("PYTHONPATH")) if p)
    env[OUT_ENV] = str(report_path)
    env[PARENT_ENV] = str(os.getpid())
    env[ROOT_ENV] = str(cwd.resolve())
    env[RAISES_ENV] = str(raises_dir)
    cmd = [
        python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", PROBE_MODULE,
        "--continue-on-collection-errors", *args,
    ]  # fmt: skip
    started = time.monotonic()
    with output.open("w", encoding="utf-8") as sink:
        sink.write(f"$ cd {cwd} && {' '.join(cmd)}\n")
        sink.flush()
        # Аргументы — список без оболочки: пути и тесты описания посадок.
        proc = subprocess.Popen(  # noqa: S603
            cmd, cwd=cwd, env=env, stdout=sink, stderr=subprocess.STDOUT, start_new_session=True
        )
        rc: int | None
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            rc = None
            sink.write(f"\nTIMEOUT {timeout:g} с\n")
        finally:
            _stop_group(proc)
    report = None
    if rc is not None and report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    return Result(rc, report, time.monotonic() - started, output)


def _stop_group(proc: subprocess.Popen[Any]) -> None:
    """Группа процессов pytest — сам pytest и запущенные им процессы, оставшиеся после него, —
    снимается: TERM, через ``KILL_GRACE`` с — KILL (процесс, ставящий свой обработчик TERM, как
    воркеры проекта, на TERM не выходит)."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        proc.wait()
        return
    deadline = time.monotonic() + KILL_GRACE
    while time.monotonic() < deadline:
        if proc.poll() is not None and not _group_alive(proc.pid):
            return
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- вердикт ----------------------------------------------------------------------------------


@dataclass
class Outcome:
    verdict: str
    detail: str
    failed: list[str]


def matches(entry: str, nodeid: str) -> bool:
    return nodeid == entry or any(nodeid.startswith(entry + s) for s in ("::", "[", "/"))


def declared(types: list[str], raises: list[str]) -> bool:
    """Класс из ``raises`` в MRO: имя без точки — встроенное исключение, с точкой — полное
    имя или его суффикс после точки."""
    for name in raises:
        if "." not in name:
            if f"builtins.{name}" in types:
                return True
        elif any(t == name or t.endswith("." + name) for t in types):
            return True
    return False


def accepted(types: list[str], raises: list[str]) -> bool:
    return any(t in ACCEPTED for t in types) or declared(types, raises)


def shadowed(report: dict[str, Any], run_dir: Path) -> list[str]:
    """Модули верхнего уровня каталога прогона, импортированные не из копии; плагин и наблюдатель
    харнесса импортируются из своего каталога и не в счёт."""
    local = {
        p.stem if p.suffix == ".py" else p.name
        for p in run_dir.iterdir()
        if (p.is_dir() and (p / "__init__.py").is_file()) or p.suffix == ".py"
    } - {PROBE_MODULE, "sitecustomize"}
    out = []
    for name, where in sorted(report.get("modules", {}).items()):
        if name in local and where and not Path(where).resolve().is_relative_to(run_dir.resolve()):
            out.append(f"{name} из {where}")
    return out


def _first(types: list[str]) -> str:
    return types[0] if types else "?"


def environment_problems(report: dict[str, Any], run_dir: Path) -> list[str]:
    """Прогон, вердикт которого ничего не значит, каким бы ни был исход тестов."""
    problems = []
    if not report.get("watch"):
        problems.append("наблюдатель исключений не работал (sys.monitoring недоступен или занят)")
    rootdir = report.get("rootdir")
    if not rootdir or Path(rootdir).resolve() != run_dir.resolve():
        problems.append(f"корень pytest {rootdir} — не RUN_DIR {run_dir}")
    if shadow := shadowed(report, run_dir):
        problems.append(f"импорт не из копии: {', '.join(shadow)}")
    return problems


def _new_raised(
    nodeid: str, report: dict[str, Any], base: dict[str, Any] | None, raises: list[str]
) -> list[str]:
    now = report["tests"].get(nodeid, {}).get("raised", {})
    before = base["tests"].get(nodeid, {}).get("raised", {}) if base else {}
    return sorted(t for t, mro in now.items() if t not in before and not declared(mro, raises))


def judge(
    result: Result,
    expect: list[str],
    raises: list[str],
    run_dir: Path,
    base: dict[str, Any] | None = None,
) -> Outcome:
    if result.rc is None:
        return Outcome("TIMEOUT", f"предел {result.secs:.0f} с", [])
    report = result.report
    if report is None:
        return Outcome("ERROR", f"pytest без отчёта плагина, код {result.rc}", [])
    if problems := environment_problems(report, run_dir):
        return Outcome("ERROR", "; ".join(problems), [])
    tests: dict[str, Any] = report["tests"]
    failed = [n for n, t in tests.items() if t["outcome"] == "failed"]
    errors = report["collect_errors"]
    if result.rc not in (0, 1, 5) and not errors:
        return Outcome("ERROR", f"pytest завершился с кодом {result.rc}", failed)
    if result.rc == 1 and not failed and not errors:
        return Outcome("ERROR", "pytest вернул 1 без провалов и ошибок сбора", failed)
    known = set(report["collected"]) | set(tests)
    states: dict[str, str] = {}
    notes: dict[str, list[str]] = {}
    for entry in expect:
        names = sorted(n for n in known if matches(entry, n))
        if not names:
            file = entry.split("::")[0].split("[")[0]
            hit = [e for e in errors if not e["nodeid"] or matches(e["nodeid"], file)]
            states[entry] = "collect" if hit else "missing"
            continue
        fails = [(n, f) for n in names for f in tests.get(n, {}).get("failures", [])]
        if not fails:
            quiet = all(tests.get(n, {}).get("outcome") == "skipped" for n in names)
            states[entry] = "skipped" if quiet else "green"
            continue
        bad = [
            f"{n}: {_first(f['types'])}" + ("" if f["when"] == "call" else f" в {f['when']}")
            for n, f in fails
            if f["when"] != "call" or not accepted(f["types"], raises)
        ]
        bad += [
            f"{n}: в коде прогона {kind}"
            for n in sorted({n for n, _ in fails})
            for kind in _new_raised(n, report, base, raises)
        ]
        states[entry] = "exception" if bad else "red"
        if bad:
            notes[entry] = bad
    expected_failed = [n for n in failed if any(matches(e, n) for e in expect)]
    others = [n for n in failed if n not in expected_failed]
    shown = expected_failed + others

    def listed(state: str) -> str:
        return ", ".join(e for e, s in states.items() if s == state)

    values = set(states.values())
    if "collect" in values:
        causes = "; ".join(f"{e['nodeid'] or 'сессия'}: {e['message'][:120]}" for e in errors)
        return Outcome("COLLECT", f"не собраны {listed('collect')} — {causes}", shown)
    if "missing" in values:
        return Outcome("MISSING", f"нет тестов: {listed('missing')}", shown)
    if "exception" in values:
        parts = [f"{e}: {'; '.join(n[:SHOWN])}" for e, n in notes.items()]
        return Outcome("EXCEPTION", "необъявленное — " + " | ".join(parts), shown)
    if values == {"red"}:
        extra = ""
        if others:
            kinds = sorted({_first(f["types"]) for n in others for f in tests[n]["failures"]})
            extra = f"; ещё упали {len(others)} ({', '.join(kinds)})"
        if errors:
            extra += f"; ошибок сбора {len(errors)}"
        return Outcome("RED", f"красные все {len(expect)} из expect{extra}", shown)
    if "red" in values:
        rest = ", ".join(f"{e} ({s})" for e, s in states.items() if s != "red")
        return Outcome("PARTIAL", f"не красные: {rest}", shown)
    if others or errors:
        names = others[:5] + [f"сбор {e['nodeid'] or 'сессии'}" for e in errors][:5]
        return Outcome("OTHER", f"ожидаемые зелёные, упали другие: {', '.join(names)}", shown)
    skipped = listed("skipped")
    return Outcome("GREEN", f"пропущены: {skipped}" if skipped else "не упало ничего", shown)


def baseline_problems(result: Result, run_dir: Path) -> list[str]:
    """Базовый прогон, на котором посадки мерить нельзя."""
    if result.rc is None:
        return [f"предел времени {result.secs:.0f} с"]
    report = result.report
    if report is None:
        return [f"pytest без отчёта плагина, код {result.rc}"]
    problems = environment_problems(report, run_dir)
    problems += [f"упал {n}" for n, t in report["tests"].items() if t["outcome"] == "failed"]
    problems += [f"ошибка сбора {e['nodeid'] or 'сессии'}" for e in report["collect_errors"]]
    if result.rc == 5 or not report["collected"]:
        problems.append("pytest не собрал ни одного теста")
    elif result.rc != 0 and not problems:
        problems.append(f"pytest завершился с кодом {result.rc}")
    return problems


def expect_problems(report: dict[str, Any], expect: Iterable[str]) -> list[str]:
    """Ожидаемые тесты, которых в базе нет или которые в ней пропущены."""
    problems = []
    known = set(report["collected"]) | set(report["tests"])
    for entry in sorted(set(expect)):
        names = [n for n in known if matches(entry, n)]
        if not names:
            problems.append(f"expect {entry}: таких тестов в подмножестве нет")
        elif all(report["tests"].get(n, {}).get("outcome") == "skipped" for n in names):
            problems.append(f"expect {entry}: пропущен без посадки")
    return problems


# --- прогон посадок -----------------------------------------------------------------------------


def _key(args: list[str]) -> str:
    return hashlib.sha256(json.dumps(args).encode("utf-8")).hexdigest()[:12]


class Runner:
    def __init__(
        self, root: Path, config: Config, log: Path, work: Path, *, full: bool, quiet: bool
    ) -> None:
        self.root, self.config, self.log, self.work = root, config, log, work
        self.full, self.quiet = full, quiet
        self.out_dir = log.with_name(log.stem + "-out")
        self.hooks_log = log.with_name(log.stem + ".hooks.jsonl")
        python = Path(config.python)
        self.python = str(python if python.is_absolute() else root / python)
        self.base: dict[str, dict[str, Any]] = {}  # подмножество → отчёт базы, один запуск

    def say(self, text: str) -> None:
        if not self.quiet:
            print(text, flush=True)

    def _copy(self, name: str) -> Path:
        copy = self.work / name
        shutil.rmtree(copy, ignore_errors=True)
        shutil.copytree(self.pristine, copy, symlinks=True)
        return copy

    def baseline(self, args: list[str], expect: Iterable[str]) -> dict[str, Any]:
        report = self.base.get(_key(args))
        if report is None:
            copy = self._copy("baseline")
            run_dir = copy / self.config.run_dir
            out = self.out_dir / "_base"
            out.mkdir(parents=True, exist_ok=True)
            try:
                result = pytest_run(
                    self.python,
                    run_dir,
                    args,
                    self.config.timeout,
                    self.probe_dir,
                    out / f"{_key(args)}.txt",
                )
                problems = baseline_problems(result, run_dir)
            finally:
                shutil.rmtree(copy, ignore_errors=True)
            if problems:
                shown = "; ".join(problems[:SHOWN])
                where = " ".join(args)
                raise StopError(f"базовый прогон {where} не зелёный: {shown} — {result.output}")
            assert result.report is not None  # noqa: S101 — иначе problems не пуст
            report = self.base[_key(args)] = result.report
            self.say(f"база   {' '.join(args)}: зелёная ({result.secs:.0f} с)")
        if problems := expect_problems(report, expect):
            raise StopError(f"базовый прогон {' '.join(args)}: {'; '.join(problems[:SHOWN])}")
        return report

    def run(self, specs: list[Spec]) -> int:
        self.tree = pa.snapshot(self.root)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.probe_dir = self.work / "probe"
        self.probe_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROBE, self.probe_dir / f"{PROBE_MODULE}.py")
        shutil.copy2(WATCH, self.probe_dir / "sitecustomize.py")
        self.pristine = self.work / "tree"
        shutil.rmtree(self.pristine, ignore_errors=True)
        export(self.root, self.tree, self.config.copy, self.pristine)
        if bad := mismatches(self.pristine, specs):
            for label, line in bad:
                print(f"образец не совпал: {label}: {line}", file=sys.stderr)
            return 1
        for args in dict.fromkeys(tuple(s.args) for s in specs):
            expect = [e for s in specs if tuple(s.args) == args for e in s.planting.expect]
            self.baseline(list(args), expect)
        red = True
        for spec in specs:
            verdict = self.measure(spec)
            red = verdict == "RED" and red
            if verdict in ("TIMEOUT", "ERROR"):
                self.base.clear()  # стенд мог остаться грязным: следующая посадка — с базы
        return 0 if red else 1

    def _planted(
        self,
        spec: Spec,
        copy: Path,
        args: list[str],
        timeout: float,
        scope: str,
        base: dict[str, Any],
    ) -> tuple[Result, Outcome]:
        """Прогон с посадкой и его вердикт; хук ``after`` — после прогона, что бы ни случилось
        (при прерывании — с вердиктом ``ERROR``)."""
        out = self.out_dir / spec.planting.label
        out.mkdir(parents=True, exist_ok=True)
        run_dir = copy / self.config.run_dir
        verdict = "ERROR"
        try:
            result = pytest_run(
                self.python, run_dir, args, timeout, self.probe_dir, out / f"{scope}.txt"
            )
            outcome = judge(result, spec.planting.expect, spec.raises, run_dir, base)
            verdict = outcome.verdict
            return result, outcome
        finally:
            if spec.after is not None:
                self._hook(spec, copy, verdict)

    def measure(self, spec: Spec) -> str:
        planting = spec.planting
        copy = self._copy("run")
        try:
            plant(copy, planting)
            base = self.baseline(spec.args, planting.expect)
            scope, args = "subset", spec.args
            result, outcome = self._planted(spec, copy, args, spec.timeout, scope, base)
            secs = result.secs
            if outcome.verdict == "GREEN" and self.full and spec.args != self.config.full:
                base_full = self.baseline(self.config.full, planting.expect)
                scope, args = "full", self.config.full
                result, outcome = self._planted(
                    spec, copy, args, self.config.timeout, scope, base_full
                )
                secs += result.secs
            row = {
                "label": planting.label,
                "verdict": outcome.verdict,
                "tree": self.tree,
                "spec": planting.spec,
                "what": planting.what,
                "scope": scope,
                "args": args,
                "detail": outcome.detail,
                "failed": outcome.failed[:SHOWN],
                "secs": round(secs, 1),
            }
            with self.log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.say(
                f"{planting.label:6} {outcome.verdict:9} {outcome.detail} ({secs:.0f} с) — "
                f"{planting.what}"
            )
            return outcome.verdict
        finally:
            shutil.rmtree(copy, ignore_errors=True)

    def _hook(self, spec: Spec, copy: Path, verdict: str) -> None:
        context = HookContext(self.root, copy, spec.planting.label, verdict, dict(os.environ))
        row: dict[str, Any] = {"label": spec.planting.label, "hook": spec.after_name}
        try:
            assert spec.after is not None  # noqa: S101 — вызывается только при хуке
            note = spec.after(context)
        except Exception as exc:  # хук — код посадок: любой отказ останавливает прогон
            row["error"] = f"{type(exc).__name__}: {exc}"
            self._hook_row(row)
            raise StopError(
                f"хук {spec.after_name} посадки {spec.planting.label}: {row['error']}"
            ) from exc
        row["note"] = None if note is None else str(note)
        self._hook_row(row)
        if note is not None:
            self.say(f"       хук {spec.after_name}: {note}")

    def _hook_row(self, row: dict[str, Any]) -> None:
        with self.hooks_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def select(args: argparse.Namespace, root: Path, config: Config) -> list[Spec] | int:
    by_label = {s.planting.label: s for s in config.specs}
    if args.all:
        return config.specs
    if args.affected:
        runs = pa.load_log(args.log) if args.log.is_file() else {}
        verdicts = pa.compare([s.planting for s in config.specs], root, config.run_dir, runs)
        print(pa.render(verdicts), flush=True)
        if any(v.mismatch for v in verdicts):
            return 1
        return [by_label[v.label] for v in verdicts if v.reasons]
    unknown = [label for label in args.labels if label not in by_label]
    if unknown:
        raise pa.InputError(f"нет посадок с метками {unknown}")
    wanted = set(args.labels)
    return [s for s in config.specs if s.planting.label in wanted]


def _interrupted(signum: int, frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)  # finally снимут группу pytest и уберут копии


def main(argv: list[str] | None = None) -> int:
    previous = {s: signal.signal(s, _interrupted) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        return _main(argv)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _main(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="корень репозитория")
    sub = parser.add_subparsers(dest="command", required=True)
    dry_parser = sub.add_parser("dry", help="образцы посадок против рабочей копии")
    dry_parser.add_argument("--plantings", required=True, type=Path, help="модуль .py или JSON")
    run_parser = sub.add_parser("run", help="измерить посадки")
    run_parser.add_argument("--plantings", required=True, type=Path, help="модуль .py или JSON")
    run_parser.add_argument("--log", required=True, type=Path, help="журнал прогонов (JSONL)")
    run_parser.add_argument("--work", type=Path, help="пустой каталог копий вне репозитория")
    run_parser.add_argument("--no-full", action="store_true", help="зелёную не перемерять FULL")
    run_parser.add_argument("--keep", action="store_true", help="не удалять каталог копий")
    run_parser.add_argument("--quiet", action="store_true", help="без строк прогресса")
    chosen = run_parser.add_mutually_exclusive_group(required=True)
    chosen.add_argument("--all", action="store_true", help="все посадки")
    chosen.add_argument("--affected", action="store_true", help="отобранные compare (правило 3)")
    chosen.add_argument("labels", nargs="*", default=[], help="метки посадок")
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    try:
        config = load(args.plantings)
        if args.command == "dry":
            bad = mismatches(root, config.specs)
            for label, line in bad:
                print(f"образец не совпал: {label}: {line}")
            dry_count = len(config.specs) - len({label for label, _ in bad})
            print(f"сухо: {dry_count} из {len(config.specs)}")
            return 1 if bad else 0
        if args.log.is_file():
            pa.load_log(args.log)  # журнал читается compare: посторонняя строка — ошибка сразу
        selected = select(args, root, config)
        if isinstance(selected, int):
            return selected
        if not selected:
            print("посадок к прогону нет")
            return 0
        work = Path(tempfile.mkdtemp(prefix="plantings-")) if args.work is None else args.work
        work = work.resolve()
        made = args.work is None or not work.exists()
        if _inside(work, root) or _inside(args.log.parent, work):
            raise pa.InputError(f"каталог копий {work} — вне репозитория и не над журналом")
        if work.exists() and not made and any(work.iterdir()):
            raise pa.InputError(f"каталог копий {work} не пуст")
        work.mkdir(parents=True, exist_ok=True)
        args.log.parent.mkdir(parents=True, exist_ok=True)
        runner = Runner(root, config, args.log, work, full=not args.no_full, quiet=args.quiet)
        try:
            return runner.run(selected)
        finally:
            if not args.keep:
                if made:
                    shutil.rmtree(work, ignore_errors=True)
                else:
                    for name in WORK_PARTS:
                        shutil.rmtree(work / name, ignore_errors=True)
    except (pa.InputError, OSError, ValueError) as exc:
        print(f"plantings_run: {exc}", file=sys.stderr)
        return 2
    except StopError as exc:
        print(f"plantings_run: останов: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
