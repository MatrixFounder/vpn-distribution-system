#!/usr/bin/env python3
"""Отбор посадок для перемера между раундами роаста (правило 3 «Цикла задачи», CLAUDE.md; WI-40).

Между раундами перемеряются только посадки, у которых изменился образец, код вокруг образца или
ожидаемые тесты; остальные проходят сухую проверку — образец совпадает с деревом столько раз,
сколько ждёт посадка. Скрипт сравнивает дерево со снимком, снятым перед раундом, и печатает для
каждой посадки «перемерить» с причинами или «сухо».

Что считается изменением (решения заказчика 2026-11-21, WI-40):

- код вокруг образца — наименьшая функция или класс Python, содержащие образец, либо оператор
  присваивания модуля; сравнивается AST без докстрингов, поэтому комментарии, переносы и
  докстринги перемера не вызывают. Образец в другом операторе модуля — сравнивается модуль
  целиком; файл не на Python — целиком, по тексту;
- ожидаемый тест — функция теста с декораторами (параметризация входит) и всё, что она берёт из
  тестовых модулей проекта, по цепочке: определения того же файла, импорты из пакета тестов,
  фикстуры по имени параметра и ``usefixtures`` (сам модуль, затем ближайший ``conftest.py``),
  автоматические фикстуры ``conftest.py`` по пути теста, ``pytestmark`` модуля. Код приложения в
  тест не входит: его правка вокруг образца ловится первым пунктом. Тест не на Python —
  сравнивается его файл целиком;
- образец — в снимке он встречается не столько раз, сколько ждёт посадка: образец новый или
  изменён.

Посадка, которой нет в журнале прогонов (``--log``), тоже перемеряется. Образец, который в
дереве встречается не столько раз, сколько ждёт посадка, — ошибка сухой проверки: код
возврата 1.

Команды::

    plantings_affected.py snapshot        # перед роастом: идентификатор дерева — в отчёт
    plantings_affected.py compare --base <снимок> --plantings <посадки> [--log <журнал>]

Снимок — дерево git рабочей копии вместе с неотслеживаемыми файлами, кроме игнорируемых (индекс
и рабочее дерево не меняются; объект дерева без ссылки живёт до сборки мусора git — недели),
либо каталог с копией дерева. Посадки — JSON (список объектов или ``{"plantings": [...]}``) либо
модуль харнесса со списком ``PLANTS`` (его запуск — под ``if __name__ == "__main__"``). Поля
посадки: ``label``, ``file`` (путь от корня репозитория), ``old`` (образец), ``count`` (сколько
раз, по умолчанию 1), ``expect`` (тесты ``путь::имя[параметры]`` от ``--tests-root``), ``what``
(подпись, необязательно). Журнал — JSON по строке на прогон с полем ``label``.
"""

from __future__ import annotations

import argparse
import ast
import copy
import importlib.util
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TESTS_ROOT = "control-plane"
GIT = shutil.which("git") or "git"
WHOLE_MODULE = "<модуль>"
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


class InputError(Exception):
    """Негодный вход: посадки, снимок, журнал."""


def _run_git(
    root: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    # Аргументы — фиксированный список без оболочки; ревизия снимка приходит от оператора.
    return subprocess.run(  # noqa: S603
        [GIT, "-C", str(root), *args], capture_output=True, env=env, check=False
    )


class Source:
    """Файлы одного состояния дерева: каталог или дерево git (``treeish``)."""

    def __init__(self, root: Path, treeish: str | None = None) -> None:
        self.root = root
        self.treeish = treeish
        self._cache: dict[str, str | None] = {}
        self._trees: dict[str, ast.Module | None] = {}

    def read(self, rel: str) -> str | None:
        if rel not in self._cache:
            self._cache[rel] = self._read(rel)
        return self._cache[rel]

    def tree(self, rel: str) -> ast.Module | None:
        """Разобранный файл (``None`` — нет файла или он не разбирается); узлы не изменять."""
        if rel not in self._trees:
            self._trees[rel] = _parse(self.read(rel))
        return self._trees[rel]

    def _read(self, rel: str) -> str | None:
        if self.treeish is None:
            path = self.root / rel
            return path.read_text(encoding="utf-8") if path.is_file() else None
        proc = _run_git(self.root, "show", f"{self.treeish}:{rel}")
        return proc.stdout.decode("utf-8") if proc.returncode == 0 else None


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, *_DEFS)) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                node.body = node.body[1:] or [ast.Pass()]
    return tree


