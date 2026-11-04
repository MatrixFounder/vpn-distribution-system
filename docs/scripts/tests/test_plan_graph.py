"""Тесты графа плана (``docs/scripts/plan_graph.py``): части разделённой задачи с буквенным
суффиксом (``001.77b``, WI-35) — запись, зависимость, критический путь и пункт чек-листа RTM;
критический путь и диаграмма по оставшейся работе."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plan_graph as pg  # noqa: E402

PLAN = """\
### Этап 1 — учёт — 6 ч

- **Задача 001.77** — Факты частей [LOGIC IMPLEMENTATION]
  - Приоритет: Critical
  - Оценка: 3 ч
  - Зависимости: нет
  - Статус: принята — коммит abc1234 (2026-10-25)

- **Задача 001.77b** — Накопление часа [LOGIC IMPLEMENTATION]
  - Приоритет: High
  - Оценка: 2 ч
  - Зависимости: Задача 001.77
  - Статус: не начата

- **Задача 001.73** — Приёмка [CONFIGURATION]
  - Приоритет: Critical
  - Оценка: 1 ч
  - Зависимости: Задача 001.77b
  - Статус: не начата

## Чек-лист требований RTM

- [ ] [R-21] Приём отчёта — задачи 77, 77b (приоритет M)
- [ ] [R-19] Отчёт — задачи 77 (приоритет M)

<!-- generated:plan-status start -->
<!-- generated:plan-status end -->
"""


class SuffixedParts(unittest.TestCase):
    def test_a_suffixed_part_is_a_task_and_a_dependency(self) -> None:
        plan = pg.parse(PLAN)
        self.assertEqual(list(plan.tasks), ["001.77", "001.77b", "001.73"])
        self.assertEqual(plan.tasks["001.77b"].deps, ["001.77"])
        self.assertEqual(plan.tasks["001.73"].deps, ["001.77b"])
        self.assertEqual(plan.tasks["001.77b"].nn, "77b")

    def test_the_critical_path_runs_through_the_part(self) -> None:
        hours, path = pg.critical_path(pg.parse(PLAN), "001.73")
        self.assertEqual((hours, path), (3, ["001.77b", "001.73"]))

    def test_an_rtm_item_waits_for_every_part(self) -> None:
        text = pg.apply(PLAN, pg.parse(PLAN))
        self.assertIn("- [ ] [R-21] Приём отчёта — задачи 77, 77b (приоритет M)", text)
        self.assertIn("- [x] [R-19] Отчёт — задачи 77 (приоритет M)", text)
        done = PLAN.replace(
            "  - Зависимости: Задача 001.77\n  - Статус: не начата",
            "  - Зависимости: Задача 001.77\n  - Статус: принята — коммит def5678 (2026-10-26)",
        )
        self.assertNotEqual(done, PLAN)
        text = pg.apply(done, pg.parse(done))
        self.assertIn("- [x] [R-21] Приём отчёта — задачи 77, 77b (приоритет M)", text)

    def test_a_dependency_on_an_unknown_part_is_refused(self) -> None:
        broken = PLAN.replace("Зависимости: Задача 001.77b", "Зависимости: Задача 001.77c")
        with self.assertRaisesRegex(pg.PlanError, "001.77c"):
            pg.parse(broken)


# От старта плана длиннее цепочка 001.10 → 001.12 (10 + 1 ч), но 001.10 принята; оставшаяся работа
# длиннее через 001.11 (3 + 1 ч).
REMAINING = """\
### Этап 1 — схема — 13 ч

- **Задача 001.10** — Каркас [STUB CREATION]
  - Приоритет: Critical
  - Оценка: 10 ч
  - Зависимости: нет
  - Статус: принята — коммит abc1234 (2026-10-25)

- **Задача 001.11** — Очередь [STUB CREATION]
  - Приоритет: High
  - Оценка: 3 ч
  - Зависимости: нет
  - Статус: не начата

### Этап 2 — кабинет — 1 ч

- **Задача 001.12** — Безопасность [LOGIC IMPLEMENTATION]
  - Приоритет: Critical
  - Оценка: 1 ч
  - Зависимости: Задача 001.10, Задача 001.11
  - Статус: не начата

