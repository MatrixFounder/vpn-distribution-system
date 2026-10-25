"""Тесты графа плана (``docs/scripts/plan_graph.py``): части разделённой задачи с буквенным
суффиксом (``001.77b``, WI-35) — запись, зависимость, критический путь и пункт чек-листа RTM."""

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
        self.assertEqual((hours, path), (6, ["001.77", "001.77b", "001.73"]))

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


if __name__ == "__main__":
    unittest.main()
