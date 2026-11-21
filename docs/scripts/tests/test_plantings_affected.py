"""Тесты отбора посадок по правилу 3 «Цикла задачи» (``docs/scripts/plantings_affected.py``,
WI-40): код вокруг образца по AST без докстрингов, ожидаемый тест с помощниками и фикстурами,
образец, журнал, загрузка посадок, снимок git."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plantings_affected as pa  # noqa: E402

GIT = shutil.which("git") or "git"

TREE = {
    "app/__init__.py": "",
    "app/mod.py": '''def guarded(x):
    """Вентиль."""
    # проверка знака
    return x > 0


def other():
    return 1


QUERY = (
    "select 1"
    " from t"
)
''',
    "config.conf": "first\nPATTERN\nlast\n",
    "tests/__init__.py": "",
    "tests/conftest.py": """import pytest


@pytest.fixture
def stand():
    return 1


@pytest.fixture(autouse=True)
def env():
    yield
""",
    "tests/helpers.py": """def helper():
    return 1
""",
    "tests/test_mod.py": """import pytest

from app.mod import guarded, other
from tests.helpers import helper

VALUES = (1, 2)


def local():
    return helper()


@pytest.mark.parametrize("v", VALUES)
def test_guard(stand, v):
    \"\"\"Вентиль.\"\"\"
    assert guarded(v) and local()


def test_other():
    assert other() == 1


class TestGroup:
    def test_one(self):
        assert True

    def test_two(self):
        assert True