<!-- generated:plan-status start -->
<!-- generated:plan-status end -->
"""


def gantt_rows(text: str) -> list[str]:
    """Секции и полосы диаграммы — строки после заголовка ``todayMarker off``."""
    block = text.split("  todayMarker off\n", 1)[1].split("```", 1)[0]
    return [line.strip() for line in block.splitlines()]


class RemainingWork(unittest.TestCase):
    def test_an_accepted_task_weighs_nothing_on_the_critical_path(self) -> None:
        hours, path = pg.critical_path(pg.parse(REMAINING), "001.12")
        self.assertEqual((hours, path), (4, ["001.11", "001.12"]))

    def test_the_path_line_counts_only_remaining_tasks(self) -> None:
        text = pg.render(pg.parse(REMAINING))
        self.assertIn(
            "Критический путь до задачи 001.12 по оставшейся работе (принятые задачи не входят) — "
            "4 ч по оценкам, задач в цепочке — 2: 11 → 12.",
            text.replace("\n", " "),
        )

    def test_the_gantt_draws_accepted_work_before_now(self) -> None:
        """Принятая задача — зелёная полоса от старта; оставшаяся работа — после отметки."""
        rows = gantt_rows(pg.render(pg.parse(REMAINING)))
        self.assertEqual(
            rows,
            [
                "section Состояние",
                "Сейчас — 10 ч, принято 1 из 3 :milestone, tnow, 10, 0ms",
                "section Этап 1 — схема",
                "10 Каркас FastAPI :done, t10, 0, 10ms",
                "11 Очередь — заглушки :active, crit, t11, after tnow, 3ms",
                "section Этап 2 — кабинет",
                "12 Безопасность — заглушки :crit, t12, after t11, 1ms",
            ],
        )

    def test_a_task_waiting_only_on_accepted_work_starts_now(self) -> None:
        """Отметка — конец самой длинной цепочки принятых (10 ч), а не сумма оценок (13 ч)."""
        done = REMAINING.replace(
            "  - Оценка: 3 ч\n  - Зависимости: нет\n  - Статус: не начата",
            "  - Оценка: 3 ч\n  - Зависимости: нет\n"
            "  - Статус: принята — коммит def5678 (2026-10-26)",
        )
        self.assertNotEqual(done, REMAINING)
        plan = pg.parse(done)
        self.assertEqual(pg.critical_path(plan, "001.12"), (1, ["001.12"]))
        self.assertEqual(pg.accepted_hours(plan), 10)
        rows = gantt_rows(pg.render(plan))
        self.assertEqual(
            rows,
            [
                "section Состояние",
                "Сейчас — 10 ч, принято 2 из 3 :milestone, tnow, 10, 0ms",
                "section Этап 1 — схема",
                "10 Каркас FastAPI :done, t10, 0, 10ms",
                "11 Очередь — заглушки :done, t11, 0, 3ms",
                "section Этап 2 — кабинет",
                "12 Безопасность — заглушки :active, crit, t12, after tnow, 1ms",
            ],
        )

    def test_a_fully_accepted_plan_has_no_remaining_path(self) -> None:
        """Пути нет, диаграмма остаётся: все полосы зелёные, принятая задача ждёт принятых
        зависимостей, отметка — в конце цепочки 001.10 → 001.12 (10 + 1 ч)."""
        done = REMAINING.replace(
            "Статус: не начата", "Статус: принята — коммит def5678 (2026-10-26)"
        )
        plan = pg.parse(done)
        self.assertEqual(pg.critical_path(plan, "001.12"), (0, []))
        text = pg.render(plan)
        self.assertIn("Критический путь до задачи 001.12: вся работа принята.", text)
        self.assertEqual(
            gantt_rows(text),
            [
                "section Состояние",
                "Сейчас — 11 ч, принято 3 из 3 :milestone, tnow, 11, 0ms",
                "section Этап 1 — схема",
                "10 Каркас FastAPI :done, t10, 0, 10ms",
                "11 Очередь — заглушки :done, t11, 0, 3ms",
                "section Этап 2 — кабинет",
                "12 Безопасность — заглушки :done, t12, after t10 t11, 1ms",
            ],
        )

    def test_without_accepted_work_the_diagram_starts_at_zero(self) -> None:
        todo = REMAINING.replace(
            "Статус: принята — коммит abc1234 (2026-10-25)", "Статус: не начата"
        )
        self.assertNotEqual(todo, REMAINING)
        rows = gantt_rows(pg.render(pg.parse(todo)))
        self.assertEqual(
            rows,
            [
                "section Этап 1 — схема",
                "10 Каркас FastAPI :active, crit, t10, 0, 10ms",
                "11 Очередь — заглушки :active, t11, 0, 3ms",
                "section Этап 2 — кабинет",
                "12 Безопасность — заглушки :crit, t12, after t10 t11, 1ms",
            ],
        )


if __name__ == "__main__":
    unittest.main()
