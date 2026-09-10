#!/usr/bin/env python3
"""Граф зависимостей и состояние задач плана (``docs/PLAN.md``).

Источник истины — записи задач в разделе «Последовательность выполнения задач»: строки
``Зависимости:`` задают рёбра графа, строки ``Статус:`` — состояние. Скрипт собирает из них блок
между маркерами ``<!-- generated:plan-status start -->`` и ``<!-- generated:plan-status end -->``
(таблица прогресса по этапам, задачи, готовые к началу, критический путь, диаграмма Mermaid)
и отмечает пункты чек-листа RTM, все задачи которых приняты.

Словарь статусов (строка ``  - Статус:`` в записи задачи):

- ``принята — коммит <hash> (<дата>)`` — код принят ревью и закоммичен; текст после «принята»
  свободный, дата в скобках обязательна;
- ``в работе (с <дата>)`` — задача начата;
- ``не начата`` — остальные. «Готова к началу» не пишется руками: она выводится из графа
  (все зависимости приняты) и показывается в таблице и на диаграмме.

Запуск: ``python3 docs/scripts/plan_graph.py`` — печатает блок; ``--write`` — переписывает блок
и чек-лист RTM в файле; ``--check`` — код возврата 1, если файл отстал от записей задач (для
шага приёмки задачи и CI). Зависимость на несуществующую задачу и цикл — ошибка в любом режиме.
Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

START = "<!-- generated:plan-status start -->"
END = "<!-- generated:plan-status end -->"
TASK_RE = re.compile(r"- \*\*Задача (001\.\d{2})\*\* — ([^\n]+)\n((?:  - [^\n]+\n)+)")
STAGE_RE = re.compile(r"^### (Этап (\d+) — [^\n]*?)(?: — \d+ ч)?$", re.MULTILINE)
DEP_RE = re.compile(r"Задача (001\.\d{2})")
DATE_RE = re.compile(r"\((?:с )?(\d{4}-\d{2}-\d{2})\)")
RTM_RE = re.compile(
    r"^- \[( |x)\] \[(R-\d+)\] (.*? — задачи ((?:\d{2}(?:, )?)+) \(приоритет \w+\))$", re.MULTILINE
)
KINDS = {
    "CONFIGURATION": "конфигурация",
    "STUB CREATION": "заглушки",
    "LOGIC IMPLEMENTATION": "логика",
}
# Диаграмма Ганта без календаря: единица оси — час оценки, задача стартует после всех своих
# зависимостей (``after``). Mermaid gantt умеет только даты, поэтому час оценки кодируется
# миллисекундой от эпохи (``dateFormat x``, длительность ``Nms``), а ось подписана ``%-L`` —
# миллисекундами, то есть часами от старта. Подпись справа от полосы, если правый отступ не
# меньше ширины подписи плюс половины левого — иначе mermaid переносит её влево.
GANTT_INIT = {
    "theme": "base",
    "themeVariables": {
        "fontSize": "12px",
        "taskBkgColor": "#ffffff",
        "taskBorderColor": "#9e9e9e",
        "taskTextColor": "#212121",
        "taskTextOutsideColor": "#212121",
        "taskTextDarkColor": "#212121",
        "taskTextLightColor": "#212121",
        "doneTaskBkgColor": "#c8e6c9",
        "doneTaskBorderColor": "#2e7d32",
        "activeTaskBkgColor": "#fff3c4",
        "activeTaskBorderColor": "#f9a825",
        "critBkgColor": "#ffffff",
        "critBorderColor": "#c62828",
        "sectionBkgColor": "#f5f5f5",
        "sectionBkgColor2": "#ffffff",
        "altSectionBkgColor": "#ffffff",
        "gridColor": "#e0e0e0",
    },
    "gantt": {
        "axisFormat": "%-L",
        "tickInterval": "5millisecond",
        "useWidth": 1150,
        "leftPadding": 250,
        "rightPadding": 330,
        "topPadding": 40,
        "barHeight": 16,
        "barGap": 4,
        "fontSize": 12,
        "sectionFontSize": 12,
        "numberSectionStyles": 2,
    },
}
PRIORITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}

# Короткие подписи задач на диаграмме: полное название слишком длинно для строки Ганта. Задача
# без подписи получает усечённое название. Двоеточие в подписи недопустимо — в синтаксисе gantt
# оно отделяет текст от параметров задачи.
LABELS = {
    "01": "Каркас репозитория",
    "02": "Compose стенда",
    "03": "Миграции и роли",
    "04": "Схема — аккаунты",
    "05": "Схема — тарифы",
    "06": "Схема — ноды",
    "07": "Схема — подписки",
    "08": "Схема — учёт трафика",
    "09": "Схема — события, очередь",
    "10": "Каркас FastAPI",
    "11": "Очередь — заглушки",
    "12": "Безопасность — заглушки",
    "74": "Очередь — повторы, DLQ",
    "13": "Auth API — заглушки",
    "14": "Auth API — логика",
    "15": "/me — заглушки",
    "84": "Сессии, CSRF, лимиты входа",
    "18": "Admin-каталог — заглушки",
    "19": "Тарифы и группы",
    "21": "Подписки — заглушки",
    "22": "Подписки — логика",
    "20": "Коды — логика",
    "83": "Истечение подписок",
    "24": "Nodes API — заглушки",
    "25": "Enrollment, identity",
    "26": "Inbound, Xray — заглушки",
    "28": "Node API — заглушки",
    "29": "Поток состава",
    "27": "Inbound, Xray — логика",
    "30": "Heartbeat, статусы",
    "31": "Версии, update_agent",
    "32": "Внешние пробы",
    "75": "Снапшот, long-poll",
    "76": "Служба команд",
    "33": "Отчёты, учёт — заглушки",
    "23": "Коэффициент по дате",
    "34": "Приём отчёта — проверки",
    "77": "Приём отчёта — факты",
    "35": "Лимиты 80/95/100",
    "36": "Гранты квоты",
    "37": "Сверки, партиции",
    "38": "Лимит адресов",
    "39": "Признаки перепродажи",
    "40": "Стратегия разрыва",
    "41": "/s/token — заглушки",
    "42": "Токен подписки",
    "16": "/me — логика",
    "17": "Удаление аккаунта",
    "43": "Генераторы форматов",
    "44": "Состав серверов",
    "45": "Два домена подписки",
    "46": "Admin auth, RBAC — заглушки",
    "47": "TOTP, сессии админов",
    "48": "RBAC, Audit Log",
    "49": "API панели — заглушки",
    "50": "API панели — пользователи",
    "85": "API панели — ноды, дашборд",
    "51": "События, почта — заглушки",
    "52": "Уведомления",
    "78": "Доставка почты",
    "79": "Доставка webhook",
    "53": "Agent — каркас",
    "54": "Agent — enrollment",
    "55": "Agent — long-poll",
    "56": "Agent — счётчики, отчёты",
    "80": "Agent — применение конфига",
    "60": "Agent — разрыв readd",
    "57": "Agent — локальный грант",
    "58": "Agent — блокировка адресов",
    "59": "Agent — heartbeat, метрики",
    "61": "Bootstrap ноды, systemd",
    "81": "Agent — команды, update",
    "82": "Agent — route_block, restart",
    "86": "Agent — nftables",
    "62": "Web — каркасы",
    "63": "Web — кабинет",
    "64": "Web — панель",
    "65": "Локализация RU/EN",
    "66": "Compose prod, mTLS",
    "67": "pgbackrest, восстановление",
    "68": "Метрики, алерты",
    "69": "Правила, Suspended",
    "70": "Страница состояния",
    "71": "Документация",
    "72": "Rate limits — полный список",
    "73": "Стенд — приёмка",
}


def wrap(text: str) -> str:
    """Абзац шириной 100 колонок — как остальной текст плана."""
    return textwrap.fill(text, width=100, break_long_words=False, break_on_hyphens=False)


class PlanError(Exception):
    """Ошибка структуры плана: битая ссылка, цикл, отсутствие маркеров."""


@dataclass
class Task:
    id: str
    title: str
    kind: str
    stage: int
    estimate: int
    priority: str
    deps: list[str]
    status: str  # принята | в работе | не начата
    status_text: str
    date: str | None

    @property
    def nn(self) -> str:
        return self.id[4:]


@dataclass
class Plan:
    text: str
    stages: dict[int, str]
    tasks: dict[str, Task]
    order: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> set[str]:
        return {t for t, task in self.tasks.items() if task.status == "принята"}

    def ready(self, task: Task) -> bool:
        accepted = self.accepted
        return task.status == "не начата" and all(d in accepted for d in task.deps)

    def state(self, task: Task) -> str:
        """Состояние узла: принята, в работе, готова (выводится) или ждёт зависимостей."""
        if task.status != "не начата":
            return task.status
        return "готова" if self.ready(task) else "ждёт"


def parse(text: str) -> Plan:
    stages = {int(m.group(2)): m.group(1) for m in STAGE_RE.finditer(text)}
    stage_at = [(m.start(), int(m.group(2))) for m in STAGE_RE.finditer(text)]
    plan = Plan(text=text, stages=stages, tasks={})
    for m in TASK_RE.finditer(text):
        tid, title, block = m.group(1), m.group(2).strip(), m.group(3)
        if tid in plan.tasks:
            raise PlanError(f"задача {tid} описана дважды")
        stage = max((s for s in stage_at if s[0] < m.start()), key=lambda s: s[0])[1]
        fields = dict(re.findall(r"  - ([^:\n]+): ([^\n]*)", block))
        kind = re.search(r"\[(CONFIGURATION|STUB CREATION|LOGIC IMPLEMENTATION)\]", title)
        if not kind:
            raise PlanError(f"задача {tid}: нет метки типа в заголовке")
        status_text = fields.get("Статус", "не начата").strip()
        status = next(
            (s for s in ("принята", "в работе", "не начата") if status_text.startswith(s)), None
        )
        if status is None:
            raise PlanError(f"задача {tid}: неизвестный статус «{status_text}»")
        date = DATE_RE.search(status_text)
        if status != "не начата" and not date:
            raise PlanError(f"задача {tid}: статус «{status}» требует даты в скобках")
        plan.tasks[tid] = Task(
            id=tid,
            title=re.sub(r"\s*\[[A-Z ]+\]$", "", title),
            kind=kind.group(1),
            stage=stage,
            estimate=int(re.match(r"(\d+)", fields.get("Оценка", "0")).group(1)),  # type: ignore[union-attr]
            priority=fields.get("Приоритет", "").strip(),
            deps=DEP_RE.findall(fields.get("Зависимости", "")),
            status=status,
            status_text=status_text,
            date=date.group(1) if date else None,
        )
        plan.order.append(tid)
    if not plan.tasks:
        raise PlanError("в плане не найдено ни одной записи задачи")
    for task in plan.tasks.values():
        for dep in task.deps:
            if dep not in plan.tasks:
                raise PlanError(f"задача {task.id} зависит от несуществующей задачи {dep}")
    check_acyclic(plan)
    return plan


def check_acyclic(plan: Plan) -> None:
    """Обход в глубину с тремя цветами; цикл — ошибка с перечислением его задач."""
    colour: dict[str, int] = {}
    stack: list[str] = []

    def visit(tid: str) -> None:
        if colour.get(tid) == 1:
            cycle = stack[stack.index(tid) :] + [tid]
            raise PlanError("цикл зависимостей: " + " → ".join(cycle))
        if colour.get(tid) == 2:
            return
        colour[tid] = 1
        stack.append(tid)
        for dep in plan.tasks[tid].deps:
            visit(dep)
        stack.pop()
        colour[tid] = 2

    for tid in plan.order:
        visit(tid)


def critical_path(plan: Plan, target: str) -> tuple[int, list[str]]:
    """Самая длинная по оценке цепочка зависимостей до ``target`` (часы, задачи)."""
    memo: dict[str, tuple[int, list[str]]] = {}

    def longest(tid: str) -> tuple[int, list[str]]:
        if tid not in memo:
            task = plan.tasks[tid]
            if not task.deps:
                memo[tid] = (task.estimate, [tid])
            else:
                hours, path = max((longest(d) for d in task.deps), key=lambda x: x[0])
                memo[tid] = (hours + task.estimate, [*path, tid])
        return memo[tid]

    return longest(target)


def render(plan: Plan) -> str:
    tasks = plan.tasks
    accepted = plan.accepted
    dates = [t.date for t in tasks.values() if t.date]
    as_of = max(dates) if dates else "—"
    total_h = sum(t.estimate for t in tasks.values())
    done_h = sum(tasks[t].estimate for t in accepted)
    wip = [t for t in plan.order if tasks[t].status == "в работе"]
    ready = sorted(
        (t for t in plan.order if plan.ready(tasks[t])),
        key=lambda t: (PRIORITY_ORDER.get(tasks[t].priority, 9), t),
    )
    final = plan.order[-1]
    cp_hours, cp = critical_path(plan, final)
    lines = [
        START,
        "",
        wrap(
            f"Состояние на {as_of}: принято {len(accepted)} задач из {len(tasks)} "
            f"({done_h} ч из {total_h} ч), в работе {len(wip)}, готовы к началу {len(ready)}. "
            "Блок собран скриптом `docs/scripts/plan_graph.py` из строк `Статус:` и `Зависимости:` "
            "записей задач; после приёмки задачи обновите её строку `Статус:` и выполните "
            "`python3 docs/scripts/plan_graph.py --write`."
        ),
        "",
        "| Этап | Задач | Часов | Принято | Часов принято | В работе | Готовы к началу |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | :--- |",
    ]
    for stage in sorted(plan.stages):
        in_stage = [t for t in plan.order if tasks[t].stage == stage]
        acc = [t for t in in_stage if t in accepted]
        rdy = sorted(tasks[t].nn for t in ready if tasks[t].stage == stage)
        hours = sum(tasks[t].estimate for t in in_stage)
        started = sum(1 for t in in_stage if tasks[t].status == "в работе")
        lines.append(
            f"| {plan.stages[stage]} | {len(in_stage)} | {hours} | {len(acc)} "
            f"| {sum(tasks[t].estimate for t in acc)} | {started} | {', '.join(rdy) or '—'} |"
        )
    lines.append(
        f"| **Итого** | {len(tasks)} | {total_h} | {len(accepted)} | {done_h} | {len(wip)} "
        f"| {', '.join(sorted(tasks[t].nn for t in ready)) or '—'} |"
    )
    lines += ["", "Готовы к началу (все зависимости приняты; порядок — приоритет, затем номер):"]
    lines.append("")
    for t in ready:
        task = tasks[t]
        on_cp = " — на критическом пути" if t in cp else ""
        lines.append(
            wrap(
                f"- **{task.id}** — {task.title} ({KINDS[task.kind]}, {task.priority}, "
                f"{task.estimate} ч){on_cp}"
            ).replace("\n", "\n  ")
        )
    if not ready:
        lines.append("- нет: все незанятые задачи ждут зависимостей")
    lines += [
        "",
        wrap(
            f"Критический путь до задачи {final} — {cp_hours} ч по оценкам, "
            f"{sum(1 for t in cp if t in accepted)} из {len(cp)} задач приняты: "
            + " → ".join(tasks[t].nn for t in cp)
            + "."
        ),
        "",
        wrap(
            "Диаграмма — Гант без календаря: горизонталь — часы оценки от старта, полоса задачи "
            "начинается после всех её зависимостей и длится её оценку; секции — этапы. Заливка — "
            "состояние (зелёная — принята, жёлтая — готова к началу или в работе, белая — ждёт "
            "зависимостей), красная рамка — критический путь. Полные названия — в разделе "
            "«Последовательность выполнения задач»."
        ),
        "",
        "```mermaid",
        "%%{init: " + json.dumps(GANTT_INIT, ensure_ascii=False, indent=2) + "}%%",
        "gantt",
        "  title Порядок задач по графу зависимостей — часы оценки от старта, не календарь",
        "  dateFormat x",
        "  todayMarker off",
    ]
    for stage in sorted(plan.stages):
        lines.append(f"  section {plan.stages[stage]}")
        for t in plan.order:
            task = tasks[t]
            if task.stage != stage:
                continue
            label = LABELS.get(task.nn) or task.title[:24] + "…"
            label = label.replace(":", " —")
            state = plan.state(task)
            tags = ["done"] if state == "принята" else ["active"] if state != "ждёт" else []
            if t in cp:
                tags.append("crit")
            suffix = " · в работе" if state == "в работе" else ""
            start = f"after {' '.join('t' + tasks[d].nn for d in task.deps)}" if task.deps else "0"
            meta = ", ".join([*tags, f"t{task.nn}", start, f"{task.estimate}ms"])
            lines.append(f"    {task.nn} {label}{suffix} :{meta}")
    lines += ["```", "", END]
    return "\n".join(lines)


def apply(text: str, plan: Plan) -> str:
    """Текст плана с обновлённым блоком и чек-листом RTM."""
    if START not in text or END not in text:
        raise PlanError(f"в плане нет маркеров {START} … {END}")
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    accepted = {plan.tasks[t].nn for t in plan.accepted}

    def tick(m: re.Match[str]) -> str:
        done = all(nn in accepted for nn in m.group(4).split(", "))
        return f"- [{'x' if done else ' '}] [{m.group(2)}] {m.group(3)}"

    return RTM_RE.sub(tick, head + render(plan) + tail)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--plan", default="docs/PLAN.md", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="переписать блок и чек-лист RTM")
    mode.add_argument("--check", action="store_true", help="код 1, если файл отстал от задач")
    args = parser.parse_args(argv)
    text = args.plan.read_text(encoding="utf-8")
    try:
        plan = parse(text)
        if args.write or args.check:
            updated = apply(text, plan)
        else:
            print(render(plan))
            return 0
    except PlanError as exc:
        print(f"ошибка плана: {exc}", file=sys.stderr)
        return 2
    if args.write:
        if updated != text:
            args.plan.write_text(updated, encoding="utf-8")
            print(f"{args.plan}: блок состояния и чек-лист RTM обновлены")
        else:
            print(f"{args.plan}: без изменений")
        return 0
    if updated != text:
        print(f"{args.plan}: блок состояния или чек-лист RTM отстали — --write", file=sys.stderr)
        return 1
    print(f"{args.plan}: актуален")
    return 0


if __name__ == "__main__":
    sys.exit(main())