def shape(node: ast.AST) -> str:
    """Исполняемая форма узла: AST без докстрингов и позиций (комментариев в AST нет)."""
    return ast.dump(_strip_docstrings(copy.deepcopy(node)), include_attributes=False)


def _parse(text: str | None) -> ast.Module | None:
    if text is None:
        return None
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


# --- код вокруг образца ---------------------------------------------------------------------


def enclosing(tree: ast.Module, text: str, old: str) -> list[str]:
    """Путь имён к наименьшей функции или классу с образцом (``["Class", "method"]``), имя
    присваивания модуля (``["_QUERY"]``) или ``[WHOLE_MODULE]``."""
    start = text.index(old)
    first = text.count("\n", 0, start) + 1
    last = first + old.rstrip("\n").count("\n")

    def inside(node: ast.stmt) -> bool:
        end = node.end_lineno if node.end_lineno is not None else node.lineno
        begin = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])
        return begin <= first and last <= end

    for stmt in tree.body:
        if not inside(stmt):
            continue
        if isinstance(stmt, _DEFS):
            path, body = [stmt.name], stmt.body
            while True:
                inner = next((s for s in body if isinstance(s, _DEFS) and inside(s)), None)
                if inner is None:
                    return path
                path, body = [*path, inner.name], inner.body
        names = _assigned_names(stmt)
        return [names[0]] if names else [WHOLE_MODULE]
    return [WHOLE_MODULE]


def _assigned_names(stmt: ast.stmt) -> list[str]:
    targets: list[ast.expr] = []
    if isinstance(stmt, ast.Assign):
        targets = list(stmt.targets)
    elif isinstance(stmt, ast.AnnAssign):
        targets = [stmt.target]
    return [n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)]


def find(tree: ast.Module, path: list[str]) -> ast.AST | None:
    if path == [WHOLE_MODULE]:
        return tree
    body: list[ast.stmt] = tree.body
    node: ast.AST | None = None
    for i, name in enumerate(path):
        node = None
        for stmt in body:
            if isinstance(stmt, _DEFS) and stmt.name == name:
                node = stmt
            elif i == 0 and not isinstance(stmt, _DEFS) and name in _assigned_names(stmt):
                node = stmt
        if node is None:
            return None
        body = node.body if isinstance(node, _DEFS) else []
    return node


# --- ожидаемый тест и всё, что он берёт из тестовых модулей -----------------------------------


@dataclass
class Module:
    rel: str  # путь от корня репозитория
    tree: ast.Module
    defs: dict[str, ast.stmt] = field(default_factory=dict)
    imports: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    fixtures: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = field(default_factory=dict)
    autouse: list[str] = field(default_factory=list)


def _fixture_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, bool] | None:
    for dec in node.decorator_list:
        func = dec.func if isinstance(dec, ast.Call) else dec
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "fixture":
            continue
        fixture_name, autouse = node.name, False
        if isinstance(dec, ast.Call):
            for kw in dec.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    fixture_name = str(kw.value.value)
                if kw.arg == "autouse" and isinstance(kw.value, ast.Constant):
                    autouse = bool(kw.value.value)
        return fixture_name, autouse
    return None