""",
}

T = "tests/test_mod.py::"
PLANTS = [
    {"label": "P1", "file": "app/mod.py", "old": "return x > 0", "expect": [T + "test_guard[1]"]},
    {"label": "P2", "file": "app/mod.py", "old": '"select 1"', "expect": [T + "test_other"]},
    {"label": "P3", "file": "config.conf", "old": "PATTERN", "expect": [T + "TestGroup::test_one"]},
]


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class Selection(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base, self.cur = Path(tmp.name) / "base", Path(tmp.name) / "cur"
        write(self.base, TREE)
        write(self.cur, TREE)

    def edit(self, rel: str, old: str, new: str) -> None:
        path = self.cur / rel
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, f"образец правки {old!r} в {rel}")
        path.write_text(text.replace(old, new), encoding="utf-8")

    def reasons(self, measured: set[str] | None = None) -> dict[str, list[str]]:
        plantings = [pa.Planting(**p) for p in PLANTS]  # type: ignore[arg-type]
        verdicts = pa.compare(plantings, pa.Source(self.base), pa.Source(self.cur), ".", measured)
        return {v.label: v.reasons for v in verdicts}

    def test_an_unchanged_tree_leaves_every_planting_dry(self) -> None:
        self.assertEqual(self.reasons(), {"P1": [], "P2": [], "P3": []})

    def test_executable_code_around_the_pattern_is_a_change(self) -> None:
        self.edit("app/mod.py", "    return x > 0", "    x = abs(x)\n    return x > 0")
        self.assertEqual(self.reasons()["P1"], ["код guarded"])

    def test_docstrings_comments_and_formatting_are_not(self) -> None:
        self.edit("app/mod.py", '"""Вентиль."""', '"""Вентиль: знак аргумента."""')
        self.edit("app/mod.py", "# проверка знака", "# знак, а не модуль")
        self.edit("app/mod.py", "def guarded(x):\n", "def guarded(\n    x,\n):\n")
        self.edit("tests/test_mod.py", '"""Вентиль."""', '"""Вентиль: перечитан."""')
        self.assertEqual(self.reasons()["P1"], [])

    def test_another_function_of_the_file_is_not_around_the_pattern(self) -> None:
        self.edit("app/mod.py", "    return 1\n", "    return 2\n")
        self.assertEqual(self.reasons()["P1"], [])

    def test_a_module_assignment_is_the_block_of_its_pattern(self) -> None:
        self.edit("app/mod.py", '" from t"', '" from u"')
        self.assertEqual(self.reasons()["P2"], ["код QUERY"])
        self.assertEqual(self.reasons()["P1"], [])

    def test_a_pattern_missing_from_the_snapshot_is_new(self) -> None:
        write(self.base, {"app/mod.py": TREE["app/mod.py"].replace("x > 0", "x >= 0")})
        self.assertEqual(self.reasons()["P1"], ["образец новый или изменён"])

    def test_a_pattern_count_off_in_the_tree_fails_the_dry_check(self) -> None:
        self.edit("app/mod.py", "    return 1\n", "    return x > 0\n")
        plantings = [pa.Planting(**PLANTS[0])]  # type: ignore[arg-type]
        (verdict,) = pa.compare(plantings, pa.Source(self.base), pa.Source(self.cur), ".")
        self.assertTrue(verdict.mismatch)
        self.assertEqual(verdict.reasons, ["образец в дереве 2 раз вместо 1"])
        plants = self.cur / "plants.json"
        plants.write_text(json.dumps(PLANTS))
        args = ["--repo-root", str(self.cur), "compare", "--base", str(self.base)]
        out = io.StringIO()
        with redirect_stdout(out):
            code = pa.main([*args, "--plantings", str(plants), "--tests-root", "."])
        self.assertEqual(code, 1, "несовпавший образец — код 1")
        self.assertIn("образец не совпал: 1", out.getvalue())

    def test_the_test_body_is_part_of_the_expected_test(self) -> None:
        self.edit(
            "tests/test_mod.py", "assert guarded(v) and local()", "assert local() and guarded(v)"
        )
        self.assertEqual(self.reasons()["P1"], ["тест test_guard: изменено test_guard"])

    def test_helpers_of_the_same_file_and_of_test_modules_belong_to_it(self) -> None:
        self.edit("tests/test_mod.py", "    return helper()", "    return helper() + 0")
        self.assertIn("изменено local", self.reasons()["P1"][0])
        self.edit("tests/helpers.py", "    return 1", "    return 2")
        self.assertIn("helper", self.reasons()["P1"][0])

    def test_a_relative_import_of_a_test_module_is_followed(self) -> None:
        self.edit(
            "tests/test_mod.py", "from tests.helpers import helper", "from .helpers import helper"
        )
        write(self.base, {"tests/test_mod.py": (self.cur / "tests/test_mod.py").read_text()})
        self.edit("tests/helpers.py", "    return 1", "    return 2")
        self.assertEqual(self.reasons()["P1"], ["тест test_guard: изменено helper"])

    def test_fixtures_and_autouse_fixtures_belong_to_it(self) -> None:
        self.edit("tests/conftest.py", "    return 1", "    return 2")
        self.assertEqual(self.reasons()["P1"], ["тест test_guard: изменено stand"])
        self.edit("tests/conftest.py", "    yield", "    yield None")
        self.assertIn("env", self.reasons()["P1"][0])
        self.assertIn("env", self.reasons()["P2"][0])  # автоматическая — у каждого теста

    def test_application_code_is_not_followed_from_the_test(self) -> None:
        self.edit("app/mod.py", "    return 1\n", "    return 2\n")
        self.assertEqual(self.reasons()["P2"], [])  # other() — код приложения, не помощник

    def test_a_parametrization_value_is_part_of_the_test(self) -> None:
        self.edit("tests/test_mod.py", "VALUES = (1, 2)", "VALUES = (1, 2, 3)")
        self.assertEqual(self.reasons()["P1"], ["тест test_guard: изменено VALUES"])
        # Значения прямо в декораторе: декоратор — часть формы теста, а не только ссылки из него.
        inline = TREE["tests/test_mod.py"].replace(
            '@pytest.mark.parametrize("v", VALUES)', '@pytest.mark.parametrize("v", (1, 2))'
        )
        write(self.base, {"tests/test_mod.py": inline})
        write(self.cur, {"tests/test_mod.py": inline.replace("(1, 2))", "(1, 2, 3))")})
        self.assertEqual(self.reasons()["P1"], ["тест test_guard: изменено test_guard"])

    def test_a_class_test_is_compared_as_its_class(self) -> None:
        self.edit(
            "tests/test_mod.py",
            "    def test_two(self):\n        assert True",
            "    def test_two(self):\n        assert 1",
        )
        self.assertEqual(self.reasons()["P3"], ["тест TestGroup::test_one: изменено TestGroup"])

    def test_a_non_python_file_is_compared_whole(self) -> None:
        self.edit("config.conf", "last", "final")
        self.assertEqual(self.reasons()["P3"], ["файл config.conf изменён (не Python — целиком)"])

    def test_a_planting_absent_from_the_run_log_is_measured(self) -> None:
        reasons = self.reasons(measured={"P1"})
        self.assertEqual((reasons["P1"], reasons["P2"]), ([], ["не измерялась"]))


class Loading(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_a_harness_module_supplies_its_plants_list(self) -> None:
        harness = self.dir / "harness.py"
        harness.write_text(
            f"PLANTS = {PLANTS!r}\nif __name__ == '__main__':\n    raise SystemExit(9)\n"
        )
        self.assertEqual([p.label for p in pa.load_plantings(harness)], ["P1", "P2", "P3"])

    def test_a_json_file_supplies_a_list_or_a_plantings_object(self) -> None:
        listed, wrapped = self.dir / "a.json", self.dir / "b.json"
        listed.write_text(json.dumps(PLANTS))
        wrapped.write_text(json.dumps({"plantings": PLANTS}))
        self.assertEqual(len(pa.load_plantings(listed)), len(pa.load_plantings(wrapped)))

    def test_a_malformed_planting_is_refused(self) -> None:
        bad = self.dir / "bad.json"
        for broken in ({**PLANTS[0], "expect": "tests/x.py::t"}, {**PLANTS[0], "old": ""}):
            bad.write_text(json.dumps([broken]))
            with self.assertRaises(pa.InputError):
                pa.load_plantings(bad)
        bad.write_text(json.dumps([PLANTS[0], PLANTS[0]]))
        with self.assertRaises(pa.InputError):
            pa.load_plantings(bad)  # метки повторяются


class Snapshot(unittest.TestCase):
    def git(self, *args: str) -> str:
        # Аргументы — литералы теста, без оболочки.
        return subprocess.run(  # noqa: S603
            [GIT, "-C", str(self.repo), *args], capture_output=True, text=True, check=True
        ).stdout

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "test")
        write(self.repo, {**TREE, ".gitignore": "ignored/\n"})
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")

    def test_the_snapshot_holds_untracked_files_and_leaves_the_index_alone(self) -> None:
        write(self.repo, {"tests/new_helper.py": "X = 1\n", "ignored/junk.txt": "x\n"})
        self.git("add", "app/mod.py")
        (self.repo / "app/mod.py").write_text(TREE["app/mod.py"] + "\n# правка\n")
        status = self.git("status", "--porcelain")
        tree = pa.snapshot(self.repo)
        self.assertEqual(self.git("status", "--porcelain"), status)
        files = self.git("ls-tree", "-r", "--name-only", tree).split()
        self.assertIn("tests/new_helper.py", files)
        self.assertNotIn("ignored/junk.txt", files)
        self.assertIn("# правка", pa.Source(self.repo, tree).read("app/mod.py") or "")

    def test_compare_against_a_snapshot_tree_from_the_command_line(self) -> None:
        plants = self.repo / "ignored" / "plants.json"
        plants.parent.mkdir(exist_ok=True)
        plants.write_text(json.dumps(PLANTS))
        tree = pa.snapshot(self.repo)
        text = (self.repo / "app/mod.py").read_text()
        (self.repo / "app/mod.py").write_text(
            text.replace("    return x > 0", "    x = -x\n    return x > 0")
        )
        out, err = io.StringIO(), io.StringIO()
        args = ["--repo-root", str(self.repo), "compare", "--base", tree]
        args += ["--plantings", str(plants), "--tests-root", ".", "--json"]
        with redirect_stdout(out), redirect_stderr(err):
            code = pa.main(args)
        self.assertEqual(code, 0, err.getvalue())
        rows = {r["label"]: r["reasons"] for r in json.loads(out.getvalue())}
        self.assertEqual(rows, {"P1": ["код guarded"], "P2": [], "P3": []})

    def test_a_base_that_is_neither_a_directory_nor_a_tree_is_a_usage_error(self) -> None:
        plants = self.repo / "ignored" / "plants.json"
        plants.parent.mkdir(exist_ok=True)
        plants.write_text(json.dumps(PLANTS))
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = pa.main(
                ["--repo-root", str(self.repo), "compare", "--base", "no-such-ref"]
                + ["--plantings", str(plants)]
            )
        self.assertEqual(code, 2)
        self.assertIn("не каталог и не дерево git", err.getvalue())


if __name__ == "__main__":
    unittest.main()
