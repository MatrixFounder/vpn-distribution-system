#!/usr/bin/env python3
"""Отбор посадок для перемера между раундами роаста (правило 3 «Цикла задачи», CLAUDE.md; WI-40).

Между раундами перемеряются только посадки, у которых изменился образец, код вокруг образца,
ожидаемые тесты, входы теста или само описание посадки; остальные проходят сухую проверку —
образец совпадает с деревом столько раз, сколько ждёт посадка. Вердикт посадки верен для дерева,
на котором она мерилась, поэтому база каждой посадки — дерево её прогона из журнала харнесса
(решения заказчика 2026-11-21, WI-40).

Журнал — JSON по строке на прогон: ``label``, ``verdict`` (``RED`` — измерена; любое другое
значение — нет), ``tree`` (``snapshot()`` рабочей копии, снятый ДО копии, с которой мерился
прогон) и ``spec`` (``spec_hash()`` описания посадки); прочие поля не читаются. Решает последняя
строка метки. Перемерить, если: строки нет; её вердикт не ``RED``; нет ``tree`` или дерева нет в
репозитории; ``spec`` нет или он не совпадает с описанием посадки сейчас; иначе — по сравнению
базы с рабочей копией:

- образец: в базе его не столько раз, сколько ждёт посадка, или его вхождения стоят в других
  блоках («переехал» — правкой комментария или переносов замена бьёт по другому коду);
- код вокруг образца — для каждого вхождения наименьшая функция или класс (одноимённые
  определения различаются по порядку), либо присваивание модуля, иначе модуль целиком; AST без
  докстрингов, поэтому комментарии, переносы и докстринги перемера не вызывают; файл не на Python
  или неразбираемый — целиком, по байтам;
- ожидаемый тест — функция теста с декораторами, все привязки её имени (``t = mark(t)``); класс
  теста — целиком, с базовыми классами (в том числе ``helpers.Base``), вложенные классы по пути
  ``A::B::t``; ``expect`` без ``::`` — модуль целиком. По цепочке входит всё, что тест берёт:
  привязки имён модуля (все: ``+=``, импорт вместе с другой привязкой; модуль с ``if``/``try`` —
  целиком, см. запасной путь), привязки
  импортов (и из кода приложения — сама привязка, без кода), определения модулей пакета тестов
  (импорты модуля и внутри функции — те затеняют имена модуля; относительные любого уровня;
  ``import a.b as h`` с ``h.f``; ``import a.b`` с ``a.b.f``; реэкспорт; подмодуль;
  ``from m import *``; модуль, переданный объектом, — целиком); фикстуры по имени аргумента
  (и keyword-only), ``usefixtures`` и ``getfixturevalue("имя")`` — модуль теста, затем
  ``conftest.py`` от каталога теста до корня тестов, затем ``pytest_plugins`` (вложенные тоже, и
  вне пакета тестов); фикстура регистрируется по имени из декоратора (``name=``), в том числе
  импортированная в поставщик или пришедшая через ``*``; фикстура, переопределяющая одноимённую,
  тянет следующую; автоматические фикстуры (и импортированные), хуки ``pytest_*``,
  ``collect_ignore``, ``collect_ignore_glob``, ``pytestmark``; настройки pytest корня тестов
  (``[tool.pytest]``, ``[tool.pytest.ini_options]``, ``pytest.toml``, ``.pytest.toml``,
  ``pytest.ini``, ``.pytest.ini``, ``[tool:pytest]``, ``[pytest]``); файлы не на Python в каталоге
  теста и подкаталогах (по байтам). Код приложения в тест не входит — его правка вокруг образца
  ловится первым пунктом. Тест не на Python — его файл целиком.
- осторожный запасной путь: модуль цепочки — поставщики фикстур, ``__init__.py`` пакетов, модули,
  к которым ведёт цепочка, и всё, что они импортируют, — с оператором, который скрипт не
  разбирает, входит в тест целиком: вызов, ``if``/``try``/``with``/цикл (кроме ``if __name__ ==
  "__main__"`` и ``if TYPE_CHECKING`` без ``else``), присваивание в индекс или атрибут,
  ``_ = вызов()``, декоратор вне известного безопасного набора, класс с побочным кодом в теле или
  ``__init_subclass__`` — код при импорте (``pytest.skip`` модуля, окружение conftest, реестр)
  меняет тесты без ссылки на имя;
- входы теста — файлы и каталоги ``inputs`` посадки (пути от корня репозитория): тест читает
  данные вне своего каталога (``contracts/``, ``deploy/``); по идентификатору блоба git.

Не видны (объявленное ограничение, правило 3 в CLAUDE.md): реестр, который наполняют
``__init_subclass__`` или метакласс подклассов из другого модуля; фикстура, созданная
присваиванием (``stand = pytest.fixture(_impl)``); плагин из ``-p`` в ``addopts`` (сами настройки
сравниваются); ``importlib`` и строковые имена модулей; код, запущенный подпроцессом; файлы,
которые тест читает вне своего каталога и которых нет в ``inputs``. Посадку с таким тестом
строитель отмечает вручную.

Образец, который в дереве встречается не столько раз, сколько ждёт посадка, — ошибка сухой
проверки: код возврата 1. Негодный вход — код 2.

Команды::

    plantings_affected.py snapshot                      # дерево git рабочей копии
    plantings_affected.py spec --plantings <посадки>     # метка и spec каждой посадки
    plantings_affected.py compare --plantings <посадки> --log <журнал> [--tests-root DIR]

Снимок — дерево git рабочей копии: индекс плюс правки и неотслеживаемые файлы, кроме
игнорируемых (отслеживаемые игнорируемые остаются); индекс и рабочее дерево не меняются; объект
без ссылки живёт до сборки мусора git (недели). Харнесс на Python вызывает ``snapshot(root)`` и
``spec_hash(посадка)`` напрямую. Посадки — JSON (список объектов или ``{"plantings": [...]}``)
либо модуль харнесса со списком ``PLANTS`` (его запуск — под ``if __name__ == "__main__"``). Поля
посадки: ``label``, ``file`` (путь от корня репозитория), ``old`` (образец), ``count`` (сколько
раз, по умолчанию 1), ``expect`` (``путь[::имя[::…]][параметры]`` от ``--tests-root``),
``inputs`` (необязательно), ``what``; ``spec`` — отпечаток всего описания, включая замену ``new``.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import importlib.util
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_TESTS_ROOT = "control-plane"
GIT = shutil.which("git") or "git"
WHOLE_MODULE = "<модуль>"
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_COMPOUND = (
    ast.If, ast.Try, ast.TryStar, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While,
)  # fmt: skip
SPECIAL_NAMES = ("collect_ignore", "collect_ignore_glob")
PYTEST_CONFIGS: tuple[tuple[str, tuple[str, ...] | None], ...] = (
    ("pyproject.toml", ("[tool.pytest]", "[tool.pytest.ini_options]")),
    ("pytest.toml", None),
    (".pytest.toml", None),
    ("pytest.ini", None),
    (".pytest.ini", None),
    ("setup.cfg", ("[tool:pytest]",)),
    ("tox.ini", ("[pytest]",)),
)


class InputError(Exception):
    """Негодный вход: посадки, журнал, пути."""


def _run_git(
    root: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    # Аргументы — фиксированный список без оболочки; деревья приходят из журнала харнесса.
    return subprocess.run(  # noqa: S603
        [GIT, "-C", str(root), "--literal-pathspecs", *args],
        capture_output=True,
        env=env,
        check=False,
    )


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> bytes:
    proc = _run_git(root, *args, env=env)
    if proc.returncode != 0:
        raise InputError(f"git {' '.join(args)}: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def snapshot(root: Path) -> str:
    """Дерево git рабочей копии — через пустой временный индекс: каждый файл хэшируется по
    содержимому (индекс без отметок о размере и времени не примет правку того же размера в ту же
    секунду за неизменный файл); правки, неотслеживаемые файлы (кроме игнорируемых) и
    отслеживаемые игнорируемые; настоящий индекс и рабочее дерево не меняются."""
    tracked_ignored = [
        name.decode("utf-8")
        for name in _git(
            root, "ls-files", "-z", "--cached", "--ignored", "--exclude-standard"
        ).split(b"\0")
        if name
    ]
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        _git(root, "read-tree", "--empty", env=env)
        _git(root, "add", "-A", env=env)
        present = [name for name in tracked_ignored if (root / name).is_file()]
        if present:
            _git(root, "add", "-f", "--", *present, env=env)
        return _git(root, "write-tree", env=env).decode("utf-8").strip()


def spec_hash(raw: dict[str, Any]) -> str:
    """Отпечаток описания посадки: всё описание, включая замену ``new``."""
    text = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class _Batch:
    """Один процесс ``git cat-file --batch`` на дерево: файлов базы — сотни."""

    def __init__(self, root: Path) -> None:
        # Аргументы — фиксированный список без оболочки.
        self.proc = subprocess.Popen(  # noqa: S603
            [GIT, "-C", str(root), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def blob(self, spec: str) -> bytes | None:
        stdin, stdout = self.proc.stdin, self.proc.stdout
        if stdin is None or stdout is None or "\n" in spec:
            return None
        stdin.write(spec.encode("utf-8") + b"\n")
        stdin.flush()
        header = stdout.readline().rstrip(b"\n")
        if header.endswith(b" missing") or header.endswith(b" ambiguous"):
            return None
        parts = header.split(b" ")
        if len(parts) != 3 or not parts[2].isdigit():
            return None
        data = stdout.read(int(parts[2]))
        stdout.read(1)
        return data if parts[1] == b"blob" else None

    def close(self) -> None:
        if self.proc.poll() is None:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
            self.proc.wait()
        if self.proc.stdout is not None:
            self.proc.stdout.close()

    def __del__(self) -> None:
        self.close()


class Source:
    """Файлы одного состояния: дерево git (``treeish``) или рабочая копия (``None``)."""

    def __init__(self, root: Path, treeish: str | None = None) -> None:
        self.root = root
        self.treeish = treeish
        self._batch: _Batch | None = None
        self._bytes: dict[str, bytes | None] = {}
        self._trees: dict[str, ast.Module | None] = {}
        self._files: dict[str, list[str]] = {}
        self._digests: dict[str, dict[str, str]] = {}

    def read_bytes(self, rel: str) -> bytes | None:
        if rel not in self._bytes:
            if self.treeish is None:
                path = self.root / rel
                self._bytes[rel] = path.read_bytes() if path.is_file() else None
            else:
                if self._batch is None:
                    self._batch = _Batch(self.root)
                self._bytes[rel] = self._batch.blob(f"{self.treeish}:{rel}")
        return self._bytes[rel]

    def close(self) -> None:
        if self._batch is not None:
            self._batch.close()
            self._batch = None

    def read(self, rel: str) -> str | None:
        data = self.read_bytes(rel)
        return None if data is None else data.decode("utf-8", "replace")

    def tree(self, rel: str) -> ast.Module | None:
        """Разобранный файл (``None`` — нет файла или он не разбирается); узлы не изменять."""
        if rel not in self._trees:
            self._trees[rel] = _parse(self.read(rel))
        return self._trees[rel]

    def files(self, path: str) -> list[str]:
        """Файлы под путём (каталог или файл) — тот же набор, что берёт ``snapshot``."""
        spec = path or "."
        if spec not in self._files:
            if self.treeish is None:
                args = ["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", spec]
                names = [n for n in _git(self.root, *args).decode("utf-8").split("\0") if n]
                names = [n for n in names if (self.root / n).is_file()]
            else:
                args = ["ls-tree", "-r", "-z", "--name-only", self.treeish, "--", spec]
                names = [n for n in _git(self.root, *args).decode("utf-8").split("\0") if n]
            self._files[spec] = sorted(set(names))
        return self._files[spec]

    def digests(self, path: str) -> dict[str, str]:
        """Идентификатор блоба git каждого файла под путём: в дереве — из дерева, в рабочей копии
        — ``git hash-object`` с фильтрами пути (eol, clean), то есть так, как файл лёг бы в
        снимок; поэтому CRLF рабочей копии и LF дерева не расходятся."""
        if path not in self._digests:
            self._digests[path] = self._digest(path)
        return self._digests[path]

    def _digest(self, path: str) -> dict[str, str]:
        names = self.files(path)
        if not names:
            return {}
        if self.treeish is not None:
            out = _git(self.root, "ls-tree", "-r", "-z", self.treeish, "--", path or ".")
            ids = {}
            for entry in out.decode("utf-8").split("\0"):
                if "\t" in entry:
                    meta, name = entry.split("\t", 1)
                    ids[name] = meta.split()[2]
            return {n: ids[n] for n in names if n in ids}
        plain = [n for n in names if "\n" not in n]
        result: dict[str, str] = {}
        if plain:
            # Аргументы — фиксированный список без оболочки; пути — с stdin.
            proc = subprocess.run(  # noqa: S603
                [GIT, "-C", str(self.root), "hash-object", "--stdin-paths"],
                input="\n".join(plain).encode("utf-8"),
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                raise InputError(f"git hash-object: {proc.stderr.decode('utf-8', 'replace')}")
            result = dict(zip(plain, proc.stdout.decode("utf-8").split(), strict=True))
        for name in names:
            if name not in result:
                data = self.read_bytes(name) or b""
                result[name] = hashlib.sha1(  # noqa: S324 — так git называет блоб
                    b"blob %d\0" % len(data) + data
                ).hexdigest()
        return result


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

Position = tuple[int, int]  # строка (с 1) и колонка в байтах UTF-8, как у ast


def _position(text: str, index: int) -> Position:
    line_start = text.rfind("\n", 0, index) + 1
    return text.count("\n", 0, index) + 1, len(text[line_start:index].encode("utf-8"))


def occurrences(text: str, old: str) -> list[tuple[Position, Position]]:
    """Начало и конец каждого вхождения — без перекрытий, как ``str.count``/``replace``."""
    spans: list[tuple[Position, Position]] = []
    start = 0
    while (index := text.find(old, start)) != -1:
        body = old.rstrip("\n") or old
        spans.append((_position(text, index), _position(text, index + len(body))))
        start = index + len(old)
    return spans


def _span(node: ast.stmt, lines: list[bytes]) -> tuple[Position, Position]:
    """Начало (с декораторами) и конец узла; конец — до конца строки, если после узла в ней
    только пробелы и комментарий (комментарий в узел AST не входит)."""
    begins = [(node.lineno, node.col_offset)]
    # Колонка декоратора в AST — после «@»; декоратор занимает свою строку целиком.
    begins += [(d.lineno, 0) for d in getattr(node, "decorator_list", [])]
    end_line, end_col = node.end_lineno or node.lineno, node.end_col_offset or 0
    rest = lines[end_line - 1][end_col:].decode("utf-8", "ignore").strip()
    if not rest or rest.startswith("#"):
        end_col = sys.maxsize
    return min(begins), (end_line, end_col)


def _flatten(body: list[ast.stmt]) -> list[ast.stmt]:
    """Операторы тела по порядку, с телами ``if``/``try``/``with``/циклов, без входа в
    функции и классы."""
    out: list[ast.stmt] = []
    for stmt in body:
        out.append(stmt)
        if isinstance(stmt, _COMPOUND):
            for part in ("body", "orelse", "finalbody"):
                out += _flatten(getattr(stmt, part, []) or [])
            for handler in getattr(stmt, "handlers", []) or []:
                out += _flatten(handler.body)
    return out


def _bound_names(stmt: ast.stmt) -> list[str]:
    """Имена, которые оператор привязывает в своей области (не импорты)."""
    if isinstance(stmt, _DEFS):
        return [stmt.name]
    targets: list[ast.expr] = []
    if isinstance(stmt, ast.Assign):
        targets = list(stmt.targets)
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    return [n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)]


def _defs_named(body: list[ast.stmt], name: str) -> list[ast.stmt]:
    return [s for s in _flatten(body) if isinstance(s, _DEFS) and s.name == name]


def _assigns_named(body: list[ast.stmt], name: str) -> list[ast.stmt]:
    return [s for s in _flatten(body) if not isinstance(s, _DEFS) and name in _bound_names(s)]


def enclosing(tree: ast.Module, text: str, start: Position, end: Position) -> list[str]:
    """Путь к блоку вхождения: ``["Box#0", "value#1"]`` — наименьшая функция или класс (номер —
    среди одноимённых определений области), ``["=QUERY#0"]`` — присваивание модуля,
    ``[WHOLE_MODULE]`` — иначе."""
    lines = [line.encode("utf-8") for line in text.split("\n")]

    def inside(node: ast.stmt) -> bool:
        begin, finish = _span(node, lines)
        return begin <= start and end <= finish

    path: list[str] = []
    body = tree.body
    while True:
        inner = next((s for s in _flatten(body) if isinstance(s, _DEFS) and inside(s)), None)
        if inner is None:
            break
        path.append(f"{inner.name}#{_defs_named(body, inner.name).index(inner)}")
        body = inner.body
    if path:
        return path
    for stmt in _flatten(tree.body):
        names = _bound_names(stmt)
        if names and not isinstance(stmt, _DEFS) and not isinstance(stmt, _COMPOUND):
            if inside(stmt):
                return [f"={names[0]}#{_assigns_named(tree.body, names[0]).index(stmt)}"]
    return [WHOLE_MODULE]


def find(tree: ast.Module, path: list[str]) -> ast.AST | None:
    if path == [WHOLE_MODULE]:
        return tree
    if len(path) == 1 and path[0].startswith("="):
        name, index = path[0][1:].rsplit("#", 1)
        assigns = _assigns_named(tree.body, name)
        return assigns[int(index)] if int(index) < len(assigns) else None
    body: list[ast.stmt] = tree.body
    node: ast.AST | None = None
    for element in path:
        name, index = element.rsplit("#", 1)
        defs = _defs_named(body, name)
        if int(index) >= len(defs):
            return None
        node = defs[int(index)]
        body = defs[int(index)].body  # type: ignore[attr-defined]
    return node


def block_name(path: list[str]) -> str:
    parts = []
    for element in path:
        name, _, index = element.lstrip("=").rpartition("#")
        parts.append(
            element if element == WHOLE_MODULE else name + ("" if index == "0" else f"#{index}")
        )
    return ".".join(parts)


def blocks(tree: ast.Module, text: str, old: str) -> list[list[str]]:
    """Пути блоков всех вхождений образца по порядку."""
    return [enclosing(tree, text, start, end) for start, end in occurrences(text, old)]


# --- ожидаемый тест и всё, что он берёт ------------------------------------------------------


def _is_guard(stmt: ast.If) -> bool:
    """``if __name__ == "__main__"`` (не исполняется под pytest) и ``if TYPE_CHECKING`` — без
    ветки ``else``: она исполняется при импорте."""
    if stmt.orelse:
        return False
    test = stmt.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


SAFE_DECORATORS = frozenset(
    {
        "fixture", "parametrize", "property", "setter", "getter", "deleter", "staticmethod",
        "classmethod", "cached_property", "cache", "lru_cache", "wraps", "dataclass",
        "abstractmethod", "override", "final", "total_ordering", "contextmanager",
        "asynccontextmanager",
    }
)  # fmt: skip


def _safe_decorator(dec: ast.expr) -> bool:
    """Декоратор без побочного действия при импорте: метки pytest и известные обёртки;
    любой другой (реестр, регистрация) — нет."""
    func = dec.func if isinstance(dec, ast.Call) else dec
    chain: list[str] = []
    while isinstance(func, ast.Attribute):
        chain.insert(0, func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        chain.insert(0, func.id)
    return bool(chain) and ("mark" in chain or chain[-1] in SAFE_DECORATORS)


def _plain_target(target: ast.expr) -> bool:
    if isinstance(target, ast.Name):
        return target.id != "_"
    if isinstance(target, (ast.Tuple, ast.List)):
        return all(_plain_target(t) for t in target.elts)
    return False


def _modeled(stmt: ast.stmt) -> bool:
    """Оператор, который скрипт разбирает по именам: исполнение при импорте только привязывает
    имя. Присваивание в индекс или атрибут, ``_ = вызов()``, декоратор-реестр, тело класса с
    побочным кодом или ``__init_subclass__``, ``if`` кроме стражей — нет."""
    if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.Pass)):
        return True
    if isinstance(stmt, ast.Assign):
        return all(_plain_target(t) for t in stmt.targets)
    if isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        return _plain_target(stmt.target)
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return isinstance(stmt.value.value, str)
    if isinstance(stmt, _FUNCS):
        return all(_safe_decorator(d) for d in stmt.decorator_list)
    if isinstance(stmt, ast.ClassDef):
        return (
            all(_safe_decorator(d) for d in stmt.decorator_list)
            and not stmt.keywords
            and not any(isinstance(s, _FUNCS) and s.name == "__init_subclass__" for s in stmt.body)
            and all(_modeled(s) for s in stmt.body)
        )
    return isinstance(stmt, ast.If) and _is_guard(stmt)


@dataclass(eq=False)
class Module:
    rel: str  # путь от корня репозитория
    tree: ast.Module
    root: str  # корень импорта: первый каталог вверх без __init__.py
    package: list[str]  # пакет модуля от корня импорта
    bindings: dict[str, list[ast.stmt]] = field(default_factory=dict)
    imports: dict[str, list[tuple[str, str | None]]] = field(default_factory=dict)
    stars: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)  # полные имена импортированных модулей
    fixtures: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = field(default_factory=dict)
    autouse: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    unmodeled: bool = False


def _fixture_decorator(node: ast.AST) -> tuple[str, bool] | None:
    if not isinstance(node, _FUNCS):
        return None
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


def _imports_of(
    nodes: Sequence[ast.AST], package: list[str]
) -> tuple[dict[str, list[tuple[str, str | None]]], list[str]]:
    """Привязки импортов: имя → все его цели (модуль, имя в модуле) или (модуль, None) для
    ``import`` — имя из ``try``/``except`` или ``if``/``else`` бывает связано с разными модулями;
    и модули ``from m import *``."""
    out: dict[str, list[tuple[str, str | None]]] = {}
    stars: list[str] = []
    for stmt in nodes:
        if isinstance(stmt, ast.ImportFrom):
            base = package[: len(package) - stmt.level + 1] if stmt.level else []
            dotted = ".".join([*base, *([stmt.module] if stmt.module else [])])
            for alias in stmt.names:
                if alias.name == "*":
                    stars.append(dotted)
                else:
                    out.setdefault(alias.asname or alias.name, []).append((dotted, alias.name))
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.asname:
                    out.setdefault(alias.asname, []).append((alias.name, None))
                else:
                    top = alias.name.split(".")[0]
                    out.setdefault(top, []).append((top, None))
    return out, stars


@dataclass
class Item:
    owner: Module
    name: str
    nodes: list[ast.AST]
    requests: bool  # просит ли фикстуры по именам аргументов
    text: str | None = None  # форма без узлов (привязка импорта, файл, настройки)


class Closure:
    """Форма теста и всего, что он берёт, в одном состоянии дерева."""

    def __init__(self, src: Source, tests_root: str) -> None:
        self.src = src
        self.tests_root = posixpath.normpath(tests_root)
        self._modules: dict[str, Module | None] = {}
        self._tests: dict[str, dict[str, str] | None] = {}
        self._imported: dict[str, dict[str, Item]] = {}

    def rel(self, path: str) -> str:
        return posixpath.normpath(posixpath.join(self.tests_root, path))

    def _import_root(self, rel: str) -> tuple[str, list[str]]:
        """Корень импорта и пакет модуля: вверх от его каталога, пока есть ``__init__.py`` (как
        вставляет путь pytest)."""
        directory = posixpath.dirname(rel)
        package: list[str] = []
        while (
            directory and self.src.read_bytes(posixpath.join(directory, "__init__.py")) is not None
        ):
            package.insert(0, posixpath.basename(directory))
            directory = posixpath.dirname(directory)
        return directory, package

    def module(self, rel: str) -> Module | None:
        if rel not in self._modules:
            self._modules[rel] = self._index(rel)
        return self._modules[rel]

    def _index(self, rel: str) -> Module | None:
        tree = self.src.tree(rel)
        if tree is None:
            return None
        root, package = self._import_root(rel)
        mod = Module(rel, tree, root, package)
        # Только верхний уровень: модуль с if/try и прочим кодом при импорте уходит в запасной
        # путь целиком, а тела стражей (TYPE_CHECKING, __main__) при pytest не исполняются.
        flat = list(tree.body)
        mod.imports, mod.stars = _imports_of(flat, package)
        for stmt in flat:  # импорт исполняет модуль, даже если имя не используется
            if isinstance(stmt, ast.Import):
                mod.modules += [alias.name for alias in stmt.names]
            elif isinstance(stmt, ast.ImportFrom):
                base = package[: len(package) - stmt.level + 1] if stmt.level else []
                dotted = ".".join([*base, *([stmt.module] if stmt.module else [])])
                mod.modules += [
                    dotted,
                    *(f"{dotted}.{a.name}" for a in stmt.names if a.name != "*"),
                ]
        mod.unmodeled = not all(_modeled(s) for s in tree.body)
        for stmt in flat:
            for name in _bound_names(stmt):
                mod.bindings.setdefault(name, []).append(stmt)
            fixture = _fixture_decorator(stmt)
            if fixture is not None:
                mod.fixtures[fixture[0]] = stmt  # type: ignore[assignment]
                if fixture[1]:
                    mod.autouse.append(fixture[0])
            elif isinstance(stmt, _FUNCS) and stmt.name.startswith("pytest_"):
                mod.hooks.append(stmt.name)
            plugin_value = getattr(stmt, "value", None)
            if "pytest_plugins" in _bound_names(stmt) and isinstance(plugin_value, ast.expr):
                values = (
                    plugin_value.elts
                    if isinstance(plugin_value, (ast.List, ast.Tuple))
                    else [plugin_value]
                )
                mod.plugins += [str(v.value) for v in values if isinstance(v, ast.Constant)]
        return mod

    def _module_of(self, dotted: str, owner: Module, package: frozenset[str]) -> Module | None:
        """Модуль тестового кода по имени: верхний пакет — один из пакетов теста (``package``),
        путь — от корня импорта ``owner`` (модуль вне пакета — от корня тестов); у модуля вне
        пакета ещё и соседний модуль его каталога. Код приложения и сторонние пакеты — ``None``."""
        if not dotted:
            return None
        path = dotted.replace(".", "/")
        roots: list[str] = []
        if dotted.split(".")[0] in package:
            roots.append(owner.root if owner.package else self.tests_root)
        if not owner.package and owner.root and "." not in dotted:
            roots.append(owner.root)
        for root in dict.fromkeys(roots):
            base = posixpath.join(root, path) if root not in ("", ".") else path
            found = self.module(f"{base}.py") or self.module(f"{base}/__init__.py")
            if found is not None:
                return found
        return None

    def _scope(self, test: Module, package: frozenset[str]) -> list[Module]:
        """Поставщики фикстур по порядку поиска: модуль теста, ``conftest.py`` от его каталога
        до корня тестов, модули ``pytest_plugins`` (вложенные тоже) и ``*``-импортов."""
        out = [test]
        directory = posixpath.dirname(test.rel)
        while True:
            conftest = self.module(
                posixpath.join(directory, "conftest.py") if directory else "conftest.py"
            )
            if conftest is not None:
                out.append(conftest)
            if directory in (self.tests_root, "", ".") or not directory.startswith(
                self.tests_root.rstrip(".")
            ):
                break
            directory = posixpath.dirname(directory)
        seen = list(out)
        index = 0
        while index < len(seen):
            holder = seen[index]
            index += 1
            extra: list[Module | None] = []
            for dotted in holder.plugins:
                extra.append(
                    self._module_of(dotted, holder, package)
                    or self.module(self.rel(dotted.replace(".", "/") + ".py"))
                    or self.module(dotted.replace(".", "/") + ".py")
                )
            extra += [self._module_of(dotted, holder, package) for dotted in holder.stars]
            for found in extra:
                if found is not None and found not in seen:
                    seen.append(found)
        return seen

    def of_test(self, test: str) -> dict[str, str] | None:
        """``{ключ: форма}`` теста ``путь[::имя[::…]][параметры]``; ``None`` — теста нет.
        Параметры не различаются: форма у всех параметров одна."""
        key = test.split("[")[0]
        if key not in self._tests:
            self._tests[key] = self._of_test(key)
        return self._tests[key]

    def _of_test(self, test: str) -> dict[str, str] | None:
        path, *names = test.split("::")
        test_rel = self.rel(path)
        mod = self.module(test_rel)
        if mod is None:
            return None
        if mod.package:
            package = frozenset({mod.package[0]})
        else:  # каталог теста без __init__.py: тестовый код — пакеты его пути
            parts = posixpath.relpath(posixpath.dirname(test_rel), self.tests_root).split("/")
            package = frozenset(part for part in parts if part not in ("", ".", ".."))
        if names:
            nodes: list[ast.AST] = list(mod.bindings.get(names[0], []))
            if not any(isinstance(n, _DEFS) for n in nodes):
                return None
            cls = next((n for n in reversed(nodes) if isinstance(n, ast.ClassDef)), None)
            if len(names) > 1 and (cls is None or not self._has(mod, cls, names[1:], package)):
                return None
            first = Item(mod, names[0], nodes, True)
        else:
            first = Item(mod, WHOLE_MODULE, [mod.tree], True)
        scope = self._scope(mod, package)
        queue: list[Item] = [first]
        for name in ("pytestmark",):
            if name in mod.bindings:
                queue.append(Item(mod, name, list(mod.bindings[name]), False))
        for holder in scope:
            queue += [Item(holder, n, [holder.fixtures[n]], True) for n in holder.autouse]
            queue += [
                i for i in self._imported_fixtures(holder, package).values() if self._autouse(i)
            ]
            queue += [Item(holder, n, list(holder.bindings[n]), False) for n in holder.hooks]
            queue += [
                Item(holder, n, list(holder.bindings[n]), False)
                for n in SPECIAL_NAMES
                if n in holder.bindings
            ]
        queue += self._config_items(posixpath.dirname(test_rel))
        queue += self._data_items(posixpath.dirname(test_rel))
        # Модули цепочки — для запасного пути: поставщики, __init__.py пакетов, модули, к которым
        # ведёт цепочка, и всё, что они импортируют (импорт исполняет код модуля).
        chain: dict[str, Module] = {m.rel: m for m in scope}
        items: dict[str, str] = {}
        asked: set[str] = set()
        whole: set[str] = set()
        while True:
            while queue:
                item = queue.pop()
                key = f"{item.owner.rel}::{item.name}"
                if key not in items:
                    items[key] = (
                        item.text if item.text is not None else "\n".join(map(shape, item.nodes))
                    )
                    if item.owner.rel.endswith(".py"):
                        chain.setdefault(item.owner.rel, item.owner)
                    for part in item.nodes:
                        local, _ = _imports_of(
                            [
                                s
                                for s in ast.walk(part)
                                if isinstance(s, (ast.Import, ast.ImportFrom))
                            ],
                            item.owner.package,
                        )
                        for names_chain in self._chains(part):
                            queue += self._resolve(item.owner, names_chain, package, local)
                        own = isinstance(part, (ast.ClassDef, ast.Module))
                        for fixture in self._requested(part, arguments=own):
                            provider = self._provider(scope, fixture, item, package)
                            if provider is not None:
                                queue.append(provider)
                if item.requests and key not in asked:  # фикстура, встреченная и как ссылка
                    asked.add(key)
                    for part in item.nodes:
                        for fixture in self._requested(part, arguments=True):
                            provider = self._provider(scope, fixture, item, package)
                            if provider is not None:
                                queue.append(provider)
            self._grow(chain, package)
            pending = [m for m in chain.values() if m.unmodeled and m.rel not in whole]
            if not pending:
                return items
            for module in pending:
                whole.add(module.rel)
                queue.append(Item(module, WHOLE_MODULE, [module.tree], False))

    def _grow(self, chain: dict[str, Module], package: frozenset[str]) -> None:
        """Дополнить модули цепочки до неподвижной точки: импорты модуля уровня модуля (и
        ``*``), подмодули из ``from pkg import sub``, ``__init__.py`` пакетов."""
        grown = True
        while grown:
            grown = False
            for module in list(chain.values()):
                found: list[Module | None] = [
                    self._module_of(dotted, module, package)
                    for dotted in [*module.modules, *module.stars]
                ]
                for depth in range(1, len(module.package) + 1):
                    found.append(
                        self.module(
                            posixpath.join(module.root, *module.package[:depth], "__init__.py")
                        )
                    )
                for other in found:
                    if other is not None and other.rel not in chain:
                        chain[other.rel] = other
                        grown = True

    def _has(
        self, mod: Module, cls: ast.ClassDef, names: list[str], package: frozenset[str]
    ) -> bool:
        """Есть ли в классе (с базовыми классами и вложенными классами) путь ``names``."""
        name, rest = names[0], names[1:]
        for stmt in cls.body:
            if isinstance(stmt, _DEFS) and stmt.name == name:
                if not rest:
                    return True
                if isinstance(stmt, ast.ClassDef) and self._has(mod, stmt, rest, package):
                    return True
        for base in cls.bases:
            for chain in self._chains(base)[:1]:
                for found in self._resolve(mod, chain, package, {}):
                    for node in found.nodes:
                        if isinstance(node, ast.ClassDef) and self._has(
                            found.owner, node, names, package
                        ):
                            return True
        return False

    def _config_items(self, directory: str) -> list[Item]:
        """Настройки pytest всех уровней от каталога теста до корня репозитория: pytest берёт
        ближайший файл, а надмножество не пропустит ни одного."""
        out: list[Item] = []
        holder = Module("<pytest>", ast.Module([], []), "", [])
        levels = [directory]
        while levels[-1] not in ("", "."):
            levels.append(posixpath.dirname(levels[-1]))
        for level in dict.fromkeys(levels):
            for name, sections in PYTEST_CONFIGS:
                rel = posixpath.join(level, name) if level not in ("", ".") else name
                text = self.src.read(rel)
                if text is None:
                    continue
                if sections is not None:
                    lines, inside = [], False
                    for line in text.splitlines():
                        header = line.split("#", 1)[0].strip()
                        if header.startswith("["):
                            inside = header in sections
                            if inside:
                                lines.append(line)
                            continue
                        if inside:
                            lines.append(line)
                    text = "\n".join(lines)
                out.append(Item(holder, rel, [], False, text=text))
        return out

    def _data_items(self, directory: str) -> list[Item]:
        holder = Module(directory, ast.Module([], []), "", [])
        out = []
        for rel, digest in self.src.digests(directory).items():
            if rel.endswith((".py", ".pyc")):
                continue
            out.append(
                Item(holder, posixpath.relpath(rel, directory or "."), [], False, text=digest)
            )
        return out

    @staticmethod
    def _chains(node: ast.AST) -> list[list[str]]:
        """Имена и цепочки атрибутов от имени (``tests.x.h`` → ``["tests", "x", "h"]``)."""
        chains: list[list[str]] = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                chains.append([sub.id])
            elif isinstance(sub, ast.Attribute):
                parts: list[str] = []
                cur: ast.expr = sub
                while isinstance(cur, ast.Attribute):
                    parts.insert(0, cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    chains.append([cur.id, *parts])
        return chains

    @staticmethod
    def _requested(node: ast.AST, *, arguments: bool) -> list[str]:
        """Фикстуры, которые просит узел: имена аргументов (``arguments``; у класса — его
        методов, у модуля — его функций и методов), ``usefixtures`` и ``getfixturevalue``."""
        names: list[str] = []
        if arguments:
            if isinstance(node, ast.Module):
                functions = [s for s in node.body if isinstance(s, _FUNCS)]
                for cls in (s for s in node.body if isinstance(s, ast.ClassDef)):
                    functions += [s for s in cls.body if isinstance(s, _FUNCS)]
            elif isinstance(node, ast.ClassDef):
                functions = [s for s in node.body if isinstance(s, _FUNCS)]
            else:
                functions = [node] if isinstance(node, _FUNCS) else []
            for func in functions:
                args = func.args
                names += [a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr in ("usefixtures", "getfixturevalue")
            ):
                names += [str(a.value) for a in sub.args if isinstance(a, ast.Constant)]
        return names

    def _imported_fixtures(self, holder: Module, package: frozenset[str]) -> dict[str, Item]:
        """Фикстуры, пришедшие в модуль импортом, — по имени из декоратора."""
        key = f"{holder.rel}|{','.join(sorted(package))}"
        if key not in self._imported:
            found: dict[str, Item] = {}
            for local in holder.imports:
                for item in self._resolve(holder, [local], package, {}):
                    for node in item.nodes:
                        fixture = _fixture_decorator(node)
                        if fixture is not None and item.text is None:
                            found[fixture[0]] = Item(item.owner, item.name, item.nodes, True)
            self._imported[key] = found  # модули «*»-импорта — сами поставщики (_scope)
        return self._imported[key]

    @staticmethod
    def _autouse(item: Item) -> bool:
        return any((_fixture_decorator(n) or ("", False))[1] for n in item.nodes)

    def _provider(
        self, scope: list[Module], name: str, asker: Item, package: frozenset[str]
    ) -> Item | None:
        """Ближайший поставщик фикстуры; фикстура, просящая одноимённую, — следующий за ней."""
        start = 0
        if asker.owner in scope and name in (
            asker.name,
            *(f[0] for f in map(_fixture_decorator, asker.nodes) if f),
        ):
            start = scope.index(asker.owner) + 1
        for holder in scope[start:]:
            if name in holder.fixtures:
                return Item(holder, name, [holder.fixtures[name]], True)
            imported = self._imported_fixtures(holder, package)
            if name in imported:
                return imported[name]
        return None

    def _resolve(
        self,
        owner: Module,
        chain: list[str],
        package: frozenset[str],
        local: dict[str, list[tuple[str, str | None]]],
        depth: int = 0,
    ) -> list[Item]:
        name = chain[0]
        if depth > 12:
            return []
        if name in local:  # импорт внутри функции затеняет имена модуля
            return self._binding(owner, name, local[name], chain, package, depth)
        out: list[Item] = []
        if name in owner.bindings:
            out.append(Item(owner, name, list(owner.bindings[name]), False))
        if name in owner.imports:
            out += self._binding(owner, name, owner.imports[name], chain, package, depth)
        if not out:
            for dotted in owner.stars:
                star = self._module_of(dotted, owner, package)
                if star is not None:
                    out += self._resolve(star, chain, package, {}, depth + 1)
        return out

    def _binding(
        self,
        owner: Module,
        name: str,
        targets: list[tuple[str, str | None]],
        chain: list[str],
        package: frozenset[str],
        depth: int,
    ) -> list[Item]:
        text = "\n".join(f"{dotted}:{attr}" for dotted, attr in targets)
        out = [Item(owner, f"{name} (импорт)", [], False, text=text)]
        for dotted, attr in targets:
            out += self._follow(owner, dotted, attr, chain[1:], package, depth)
        return out

    def _follow(
        self,
        owner: Module,
        dotted: str,
        attr: str | None,
        rest: list[str],
        package: frozenset[str],
        depth: int,
    ) -> list[Item]:
        if attr is None:  # import a.b.c / import a.b as x → цепочка атрибутов
            for k in range(len(rest), -1, -1):
                target = self._module_of(".".join([dotted, *rest[:k]]), owner, package)
                if target is not None:
                    if rest[k:]:
                        return self._resolve(target, rest[k:], package, {}, depth + 1)
                    return [Item(target, WHOLE_MODULE, [target.tree], False)]
            return []
        target = self._module_of(dotted, owner, package)
        if target is not None and (
            attr in target.bindings or attr in target.imports or target.stars
        ):
            found = self._resolve(target, [attr, *rest], package, {}, depth + 1)
            if found:
                return found
        submodule = self._module_of(f"{dotted}.{attr}", owner, package)
        if submodule is not None:  # from tests.e2e import _reporting → _reporting.h
            if rest:
                return self._resolve(submodule, rest, package, {}, depth + 1)
            return [Item(submodule, WHOLE_MODULE, [submodule.tree], False)]
        return []


# --- посадки и журнал ----------------------------------------------------------------------


@dataclass
class Planting:
    label: str
    file: str
    old: str
    expect: list[str]
    count: int = 1
    what: str = ""
    spec: str = ""
    inputs: list[str] = field(default_factory=list)
    new: str | None = None  # замена харнесса: блок сравнивается и после неё


def _relative(path: str, what: str) -> str:
    norm = posixpath.normpath(path)
    if posixpath.isabs(norm) or norm == ".." or norm.startswith("../") or norm == ".":
        raise InputError(f"{what} {path!r}: нужен путь внутри репозитория, от его корня")
    return norm


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
        if not isinstance(raw.get("inputs", []), list):
            raise InputError(f"{path}: посадка {raw!r:.80}: inputs — список путей")
        try:
            planting = Planting(
                label=str(raw["label"]),
                file=_relative(str(raw["file"]), "file"),
                old=str(raw["old"]),
                expect=[str(t) for t in raw["expect"]],
                count=int(raw.get("count", 1)),
                what=str(raw.get("what", "")),
                spec=spec_hash(raw),
                inputs=[_relative(str(p), "inputs") for p in raw.get("inputs", [])],
                new=raw["new"] if isinstance(raw.get("new"), str) else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InputError(f"{path}: посадка {raw!r:.80}: {exc}") from exc
        for test in planting.expect:
            _relative(test.split("::")[0].split("[")[0], f"{planting.label}: expect")
        if not planting.old or planting.count < 1:
            raise InputError(f"{path}: {planting.label}: пустой образец или count < 1")
        out.append(planting)
    labels = [p.label for p in out]
    if len(set(labels)) != len(labels):
        raise InputError(f"{path}: метки посадок повторяются")
    return out


@dataclass
class Run:
    verdict: str
    tree: str | None
    spec: str | None


def load_log(path: Path) -> dict[str, Run]:
    """Последняя строка каждой метки."""
    runs: dict[str, Run] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            runs[str(row["label"])] = Run(
                verdict=str(row["verdict"]),
                tree=str(row["tree"]) if row.get("tree") else None,
                spec=str(row["spec"]) if row.get("spec") else None,
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise InputError(
                f"{path}:{number}: строка журнала без label или verdict: {exc}"
            ) from exc
    return runs


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


def code_reasons(planting: Planting, base: Source, current: Source) -> list[str]:
    """Образец и код вокруг него; вызывается, когда образец в дереве совпал по числу."""
    if base.read_bytes(planting.file) is None:
        return [f"файла {planting.file} нет в базе"]
    before, now = base.read(planting.file) or "", current.read(planting.file) or ""
    if before.count(planting.old) != planting.count:
        return ["образец новый или изменён"]
    tree_now, tree_before = current.tree(planting.file), base.tree(planting.file)
    if not planting.file.endswith(".py") or tree_now is None or tree_before is None:
        if current.digests(planting.file) == base.digests(planting.file):
            return []
        kind = "не Python" if not planting.file.endswith(".py") else "не разбирается"
        return [f"файл {planting.file} изменён ({kind} — целиком)"]
    reasons: list[str] = []
    paths_now = blocks(tree_now, now, planting.old)
    if paths_now != blocks(tree_before, before, planting.old):
        reasons.append("образец переехал в другой блок")
    unique: list[list[str]] = []
    for path in paths_now:
        if path not in unique:
            unique.append(path)
    after_now = after_before = None
    if planting.new is not None:  # замена бьёт по тому же коду? (образец сдвинут внутри блока)
        after_now = _parse(now.replace(planting.old, planting.new))
        after_before = _parse(before.replace(planting.old, planting.new))
    for path in unique:
        node_now, node_before = find(tree_now, path), find(tree_before, path)
        if node_before is None or node_now is None:
            reasons.append(f"код {block_name(path)} нет в базе")
        elif shape(node_now) != shape(node_before):
            reasons.append(f"код {block_name(path)}")
        elif planting.new is not None:
            planted_now = find(after_now, path) if after_now is not None else None
            planted_before = find(after_before, path) if after_before is not None else None
            if (planted_now is None) != (planted_before is None) or (
                planted_now is not None
                and planted_before is not None
                and shape(planted_now) != shape(planted_before)
            ):
                reasons.append(f"код {block_name(path)} после замены")
    return reasons


def _label(key: str, ambiguous: set[str]) -> str:
    name = key.split("::")[-1]
    return key if name in ambiguous else name


def _groups(now: dict[str, str], before: dict[str, str]) -> list[str]:
    groups = (
        ("изменено", [k for k in now if k in before and now[k] != before[k]]),
        ("добавлено", [k for k in now if k not in before]),
        ("убрано", [k for k in before if k not in now]),
    )
    names = [k.split("::")[-1] for _, keys in groups for k in keys]
    ambiguous = {n for n in names if names.count(n) > 1}
    return [
        f"{title} {', '.join(sorted(_label(k, ambiguous) for k in keys))}"
        for title, keys in groups
        if keys
    ]


def test_reasons(planting: Planting, base: Closure, current: Closure) -> list[str]:
    reasons: list[str] = []
    for test in sorted({t.split("[")[0] for t in planting.expect}):
        path = test.split("::")[0]
        name = test.split("::", 1)[-1] if "::" in test else path
        if not path.endswith(".py"):
            rel = current.rel(path)
            now_id, before_id = current.src.digests(rel).get(rel), base.src.digests(rel).get(rel)
            if now_id is None:
                reasons.append(f"тест {name}: нет в дереве")
            elif before_id is None:
                reasons.append(f"тест {name}: новый")
            elif now_id != before_id:
                reasons.append(f"тест {name}: файл изменён")
            continue
        now, before = current.of_test(test), base.of_test(test)
        if now is None:
            reasons.append(f"тест {name}: нет в дереве")
        elif before is None:
            reasons.append(f"тест {name}: новый")
        elif parts := _groups(now, before):
            reasons.append(f"тест {name}: {'; '.join(parts)}")
    return reasons


def input_reasons(planting: Planting, base: Source, current: Source) -> list[str]:
    reasons: list[str] = []
    for path in planting.inputs:
        now, before = current.digests(path), base.digests(path)
        if not now and not before:
            reasons.append(f"вход {path}: нет в дереве")
        elif parts := _groups(
            {f"::{k}": v for k, v in now.items()}, {f"::{k}": v for k, v in before.items()}
        ):
            reasons.append(f"вход {path}: {'; '.join(parts)}")
    return reasons


def _bases(
    root: Path, tests_root: str, runs: dict[str, Run]
) -> Iterator[tuple[str, tuple[Source, Closure] | None]]:
    for tree in {r.tree for r in runs.values() if r.tree}:
        ok = _run_git(root, "cat-file", "-e", f"{tree}^{{tree}}").returncode == 0
        source = Source(root, tree)
        yield tree, ((source, Closure(source, tests_root)) if ok else None)


def compare(
    plantings: list[Planting], root: Path, tests_root: str, runs: dict[str, Run]
) -> list[Verdict]:
    current = Source(root)
    current_closure = Closure(current, tests_root)
    bases = dict(_bases(root, tests_root, runs))
    try:
        return _compare(plantings, current, current_closure, bases, runs)
    finally:
        for pair in bases.values():
            if pair is not None:
                pair[0].close()


def _compare(
    plantings: list[Planting],
    current: Source,
    current_closure: Closure,
    bases: dict[str, tuple[Source, Closure] | None],
    runs: dict[str, Run],
) -> list[Verdict]:
    verdicts: list[Verdict] = []
    for planting in plantings:
        text = current.read(planting.file)
        found = text.count(planting.old) if text is not None else 0
        reasons: list[str] = []
        if found != planting.count:
            reasons.append(f"образец в дереве {found} раз вместо {planting.count}")
        run = runs.get(planting.label)
        if run is None:
            reasons.append("не измерялась")
        elif run.verdict != "RED":
            reasons.append(f"последний прогон {run.verdict}")
        elif run.tree is None:
            reasons.append("в журнале нет дерева прогона")
        elif run.spec is None or run.spec != planting.spec:
            reasons.append("описание посадки изменено" if run.spec else "в журнале нет spec")
        elif bases.get(run.tree) is None:
            reasons.append(f"дерева прогона {run.tree[:12]} нет в репозитории")
        else:
            pair = bases[run.tree]
            assert pair is not None  # noqa: S101 — проверено строкой выше
            if found == planting.count:
                reasons += code_reasons(planting, pair[0], current)
            reasons += test_reasons(planting, pair[1], current_closure)
            reasons += input_reasons(planting, pair[0], current)
        verdicts.append(Verdict(planting.label, planting.what, reasons, found, planting.count))
    return verdicts


def render(verdicts: list[Verdict]) -> str:
    remeasure = [v for v in verdicts if v.reasons and not v.mismatch]
    dry = [v.label for v in verdicts if not v.reasons]
    mismatch = [v for v in verdicts if v.mismatch]
    width = max((len(v.label) for v in verdicts), default=0)
    lines = [f"перемерить: {len(remeasure)}"]
    lines += [f"  {v.label:{width}}  {'; '.join(v.reasons)}" for v in remeasure]
    lines.append(f"сухо: {len(dry)}" + (f" — {' '.join(dry)}" if dry else ""))
    lines.append(f"образец не совпал: {len(mismatch)}")
    lines += [f"  {v.label:{width}}  {'; '.join(v.reasons)}" for v in mismatch]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="корень репозитория")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("snapshot", help="дерево git рабочей копии — для строки журнала")
    spec_parser = sub.add_parser("spec", help="метка и spec каждой посадки")
    spec_parser.add_argument("--plantings", required=True, type=Path, help="JSON или харнесс .py")
    cmp_parser = sub.add_parser("compare", help="какие посадки перемерить")
    cmp_parser.add_argument("--plantings", required=True, type=Path, help="JSON или харнесс .py")
    cmp_parser.add_argument("--log", required=True, type=Path, help="журнал прогонов харнесса")
    cmp_parser.add_argument(
        "--tests-root", default=DEFAULT_TESTS_ROOT, help="каталог, от которого пути тестов"
    )
    cmp_parser.add_argument("--json", action="store_true", help="машинный вывод")
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    try:
        if args.command == "snapshot":
            print(snapshot(root))
            return 0
        plantings = load_plantings(args.plantings)
        if args.command == "spec":
            print("\n".join(f"{p.label}\t{p.spec}" for p in plantings))
            return 0
        tests_root = _relative(args.tests_root, "--tests-root") if args.tests_root != "." else "."
        if not (root / tests_root).is_dir():
            raise InputError(f"--tests-root {args.tests_root}: нет такого каталога в дереве")
        verdicts = compare(plantings, root, tests_root, load_log(args.log))
    except (InputError, OSError, ValueError) as exc:
        print(f"plantings_affected: {exc}", file=sys.stderr)
        return 2
    if args.json:
        rows = [
            {
                "label": v.label,
                "what": v.what,
                "reasons": v.reasons,
                "found": v.found,
                "expected": v.expected,
            }
            for v in verdicts
        ]
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(render(verdicts))
    return 1 if any(v.mismatch for v in verdicts) else 0


if __name__ == "__main__":
    sys.exit(main())