class Closure:
    """Форма теста и всего, что он берёт из пакета тестов, в одном состоянии дерева."""

    def __init__(self, src: Source, tests_root: str) -> None:
        self.src = src
        self.tests_root = tests_root
        self._modules: dict[str, Module | None] = {}
        self._tests: dict[str, dict[str, str] | None] = {}

    def rel(self, path: str) -> str:
        return posixpath.normpath(posixpath.join(self.tests_root, path))

    def module(self, rel: str) -> Module | None:
        if rel not in self._modules:
            self._modules[rel] = self._index(rel)
        return self._modules[rel]

    def _index(self, rel: str) -> Module | None:
        tree = self.src.tree(rel)
        if tree is None:
            return None
        mod = Module(rel, tree)
        package = posixpath.dirname(posixpath.relpath(rel, self.tests_root)).split("/")
        for stmt in tree.body:
            if isinstance(stmt, _DEFS):
                mod.defs[stmt.name] = stmt
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fixture = _fixture_decorator(stmt)
                    if fixture is not None:
                        mod.fixtures[fixture[0]] = stmt
                        if fixture[1]:
                            mod.autouse.append(fixture[0])
            elif isinstance(stmt, ast.ImportFrom):
                base = package[: len(package) - stmt.level + 1] if stmt.level else []
                dotted = ".".join([*base, *([stmt.module] if stmt.module else [])])
                for alias in stmt.names:
                    mod.imports[alias.asname or alias.name] = (dotted, alias.name)
            elif isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    local = alias.asname or alias.name.split(".")[0]
                    mod.imports[local] = (alias.name if alias.asname else local, None)
            else:
                for name in _assigned_names(stmt):
                    mod.defs[name] = stmt
        return mod

    def _module_of(self, dotted: str, package: str) -> Module | None:
        if dotted.split(".")[0] != package:
            return None  # код приложения и сторонние пакеты в тест не входят
        path = dotted.replace(".", "/")
        return self.module(self.rel(f"{path}.py")) or self.module(self.rel(f"{path}/__init__.py"))

    def _conftests(self, test_rel: str) -> list[Module]:
        """Модуль теста, затем ``conftest.py`` от его каталога до корня тестов."""
        out = [m for m in [self.module(test_rel)] if m is not None]
        directory = posixpath.dirname(test_rel)
        root = posixpath.normpath(self.tests_root)
        while True:
            conftest = self.module(posixpath.join(directory, "conftest.py"))
            if conftest is not None:
                out.append(conftest)
            if directory in (root, "", ".") or not directory.startswith(root.rstrip(".")):
                break
            directory = posixpath.dirname(directory)
        return out

    def of_test(self, test: str) -> dict[str, str] | None:
        """``{"файл::имя": форма}`` теста ``путь::имя[::метод][параметры]``; ``None`` — теста
        нет. Параметры не различаются: форма у всех параметров одна."""
        key = test.split("[")[0]
        if key not in self._tests:
            self._tests[key] = self._of_test(key)
        return self._tests[key]

    def _of_test(self, test: str) -> dict[str, str] | None:
        path, *names = test.split("::")
        test_rel = self.rel(path)
        mod = self.module(test_rel)
        if mod is None or not names:
            return None
        node = mod.defs.get(names[0])
        if not isinstance(node, _DEFS):
            return None
        package = path.split("/")[0]
        scope = self._conftests(test_rel)
        # Фикстуры по именам аргументов просят тест (у класса — его методы) и сами фикстуры;
        # аргументы помощников — обычные параметры.
        queue: list[tuple[Module, str, ast.AST, bool]] = [(mod, names[0], node, True)]
        if "pytestmark" in mod.defs:
            queue.append((mod, "pytestmark", mod.defs["pytestmark"], False))
        for holder in scope:
            queue.extend((holder, name, holder.fixtures[name], True) for name in holder.autouse)
        items: dict[str, str] = {}
        while queue:
            owner, name, item, requests = queue.pop()
            key = f"{owner.rel}::{name}"
            if key in items:
                continue
            items[key] = shape(item)
            for ref in self._references(item):
                queue.extend((*found, False) for found in self._resolve(owner, ref, package))
            for fixture in self._requested(item, arguments=requests):
                provider = next((m for m in scope if fixture in m.fixtures), None)
                if provider is not None:
                    queue.append((provider, fixture, provider.fixtures[fixture], True))
        return items

    @staticmethod
    def _references(node: ast.AST) -> list[tuple[str, str | None]]:
        refs: list[tuple[str, str | None]] = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                refs.append((sub.id, None))
            elif isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                refs.append((sub.value.id, sub.attr))
        return refs

    @staticmethod
    def _requested(node: ast.AST, *, arguments: bool) -> list[str]:
        """Фикстуры, которые просит узел: имена аргументов (``arguments``) и ``usefixtures``."""
        names: list[str] = []
        if arguments:
            functions = [node] if not isinstance(node, ast.ClassDef) else node.body
            for func in functions:
                if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = func.args
                    names += [a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "usefixtures"
            ):
                names += [str(a.value) for a in sub.args if isinstance(a, ast.Constant)]
        return names

    def _resolve(
        self, owner: Module, ref: tuple[str, str | None], package: str, depth: int = 0
    ) -> list[tuple[Module, str, ast.AST]]:
        name, attr = ref
        if depth > 8:
            return []
        if name in owner.defs:
            return [(owner, name, owner.defs[name])]
        if name not in owner.imports:
            return []
        dotted, imported = owner.imports[name]
        if imported is None:  # import tests.x as alias → alias.attr
            target = self._module_of(dotted, package)
            if target is None or attr is None:
                return []
            return self._resolve(target, (attr, None), package, depth + 1)
        target = self._module_of(dotted, package)
        if target is not None and (imported in target.defs or imported in target.imports):
            return self._resolve(target, (imported, None), package, depth + 1)
        submodule = self._module_of(f"{dotted}.{imported}", package)
        if submodule is not None and attr is not None:  # from tests.e2e import _reporting
            return self._resolve(submodule, (attr, None), package, depth + 1)
        return []


