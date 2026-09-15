#!/usr/bin/env python3
"""Развёртка устаревших чисел и формулировок по документам и коду задачи.

Когда предел, частота или правило меняются в одном месте, их прежние значения остаются в
других документах — числом («8 r/s»), словом («восьми в секунду»), в другом регистре («Два
сертификата») или производной фразой («2 × 5 > 15»). Скрипт берёт файл шаблонов задачи
(``docs/sweeps/<задача>.json``), разворачивает каждое число в числовую и словесные формы
(именительный, косвенные падежи, женский род для 1 и 2, составные числительные до 999),
ищет без учёта регистра и печатает совпадения по файлам; строки с пометкой истории
(«в раунде 7», «стояло», «было») и строки после заголовка исторического раздела отчёта
выводятся отдельной группой — это не устаревшие значения, а подписанная история.

Это развёртка, а не гейт: совпадение разбирает человек (ложные, история, действующее
значение, настоящее устаревшее), итог с классификацией идёт в раздел «Регрессия» отчёта
задачи. Код возврата 0; ``--strict`` возвращает 1, если есть совпадения вне истории.

Формат шаблонов::

    {
      "task": "001.33",
      "history_markers": ["раунд", "стояло", "было"],
      "history_sections": {"tests/tests-001/report-001-33.md": "^## Раунд 1 — роаст"},
      "history_ranges": [{"file": "tests/tests-001/report-001-33.md",
                          "from": "^## Посадки стражей", "to": "^## "}],
      "patterns": [
        {"name": "частота парка", "value": 8, "current": "15 r/s",
         "forms": ["{n} r/s", "{n}r/s", "{n} в секунду"]},
        {"name": "сертификаты рычага", "regex": "два сертификата[^.;]*целиком",
         "current": "два — две трети, три — всю"},
        {"name": "запрет временных файлов", "literal": "proxy_max_temp_file_size 0",
         "current": "8m"}
      ]
    }

``value`` — число (целое или строка вроде ``"16,2"``), ``forms`` — фразы с ``{n}``; ``regex`` —
готовое выражение; ``literal`` — точная подстрока. Файлы: изменённые и новые по ``git``
(по умолчанию), плюс изменённые с ревизии ``--since``, либо явный список.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

EXTENSIONS = (".md", ".py", ".conf", ".yml", ".yaml", ".json", ".toml", ".sh", ".sql")
DEFAULT_MARKERS = ("раунд", "стояло", "был", "прежн", "раньше", "до закрытия", "первый прогон",
                   "исправлен", "снижен", "перемерен", "истори")
# Собственные файлы развёртки и записи книг ретро цитируют старые значения по назначению.
DEFAULT_EXCLUDES = ("docs/sweeps/", "docs/scripts/sweep_stale.py", "docs/scripts/tests/",
                    "docs/backlog/", "docs/issues/")

_UNITS = {
    1: ["один", "одна", "одно", "одного", "одной", "одному", "одним", "одном"],
    2: ["два", "две", "двух", "двум", "двумя"],
    3: ["три", "трёх", "трех", "трём", "трем", "тремя"],
    4: ["четыре", "четырёх", "четырех", "четырём", "четырем", "четырьмя"],
    5: ["пять", "пяти", "пятью"],
    6: ["шесть", "шести", "шестью"],
    7: ["семь", "семи", "семью"],
    8: ["восемь", "восьми", "восемью", "восьмью"],
    9: ["девять", "девяти", "девятью"],
    10: ["десять", "десяти", "десятью"],
    11: ["одиннадцать", "одиннадцати", "одиннадцатью"],
    12: ["двенадцать", "двенадцати", "двенадцатью"],
    13: ["тринадцать", "тринадцати", "тринадцатью"],
    14: ["четырнадцать", "четырнадцати", "четырнадцатью"],
    15: ["пятнадцать", "пятнадцати", "пятнадцатью"],
    16: ["шестнадцать", "шестнадцати", "шестнадцатью"],
    17: ["семнадцать", "семнадцати", "семнадцатью"],
    18: ["восемнадцать", "восемнадцати", "восемнадцатью"],
    19: ["девятнадцать", "девятнадцати", "девятнадцатью"],
}
_TENS = {
    20: ["двадцать", "двадцати", "двадцатью"],
    30: ["тридцать", "тридцати", "тридцатью"],
    40: ["сорок", "сорока"],
    50: ["пятьдесят", "пятидесяти", "пятьюдесятью"],
    60: ["шестьдесят", "шестидесяти", "шестьюдесятью"],
    70: ["семьдесят", "семидесяти", "семьюдесятью"],
    80: ["восемьдесят", "восьмидесяти", "восемьюдесятью"],
    90: ["девяносто", "девяноста"],
}
_HUNDREDS = {
    100: ["сто", "ста"],
    200: ["двести", "двухсот", "двумстам", "двумястами", "двухстах"],
    300: ["триста", "трёхсот", "трехсот", "тремстам", "тремястами"],
    400: ["четыреста", "четырёхсот", "четырехсот", "четырёмстам", "четырьмястами"],
    500: ["пятьсот", "пятисот", "пятистам", "пятьюстами"],
    600: ["шестьсот", "шестисот", "шестистам"],
    700: ["семьсот", "семисот", "семистам"],
    800: ["восемьсот", "восьмисот", "восьмистам"],
    900: ["девятьсот", "девятисот", "девятистам"],
}


def numeral_forms(n: int) -> list[str]:
    """Словесные формы количественного числительного ``n`` (0…999) во всех падежах, которые
    встречаются в документах; составные — как сочетания форм частей (лишние сочетания
    безвредны: в тексте они не встречаются)."""
    if n == 0:
        return ["ноль", "нуль", "нуля", "нулю", "нулём", "нулем"]
    if n < 0 or n > 999:
        return []
    parts: list[list[str]] = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        parts.append(_HUNDREDS[hundreds * 100])
    if rest in _UNITS:
        parts.append(_UNITS[rest])
    elif rest:
        tens, units = divmod(rest, 10)
        parts.append(_TENS[tens * 10])
        if units:
            parts.append(_UNITS[units])
    return [" ".join(combo) for combo in itertools.product(*parts)]


def number_forms(value: int | str) -> list[str]:
    """Числовая запись (с вариантами разделителя тысяч) и словесные формы числа."""
    text = str(value).strip()
    forms = [text]
    if re.fullmatch(r"\d+", text):
        n = int(text)
        if n >= 1000:
            groups = f"{n:,}".split(",")
            for sep in (" ", " ", " ", ""):
                forms.append(sep.join(groups))
        forms.extend(numeral_forms(n))
    return list(dict.fromkeys(forms))


def _alternation(value: int | str) -> str:
    digits, words = [], []
    for form in number_forms(value):
        if re.fullmatch(r"[\d\s  ,.]+", form):
            digits.append(r"(?<![\d,.])" + re.escape(form) + r"(?![\d,.])")
        else:
            words.append(r"\b" + re.escape(form) + r"\b")
    return "(?:" + "|".join(digits + words) + ")"


@dataclass
class Pattern:
    name: str
    regex: re.Pattern[str]
    current: str = ""
    since: str = ""


@dataclass
class Hit:
    pattern: str
    path: str
    line: int
    text: str
    history: bool


@dataclass
class Sweep:
    task: str
    patterns: list[Pattern]
    markers: tuple[str, ...]
    history_sections: dict[str, re.Pattern[str]] = field(default_factory=dict)
    history_ranges: list[tuple[str, re.Pattern[str], re.Pattern[str]]] = field(default_factory=list)
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES


def compile_pattern(spec: dict) -> Pattern:
    name = spec.get("name") or spec.get("literal") or spec.get("regex") or "?"
    flags = re.IGNORECASE
    if "regex" in spec:
        source = spec["regex"]
    elif "literal" in spec:
        source = re.escape(spec["literal"])
    elif "value" in spec:
        forms = spec.get("forms") or ["{n}"]
        alt = _alternation(spec["value"])
        source = "|".join(
            re.escape(form).replace(re.escape("{n}"), alt).replace(r"\ ", r"[\s  ]+")
            for form in forms
        )
    else:
        raise ValueError(f"шаблон {name!r}: нужен regex, literal или value")
    return Pattern(name=str(name), regex=re.compile(source, flags),
                   current=str(spec.get("current", "")), since=str(spec.get("since", "")))


def load_sweep(path: Path) -> Sweep:
    data = json.loads(path.read_text(encoding="utf-8"))
    patterns = [compile_pattern(p) for p in data.get("patterns", [])]
    if not patterns:
        raise ValueError(f"{path}: в файле нет шаблонов")
    # Маркеры файла дополняют встроенные, а не заменяют их.
    markers = tuple(dict.fromkeys(DEFAULT_MARKERS + tuple(data.get("history_markers") or ())))
    sections = {k: re.compile(v) for k, v in (data.get("history_sections") or {}).items()}
    ranges = [(r["file"], re.compile(r["from"]), re.compile(r["to"]))
              for r in (data.get("history_ranges") or [])]
    excludes = tuple(data.get("exclude") or ()) + DEFAULT_EXCLUDES
    return Sweep(task=str(data.get("task", path.stem)), patterns=patterns, markers=markers,
                 history_sections=sections, history_ranges=ranges, excludes=excludes)


def scan_file(sweep: Sweep, path: Path, rel: str) -> list[Hit]:
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except (UnicodeDecodeError, OSError):
        return []
    cutoff = None
    section = sweep.history_sections.get(rel)
    if section is not None:
        for i, line in enumerate(lines):
            if section.search(line):
                cutoff = i
                break
    # Диапазоны истории: от первой строки, совпавшей с ``from``, до первой после неё,
    # совпавшей с ``to`` (например, таблица посадок, где старые значения посажены нарочно).
    ranged: set[int] = set()
    for file, start_re, end_re in sweep.history_ranges:
        if file != rel:
            continue
        start = next((i for i, line in enumerate(lines) if start_re.search(line)), None)
        if start is None:
            continue
        end = next((i for i in range(start + 1, len(lines)) if end_re.search(lines[i])), len(lines))
        ranged.update(range(start, end))
    marker_re = re.compile("|".join(re.escape(m) for m in sweep.markers), re.IGNORECASE)
    hits: list[Hit] = []
    in_fence = False
    fence_history = False
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            if in_fence:
                # Блок кода — история, если абзац перед ним помечен (например, «Раунд 7 (…):»).
                lead = [x for x in lines[max(0, i - 4):i] if x.strip()]
                fence_history = bool(lead and marker_re.search(lead[-1]))
            continue
        for pattern in sweep.patterns:
            if pattern.regex.search(line):
                history = ((cutoff is not None and i >= cutoff) or i in ranged
                           or bool(marker_re.search(line)) or (in_fence and fence_history))
                hits.append(Hit(pattern.name, rel, i + 1, line.strip(), history))
    return hits


def _git(root: Path, *args: str) -> list[str]:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    return [line for line in result.stdout.split("\n") if line.strip()] if result.returncode == 0 else []


def select_files(root: Path, since: str | None, explicit: list[str],
                 excludes: tuple[str, ...] = DEFAULT_EXCLUDES) -> list[str]:
    if explicit:
        names = explicit
    else:
        names = _git(root, "ls-files", "-m", "-o", "--exclude-standard")
        if since:
            names += _git(root, "diff", "--name-only", since)
    unique = sorted(dict.fromkeys(
        n for n in names
        if n.endswith(EXTENSIONS) and (root / n).is_file()
        and not (not explicit and any(n == e or n.startswith(e) for e in excludes))))
    return unique


def run(sweep: Sweep, root: Path, files: list[str]) -> list[Hit]:
    hits: list[Hit] = []
    for rel in files:
        hits.extend(scan_file(sweep, root / rel, rel))
    return hits


def render(sweep: Sweep, hits: list[Hit], live_only: bool) -> str:
    out = [f"развёртка {sweep.task}: шаблонов {len(sweep.patterns)}, совпадений {len(hits)}, "
           f"в истории {sum(h.history for h in hits)}, вне истории {sum(not h.history for h in hits)}"]
    for pattern in sweep.patterns:
        mine = [h for h in hits if h.pattern == pattern.name]
        shown = [h for h in mine if not (live_only and h.history)]
        if not mine:
            continue
        current = f" — сейчас: {pattern.current}" if pattern.current else ""
        out.append(f"== {pattern.name}: {len(mine)} (история {sum(h.history for h in mine)}){current}")
        for h in shown:
            tag = "[история] " if h.history else ""
            out.append(f"    {h.path}:{h.line}: {tag}{h.text[:160]}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--patterns", required=True, type=Path, help="файл шаблонов задачи (JSON)")
    parser.add_argument("--since", help="ревизия git: добавить файлы, изменённые с неё")
    parser.add_argument("--root", type=Path, default=None, help="корень репозитория (по умолчанию — из git)")
    parser.add_argument("--live-only", action="store_true", help="не печатать строки истории")
    parser.add_argument("--strict", action="store_true", help="код 1, если есть совпадения вне истории")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("files", nargs="*", help="явный список файлов (относительно корня)")
    args = parser.parse_args(argv)
    root = args.root or Path(_git(Path.cwd(), "rev-parse", "--show-toplevel")[0] if _git(Path.cwd(), "rev-parse", "--show-toplevel") else Path.cwd())
    root = root.resolve()
    sweep = load_sweep(args.patterns if args.patterns.is_absolute() else (root / args.patterns))
    files = select_files(root, args.since, args.files, sweep.excludes)
    hits = run(sweep, root, files)
    if args.json:
        print(json.dumps({"task": sweep.task, "files": files, "hits": [h.__dict__ for h in hits]},
                         ensure_ascii=False, indent=1))
    else:
        print(render(sweep, hits, args.live_only))
    live = sum(not h.history for h in hits)
    return 1 if (args.strict and live) else 0


if __name__ == "__main__":
    sys.exit(main())