# --- посадки -------------------------------------------------------------------------------


@dataclass
class Planting:
    label: str
    file: str
    old: str
    expect: list[str]
    count: int = 1
    what: str = ""


def load_plantings(path: Path) -> list[Planting]:
    if path.suffix == ".py":
        spec = importlib.util.spec_from_file_location("_plantings_harness", path)
        if spec is None or spec.loader is None:
            raise InputError(f"{path}: модуль харнесса не загружается")
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        data = getattr(harness, "PLANTS", None)
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("plantings")
    if not isinstance(data, list):
        raise InputError(f"{path}: нет списка посадок (PLANTS или plantings)")
    out: list[Planting] = []
    for raw in data:
        if not isinstance(raw, dict) or not isinstance(raw.get("expect"), list):
            raise InputError(f"{path}: посадка {raw!r:.80}: нужен объект со списком expect")
        try:
            planting = Planting(
                label=str(raw["label"]),
                file=str(raw["file"]),
                old=str(raw["old"]),
                expect=[str(t) for t in raw["expect"]],
                count=int(raw.get("count", 1)),
                what=str(raw.get("what", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InputError(f"{path}: посадка {raw!r:.80}: {exc}") from exc
        if not planting.old or planting.count < 1:
            raise InputError(f"{path}: {planting.label}: пустой образец или count < 1")
        out.append(planting)
    labels = [p.label for p in out]
    if len(set(labels)) != len(labels):
        raise InputError(f"{path}: метки посадок повторяются")
    return out


def load_log(path: Path) -> set[str]:
    labels: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            labels.add(str(json.loads(line)["label"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise InputError(f"{path}:{number}: строка журнала без label: {exc}") from exc
    return labels


@dataclass
class Verdict:
    label: str
    what: str
    reasons: list[str]
    found: int
    expected: int

    @property
    def mismatch(self) -> bool:
        return self.found != self.expected


def code_reason(planting: Planting, base: Source, current: Source) -> str | None:
    now, before = current.read(planting.file), base.read(planting.file)
    if now is None:
        return f"файла {planting.file} нет в дереве"
    if before is None:
        return f"файла {planting.file} нет в снимке"
    if before.count(planting.old) != planting.count:
        return "образец новый или изменён"
    if not planting.file.endswith(".py"):
        return None if now == before else f"файл {planting.file} изменён (не Python — целиком)"
    tree_now, tree_before = current.tree(planting.file), base.tree(planting.file)
    if tree_now is None or tree_before is None:
        return None if now == before else f"файл {planting.file} изменён (не разбирается)"
    path = enclosing(tree_now, now, planting.old)
    node_now, node_before = find(tree_now, path), find(tree_before, path)
    name = ".".join(path)
    if node_before is None or node_now is None:
        return f"код {name} нет в снимке"
    return None if shape(node_now) == shape(node_before) else f"код {name}"


def test_reasons(planting: Planting, base: Closure, current: Closure) -> list[str]:
    reasons: list[str] = []
    for test in sorted({t.split("[")[0] for t in planting.expect}):
        name = test.split("::", 1)[-1]
        if not test.split("::")[0].endswith(".py"):
            rel = current.rel(test.split("::")[0])
            if current.src.read(rel) != base.src.read(rel):
                reasons.append(f"тест {name}: файл изменён")
            continue
        now, before = current.of_test(test), base.of_test(test)
        if now is None:
            reasons.append(f"тест {name}: нет в дереве")
        elif before is None:
            reasons.append(f"тест {name}: новый")
        else:
            groups = (
                ("изменено", [k for k in now if k in before and now[k] != before[k]]),
                ("добавлено", [k for k in now if k not in before]),
                ("убрано", [k for k in before if k not in now]),
            )
            parts = [
                f"{title} {', '.join(sorted(k.split('::')[-1] for k in keys))}"
                for title, keys in groups
                if keys
            ]
            if parts:
                reasons.append(f"тест {name}: {'; '.join(parts)}")
    return reasons


def compare(
    plantings: list[Planting],
    base: Source,
    current: Source,
    tests_root: str,
    measured: set[str] | None = None,
) -> list[Verdict]:
    base_closure, current_closure = Closure(base, tests_root), Closure(current, tests_root)
    verdicts: list[Verdict] = []
    for planting in plantings:
        text = current.read(planting.file)
        found = text.count(planting.old) if text is not None else 0
        reasons: list[str] = []
        if found != planting.count:
            reasons.append(f"образец в дереве {found} раз вместо {planting.count}")
        else:
            reason = code_reason(planting, base, current)
            if reason:
                reasons.append(reason)
        reasons += test_reasons(planting, base_closure, current_closure)
        if measured is not None and planting.label not in measured:
            reasons.append("не измерялась")
        verdicts.append(Verdict(planting.label, planting.what, reasons, found, planting.count))
    return verdicts


def render(verdicts: list[Verdict]) -> str:
    remeasure = [v for v in verdicts if v.reasons and not v.mismatch]
    dry = [v.label for v in verdicts if not v.reasons]
    mismatch = [v for v in verdicts if v.mismatch]
    lines = [f"перемерить: {len(remeasure)}"]
    width = max((len(v.label) for v in verdicts), default=0)
    lines += [f"  {v.label:{width}}  {'; '.join(v.reasons)}" for v in remeasure]
    lines.append(f"сухо: {len(dry)}" + (f" — {' '.join(dry)}" if dry else ""))
    lines.append(f"образец не совпал: {len(mismatch)}")
    lines += [f"  {v.label:{width}}  {'; '.join(v.reasons)}" for v in mismatch]
    return "\n".join(lines)


# --- снимок --------------------------------------------------------------------------------


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = _run_git(root, *args, env=env)
    if proc.returncode != 0:
        raise InputError(f"git {' '.join(args)}: {proc.stderr.decode('utf-8').strip()}")
    return proc.stdout.decode("utf-8").strip()


def snapshot(root: Path) -> str:
    """Дерево git рабочей копии с неотслеживаемыми файлами (кроме игнорируемых) — через
    временный индекс: настоящий индекс и рабочее дерево не меняются."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        _git(root, "read-tree", "--empty", env=env)
        _git(root, "add", "-A", env=env)
        return _git(root, "write-tree", env=env)


def base_source(root: Path, base: str) -> Source:
    if Path(base).is_dir():
        return Source(Path(base))
    proc = _run_git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{tree}}")
    if proc.returncode != 0:
        raise InputError(f"--base {base}: не каталог и не дерево git")
    return Source(root, proc.stdout.decode("utf-8").strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="корень репозитория")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("snapshot", help="дерево git рабочей копии — снимок перед роастом")
    cmp_parser = sub.add_parser("compare", help="какие посадки перемерить после правок")
    cmp_parser.add_argument("--base", required=True, help="снимок: дерево git или каталог")
    cmp_parser.add_argument("--plantings", required=True, type=Path, help="JSON или харнесс .py")
    cmp_parser.add_argument(
        "--tests-root", default=DEFAULT_TESTS_ROOT, help="каталог, от которого пути тестов"
    )
    cmp_parser.add_argument("--log", type=Path, help="журнал прогонов: JSON по строке с label")
    cmp_parser.add_argument("--json", action="store_true", help="машинный вывод")
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    try:
        if args.command == "snapshot":
            print(snapshot(root))
            return 0
        plantings = load_plantings(args.plantings)
        measured = load_log(args.log) if args.log else None
        verdicts = compare(
            plantings, base_source(root, args.base), Source(root), args.tests_root, measured
        )
    except (InputError, OSError, ValueError) as exc:
        print(f"plantings_affected: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "label": v.label,
                        "what": v.what,
                        "reasons": v.reasons,
                        "found": v.found,
                        "expected": v.expected,
                    }
                    for v in verdicts
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(render(verdicts))
    return 1 if any(v.mismatch for v in verdicts) else 0


if __name__ == "__main__":
    sys.exit(main())
