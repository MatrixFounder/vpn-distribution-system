"""Тесты харнесса посадок (``docs/scripts/plantings_run.py``, WI-42). Вход — репозиторий git с
каталогом прогона ``proj`` (модули ``calc``, ``fmt``, ``web``, ``cli``, тесты pytest с
фикстурой и подпроцессом), неотслеживаемые файлы и модуль посадок рядом с репозиторием; прогоны —
настоящий pytest интерпретатора тестов с плагином ``plantings_probe.py`` и наблюдателем
``plantings_watch.py``. Вердикт — решение заказчика 2026-11-26: запись ``expect`` красная, если
её провалы — в теле теста на проверке или объявленном исключении, и код прогона не перехватил
новой необъявленной ошибки самой посадки."""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plantings_affected as pa  # noqa: E402
import plantings_run as pr  # noqa: E402

GIT = shutil.which("git") or "git"
HARNESS = Path(pr.__file__).resolve()

TREE = {
    ".gitignore": "__pycache__/\n*.local\n",
    ".gitattributes": "proj/export.txt export-ignore\nproj/*.crlf text eol=crlf\n",
    "proj/pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
    "proj/export.txt": "в копии, хотя export-ignore\n",
    "proj/lines.crlf": "a\nb\n",
    "proj/calc.py": textwrap.dedent(
        """\
        def sign(x):
            return 1 if x > 0 else -1


        def zero_sign():
            return 0


        def double(x):
            return x * 2


        def slow():
            return 1


        def mode():
            return "clean"


        def checked(x):
            if x < 0:
                raise ValueError("отрицательное")
            return x
        """
    ),
    "proj/fmt.py": textwrap.dedent(
        """\
        from calc import double


        def show(x):
            return f"{double(x)}"
        """
    ),
    "proj/web.py": textwrap.dedent(
        """\
        class SqlError(Exception):
            sqlstate = "42P18"


        class PrivilegeError(Exception):
            sqlstate = "42501"


        def legacy():
            try:
                return undefined_legacy  # noqa: F821 — перехватывается и без посадки
            except NameError:
                return 0


        def compute(x):
            return x + 1


        def handle(x):
            legacy()
            try:
                return 200, compute(x)
            except Exception:
                return 500, None
        """
    ),
    "proj/cli.py": textwrap.dedent(
        """\
        import sys

        from web import handle

        if __name__ == "__main__":
            sys.exit(0 if handle(1)[0] == 200 else 1)
        """
    ),
    "proj/tests/conftest.py": textwrap.dedent(
        """\
        import pytest
        from calc import sign


        @pytest.fixture
        def checked():
            assert sign(-1) == -1
            return True
        """
    ),
    "proj/tests/test_calc.py": textwrap.dedent(
        """\
        import pytest
        from calc import double, sign, slow, zero_sign


        def test_sign():
            assert sign(5) == 1 and sign(-5) == -1


        def test_sign_of_zero():
            assert zero_sign() == 0


        def test_double():
            assert double(2) == 4


        @pytest.mark.parametrize("x", [1, 2])
        def test_param(x):
            assert double(x) == 2 * x


        def test_slow():
            assert slow() == 1


        def test_skipped():
            pytest.skip("нет окружения")


        def test_with_fixture(checked):
            assert checked


        def test_refuses_negative():
            from calc import checked as check

            with pytest.raises(ValueError):
                check(-1)


        def test_fails_explicitly():
            if sign(0) != -1:
                pytest.fail("ноль — не отрицательный знак")
        """
    ),
    "proj/tests/test_fmt.py": textwrap.dedent(
        """\
        from fmt import show


        def test_show():
            assert show(2) == "4"
        """
    ),
    "proj/tests/test_web.py": textwrap.dedent(
        """\
        import os
        import subprocess
        import sys
        from pathlib import Path

        from web import handle


        def test_handle():
            assert handle(1) == (200, 2)


        def test_cli():
            run = subprocess.run(
                [sys.executable, "cli.py"], cwd=Path(__file__).parents[1], env=dict(os.environ)
            )
            assert run.returncode == 0
        """
    ),
    "proj/tests/test_stand.py": textwrap.dedent(
        """\
        import os
        from pathlib import Path

        from calc import mode


        def test_clean():
            stand = Path(os.environ["PLANT_STAND"])
            assert not stand.exists() or stand.read_text() != "dirty"


        def test_touch():
            Path(os.environ["PLANT_STAND"]).write_text(mode())
        """
    ),
    "proj/tests/test_broken.py": "def test_broken():\n    assert False\n",
    "proj/tests/test_badimport.py": "import no_such_module_xyz  # noqa: F401\n",
    "other/readme.txt": "вне COPY\n",
    # Свой plantings_probe.py в каталоге прогона (так у docs/scripts): плагин харнесса — не он.
    "proj/plantings_probe.py": "raise RuntimeError('не плагин харнесса')\n",
}
# Неотслеживаемые файлы: копия — снимок рабочей копии, а не HEAD.
UNTRACKED = {
    "proj/data.txt": "1\n",
    "proj/tests/test_data.py": textwrap.dedent(
        """\
        from pathlib import Path


        def test_data():
            assert Path("data.txt").read_text() == "1\\n"
        """
    ),
}

CALC = "proj/calc.py"
FMT = "proj/fmt.py"
WEB = "proj/web.py"
T = "tests/test_calc.py::"
W = "tests/test_web.py::"
ALL = ["tests/test_calc.py", "tests/test_fmt.py", "tests/test_data.py", "tests/test_web.py"]

HEADER = textwrap.dedent(
    f"""\
    from pathlib import Path

    COPY = ["proj"]
    RUN_DIR = "proj"
    PYTHON = {sys.executable!r}
    FULL = {ALL!r}
    TIMEOUT = 60
    SUBSETS = {{
        "calc": ["tests/test_calc.py"],
        "all": {ALL!r},
        "web": ["tests/test_web.py"],
        "broken": ["tests/test_broken.py"],
        "badimport": ["tests/test_badimport.py"],
    }}
    HOOKS = Path(__file__).with_name("hooks.jsonl")


    def note(ctx):
        with HOOKS.open("a", encoding="utf-8") as fh:
            fh.write(ctx.label + " " + ctx.verdict + " " + str(ctx.copy.is_dir()) + "\\n")
        return "стенд на месте"


    def fail(ctx):
        raise RuntimeError("стенд не восстановлен")


    def capture(ctx):
        files = ("proj/calc.py", "proj/fmt.py")
        text = "".join((ctx.copy / f).read_text(encoding="utf-8") for f in files)
        out = Path(__file__).with_name("captured-" + ctx.label + ".txt")
        out.write_text(text, encoding="utf-8")


    def clean_stand(ctx):
        Path(ctx.env["PLANT_STAND"]).write_text("clean")
        with HOOKS.open("a", encoding="utf-8") as fh:
            fh.write(ctx.label + " " + ctx.verdict + "\\n")

    """
)


def planting(label: str, old: str, new: str, expect: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "label": label,
        "what": label,
        "file": CALC,
        "old": old,
        "new": new,
        "expect": expect,
        "subset": "calc",
        **extra,
    }


def web(label: str, new: str, expect: list[str], **extra: Any) -> dict[str, Any]:
    return {
        **planting(label, "    return x + 1\n", new, expect, subset="web", **extra),
        "file": WEB,
    }


SIGN = "return 1 if x > 0 else -1"
DOUBLE = "return x * 2"
SLOW = "def slow():\n    return 1"
SHOW = 'return f"{double(x)}"'

RED = planting("red", SIGN, "return 1", [T + "test_sign"])
EXC = planting("exc", DOUBLE, "return x * undefined_name", [T + "test_double"])
RAISES = planting(
    "raises", DOUBLE, "return x * undefined_name", [T + "test_double"], raises=["NameError"]
)
BASE = planting(
    "base", DOUBLE, "return x // 0", [T + "test_double"], raises=["ArithmeticError"]
)  # ZeroDivisionError: объявлен базовый класс — ищется по MRO
PARTIAL = planting(
    "partial", DOUBLE, "return 5 if x == 2 else x * 2", [T + "test_double", T + "test_param[1]"]
)
PARAMS = planting("params", DOUBLE, "return 5 if x == 2 else x * 2", [T + "test_param"])
MIXED = planting(  # test_param[1] — NameError, test_param[2] — проверка
    "mixed", DOUBLE, "return 5 if x == 2 else x * undefined_name", [T + "test_param"]
)
COLLECT = planting("collect", "def double(x):", "def doubled(x):", [T + "test_double"])
OTHER = {**planting("other", SHOW, 'return f"{double(x)}!"', [T + "test_double"]), "file": FMT}
GREEN = planting("green", SLOW, "def slow():\n    return 1  # то же поведение", [T + "test_slow"])
DATA = {
    **planting("data", "1\n", "2\n", ["tests/test_data.py::test_data"], subset="all"),
    "file": "proj/data.txt",
}
MULTI = planting(
    "multi",
    DOUBLE,
    "return x * 3",
    ["tests/test_fmt.py::test_show"],
    subset="all",
    extra=[{"file": FMT, "old": SHOW, "new": 'return f"{double(x)}?"'}],
    after="capture",
)
SAME = planting(  # две правки одного файла: extra без file — файл посадки
    "same",
    SIGN,
    "return 1",
    [T + "test_sign", T + "test_double"],
    extra=[{"old": DOUBLE, "new": "return x * 3"}],
    after="capture",
)
PREFIX = planting(  # упал только test_sign_of_zero: test_sign — не его префикс
    "prefix", "def zero_sign():\n    return 0", "def zero_sign():\n    return 1", [T + "test_sign"]
)
WHOLE = {
    **planting("whole", SHOW, 'return f"{double(x)}!"', ["tests/test_fmt.py"], subset="all"),
    "file": FMT,
}
SETUP = planting("setup", SIGN, "return 1", [T + "test_with_fixture"])
# Провалы pytest: «DID NOT RAISE» у pytest.raises и pytest.fail — проверка (их класс pytest
# объявляет в модуле builtins).
NO_RAISE = planting(
    "no_raise",
    '        raise ValueError("отрицательное")',
    "        pass",
    [T + "test_refuses_negative"],
)
EXPLICIT = planting("explicit", SIGN, "return 1 if x > 0 else 0", [T + "test_fails_explicitly"])
ELSEWHERE = planting(  # ошибка сбора test_fmt.py не останавливает прогон test_sign
    "elsewhere",
    SIGN,
    "return 1",
    [T + "test_sign"],
    subset="all",
    extra=[{"file": FMT, "old": "from calc import double", "new": "from calc import nothing"}],
)
SWALLOW = web("swallow", "    return x + undefined_name\n", [W + "test_handle"])
DECLARED = web(
    "declared", "    return x + undefined_name\n", [W + "test_handle"], raises=["NameError"]
)
SQL = web("sql", "    raise SqlError()\n", [W + "test_handle"])
PRIVILEGE = web("privilege", "    raise PrivilegeError()\n", [W + "test_handle"])
LEGIT = web("legit", "    return x + 2\n", [W + "test_handle"])
CLI = web("cli", "    return x + undefined_name\n", [W + "test_cli"])
HOOKED = planting("hooked", SIGN, "return 1", [T + "test_sign"], after="note")
HOOK_FAILS = planting("hook_fails", SIGN, "return 1", [T + "test_sign"], after="fail")
BROKEN = planting("b", SIGN, "return 1", ["tests/test_broken.py::test_broken"], subset="broken")
ORPHAN = "plantings-orphan-"
HANG_LIFETIME = 180  # с, больше предела самого долгого сценария (120 с)


def hanging(
    mark: str, *, stubborn: bool = False, deaf_child: bool = False, timeout: float = 3
) -> dict[str, Any]:
    """Посадка, которая зависает и оставляет дочерний процесс с меткой ``mark`` в командной
    строке; ``stubborn`` — и pytest, и ребёнок не выходят по TERM (как воркеры проекта со своим
    обработчиком SIGTERM); ``deaf_child`` — не выходит только ребёнок."""
    deaf = stubborn or deaf_child
    ignore = "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); " if deaf else ""
    child = f"{ignore}import time; time.sleep(120)"
    lines = ["def slow():", "    import signal, subprocess, sys"]
    lines.append(f"    subprocess.Popen([sys.executable, '-c', {child!r}, {mark!r}])")
    if stubborn:
        lines.append("    signal.signal(signal.SIGTERM, signal.SIG_IGN)")
    # Занятый цикл ограничен сроком: посадки проверяют сторожа и снятие группы, выключая их, и
    # тогда зависший pytest переживает тест. Без срока он часами держал ядро (RF-5, 2026-11-26).
    lines += ["    import time", f"    end = time.monotonic() + {HANG_LIFETIME}"]
    lines += ["    while time.monotonic() < end:", "        pass"]
    return planting("hang", SLOW, "\n".join(lines), [T + "test_slow"], timeout=timeout)


def processes() -> str:
    # Фиксированные аргументы, без оболочки.
    return subprocess.run(  # noqa: S603
        ["/bin/ps", "-axo", "command"], capture_output=True, text=True, check=True
    ).stdout


def wait_for(predicate: Any, limit: float) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return bool(predicate())


class Harness(unittest.TestCase):
    def git(self, *args: str) -> str:
        # Аргументы — литералы теста, без оболочки.
        return subprocess.run(  # noqa: S603
            [GIT, "-C", str(self.repo), *args], capture_output=True, text=True, check=True
        ).stdout

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.repo, self.aside, self.work = base / "repo", base / "aside", base / "work"
        self.aside.mkdir()
        for rel, text in TREE.items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "t")
        for rel, text in UNTRACKED.items():
            (self.repo / rel).write_text(text, encoding="utf-8")
        self.log = self.aside / "runs.jsonl"
        self.stand = self.aside / "stand.txt"
        env = mock.patch.dict(os.environ, {"PLANT_STAND": str(self.stand)})
        env.start()
        self.addCleanup(env.stop)

    def harness(self, plants: list[dict[str, Any]], extra: str = "") -> Path:
        path = self.aside / "plants.py"
        path.write_text(
            HEADER + extra + "\nPLANTS = " + json.dumps(plants, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    def main(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pr.main(["--repo-root", str(self.repo), *args])
        return code, out.getvalue(), err.getvalue()

    def run_plants(
        self, plants: list[dict[str, Any]], *labels: str, extra: str = "", **flags: str
    ) -> int:
        path = self.harness(plants, extra)
        chosen = list(labels) or ["--all"]
        options = [f"--{k.replace('_', '-')}" for k, v in flags.items() if v]
        code, out, err = self.main(
            "run", "--plantings", str(path), "--log", str(self.log), "--work", str(self.work),
            *options, *chosen,
        )  # fmt: skip
        self.output = out + err
        return code

    def rows(self) -> dict[str, dict[str, Any]]:
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return {r["label"]: r for r in rows}

    def verdicts(self) -> dict[str, str]:
        return {label: row["verdict"] for label, row in self.rows().items()}

    def spawn(self, plants: list[dict[str, Any]]) -> subprocess.Popen[str]:
        """Харнесс отдельным процессом — для сигналов и зависаний, которые повесили бы тест."""
        path = self.harness(plants)
        cmd = [
            sys.executable, str(HARNESS), "--repo-root", str(self.repo), "run",
            "--plantings", str(path), "--log", str(self.log), "--work", str(self.work), "--all",
        ]  # fmt: skip
        # Аргументы — список без оболочки: пути теста.
        proc = subprocess.Popen(  # noqa: S603
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

        def stop() -> None:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()

        self.addCleanup(stop)
        return proc


class Verdicts(Harness):
    def test_each_verdict_on_a_real_pytest_run(self) -> None:
        plants = [
            RED, EXC, RAISES, BASE, PARTIAL, PARAMS, MIXED, COLLECT, OTHER, GREEN, DATA, MULTI,
            SAME, PREFIX, WHOLE, SETUP, ELSEWHERE, NO_RAISE, EXPLICIT,
        ]  # fmt: skip
        self.assertEqual(self.run_plants(plants), 1, self.output)
        self.assertEqual(
            self.verdicts(),
            {
                "red": "RED",
                "exc": "EXCEPTION",
                "raises": "RED",
                "base": "RED",
                "partial": "PARTIAL",
                "params": "RED",
                "mixed": "EXCEPTION",
                "collect": "COLLECT",
                "other": "OTHER",
                "green": "GREEN",
                "data": "RED",
                "multi": "RED",
                "same": "RED",
                "prefix": "OTHER",
                "whole": "RED",
                "setup": "EXCEPTION",
                "elsewhere": "RED",
                "no_raise": "RED",
                "explicit": "RED",
            },
            self.output,
        )
        rows = self.rows()
        self.assertIn("builtins.NameError", rows["exc"]["detail"])
        self.assertIn(f"{T}test_param[1]: builtins.NameError", rows["mixed"]["detail"])
        self.assertIn("builtins.AssertionError в setup", rows["setup"]["detail"])
        self.assertIn(T + "test_param[1] (green)", rows["partial"]["detail"])
        self.assertEqual((rows["other"]["scope"], rows["green"]["scope"]), ("full", "full"))
        self.assertIn("tests/test_fmt.py::test_show", rows["other"]["detail"])
        self.assertIn(T + "test_sign_of_zero", rows["prefix"]["detail"])
        self.assertEqual(rows["red"]["scope"], "subset")
        self.assertEqual(rows["red"]["args"], ["tests/test_calc.py"])
        multi = (self.aside / "captured-multi.txt").read_text(encoding="utf-8")
        self.assertIn("return x * 3", multi, "основная правка")
        self.assertIn('return f"{double(x)}?"', multi, "правка extra в другом файле")
        same = (self.aside / "captured-same.txt").read_text(encoding="utf-8")
        self.assertIn("return x * 3", same, "правка extra в файле посадки")
        self.assertIn("    return 1\n", same, "основная правка того же файла")

    def test_an_error_swallowed_by_the_code_is_not_a_check(self) -> None:
        plants = [SWALLOW, DECLARED, SQL, PRIVILEGE, LEGIT, CLI]
        self.assertEqual(self.run_plants(plants), 1, self.output)
        self.assertEqual(
            self.verdicts(),
            {
                "swallow": "EXCEPTION",
                "declared": "RED",
                "sql": "EXCEPTION",
                "privilege": "RED",
                "legit": "RED",
                "cli": "EXCEPTION",
            },
            self.output,
        )
        rows = self.rows()
        swallowed = "в коде прогона builtins.NameError @ web.py:compute"
        self.assertIn(swallowed, rows["swallow"]["detail"])
        self.assertIn("web.SqlError @ web.py:compute", rows["sql"]["detail"])
        self.assertIn(swallowed, rows["cli"]["detail"], "подпроцесс теста")

    def test_all_red_is_exit_zero_and_every_row_feeds_compare(self) -> None:
        self.assertEqual(self.run_plants([RED, DATA]), 0, self.output)
        plantings = pa.load_plantings(self.aside / "plants.py")
        verdicts = pa.compare(plantings, self.repo, "proj", pa.load_log(self.log))
        self.assertEqual({v.label: v.reasons for v in verdicts}, {"red": [], "data": []})
        tree = pa.snapshot(self.repo)
        self.assertEqual({r["tree"] for r in self.rows().values()}, {tree})
        probe_path = self.aside / "runs-out/red/subset.probe.json"
        probe = json.loads(probe_path.read_text(encoding="utf-8"))
        self.assertIn("calc", probe["modules"], "плагин не записал модули прогона")
        copy = str(self.work / "run" / "proj")
        self.assertTrue(probe["modules"]["calc"].startswith(copy), probe["modules"]["calc"])
        self.assertEqual({r["spec"] for r in self.rows().values()}, {p.spec for p in plantings})

    def test_full_re_measure_is_skipped_on_request(self) -> None:
        self.assertEqual(self.run_plants([GREEN], no_full="1"), 1, self.output)
        row = self.rows()["green"]
        self.assertEqual((row["verdict"], row["scope"]), ("GREEN", "subset"))

    def test_pytest_addopts_of_the_environment_is_not_passed(self) -> None:
        with mock.patch.dict(os.environ, {"PYTEST_ADDOPTS": "--collect-only"}):
            self.assertEqual(self.run_plants([RED]), 0, self.output)

    def test_variables_of_an_outer_run_do_not_leak_into_the_run(self) -> None:
        outer = {  # харнесс под харнессом: окружение внешнего прогона
            "PLANTINGS_PROBE_PARENT": "1",
            "PLANTINGS_PROBE_OUT": str(self.aside / "outer.json"),
            "PLANTINGS_PROBE_ROOT": str(self.aside),
            "PLANTINGS_PROBE_RAISES": str(self.aside / "outer"),
            "PLANTINGS_PROBE_TEST": "outer::test",
        }
        with mock.patch.dict(os.environ, outer):
            self.assertEqual(self.run_plants([RED, SWALLOW]), 1, self.output)
        self.assertEqual(self.verdicts(), {"red": "RED", "swallow": "EXCEPTION"})
        self.assertFalse((self.aside / "outer.json").exists())

    def test_a_stale_probe_report_is_not_reused(self) -> None:
        self.assertEqual(self.run_plants([RED]), 0, self.output)
        dying = planting("red", SIGN, "import os; os._exit(1)", [T + "test_sign"])
        self.assertEqual(self.run_plants([dying]), 1, self.output)
        self.assertEqual(self.verdicts(), {"red": "ERROR"})


class Limits(Harness):
    def test_a_run_past_its_limit_is_a_timeout_and_its_processes_are_gone(self) -> None:
        mark = ORPHAN + uuid.uuid4().hex
        started = time.monotonic()
        self.assertEqual(self.run_plants([hanging(mark)]), 1, self.output)
        self.assertEqual(self.verdicts(), {"hang": "TIMEOUT"})
        self.assertLess(time.monotonic() - started, 40)
        self.assertNotIn(mark, processes(), "дочерний процесс зависшего прогона снят")

    def test_processes_deaf_to_term_are_killed(self) -> None:
        mark = ORPHAN + uuid.uuid4().hex
        proc = self.spawn([hanging(mark, stubborn=True)])
        try:
            output, _ = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("харнесс не снял pytest, глухой к TERM")
        self.assertEqual(proc.returncode, 1, output)
        self.assertEqual(self.verdicts(), {"hang": "TIMEOUT"})
        self.assertTrue(wait_for(lambda: mark not in processes(), 5), "ребёнок, глухой к TERM")

    def test_a_child_deaf_to_term_is_killed_after_pytest_exits(self) -> None:
        mark = ORPHAN + uuid.uuid4().hex
        self.assertEqual(self.run_plants([hanging(mark, deaf_child=True)]), 1, self.output)
        self.assertEqual(self.verdicts(), {"hang": "TIMEOUT"})
        self.assertNotIn(mark, processes(), "ребёнок пережил pytest, вышедший по TERM")

    def test_a_terminated_harness_takes_pytest_with_it(self) -> None:
        mark = ORPHAN + uuid.uuid4().hex
        proc = self.spawn([hanging(mark, timeout=120)])
        self.assertTrue(wait_for(lambda: mark in processes(), 60), "посадка не запустилась")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=30), 128 + signal.SIGTERM)
        self.assertTrue(wait_for(lambda: mark not in processes(), 5), "pytest пережил харнесс")

    def test_a_killed_harness_does_not_leave_pytest_running(self) -> None:
        mark = ORPHAN + uuid.uuid4().hex
        proc = self.spawn([hanging(mark, timeout=120)])
        self.assertTrue(wait_for(lambda: mark in processes(), 60), "посадка не запустилась")
        proc.kill()
        proc.wait(timeout=10)
        self.assertTrue(wait_for(lambda: mark not in processes(), 10), "сторож плагина")


class Baseline(Harness):
    def test_a_failing_subset_stops_before_any_planting_every_time(self) -> None:
        for _ in range(2):
            self.assertEqual(self.run_plants([BROKEN]), 3, self.output)
            self.assertIn("упал tests/test_broken.py::test_broken", self.output)
            self.assertFalse(self.log.exists())

    def test_each_subset_has_its_own_baseline(self) -> None:
        self.assertEqual(self.run_plants([RED, BROKEN]), 3, self.output)
        self.assertFalse(self.log.exists())

    def test_a_collection_error_in_the_subset_stops(self) -> None:
        bad = planting("bad", SIGN, "return 1", ["tests/test_badimport.py"], subset="badimport")
        self.assertEqual(self.run_plants([bad]), 3, self.output)
        self.assertIn("ошибка сбора tests/test_badimport.py", self.output)

    def test_an_expected_test_absent_from_the_subset_stops(self) -> None:
        code = self.run_plants([planting("m", SIGN, "return 1", [T + "test_nope"])])
        self.assertEqual(code, 3, self.output)
        self.assertIn(f"expect {T}test_nope: таких тестов в подмножестве нет", self.output)

    def test_an_expected_test_skipped_without_a_planting_stops(self) -> None:
        skipped = planting("s", SIGN, "return 1", [T + "test_skipped"])
        self.assertEqual(self.run_plants([RED, skipped]), 3, self.output)
        self.assertIn("пропущен без посадки", self.output)
        self.assertFalse(self.log.exists(), "ожидаемые проверены до первой посадки")

    def test_the_full_set_has_a_baseline_before_a_re_measure(self) -> None:
        extra = f"FULL = {[*ALL, 'tests/test_broken.py']!r}\n"
        self.assertEqual(self.run_plants([GREEN], extra=extra), 3, self.output)
        self.assertIn("упал tests/test_broken.py::test_broken", self.output)

    def test_a_timeout_re_runs_the_baseline_of_the_next_planting(self) -> None:
        code = self.run_plants([hanging(ORPHAN + uuid.uuid4().hex), RED])
        self.assertEqual(code, 1, self.output)
        self.assertEqual(self.output.count("база   tests/test_calc.py"), 2, self.output)
        self.assertEqual(self.verdicts(), {"hang": "TIMEOUT", "red": "RED"})

    def test_the_baseline_lives_one_run(self) -> None:
        self.assertEqual(self.run_plants([RED, DATA]), 0, self.output)
        self.assertEqual(self.output.count("база"), 2, self.output)
        self.assertEqual(self.run_plants([RED]), 0, self.output)
        self.assertEqual(self.output.count("база"), 1, self.output)


class Hooks(Harness):
    def test_the_after_hook_sees_the_verdict_and_the_copy(self) -> None:
        self.assertEqual(self.run_plants([HOOKED]), 0, self.output)
        calls = self.aside / "hooks.jsonl"
        self.assertTrue(calls.is_file(), "хук не вызван")
        self.assertEqual(calls.read_text(encoding="utf-8"), "hooked RED True\n")
        hooks = self.log.with_name("runs.hooks.jsonl").read_text(encoding="utf-8")
        self.assertEqual(json.loads(hooks)["note"], "стенд на месте")

    def test_a_failing_hook_stops_the_run(self) -> None:
        code = self.run_plants([HOOK_FAILS, RED])
        self.assertEqual(code, 3, self.output)
        self.assertIn("стенд не восстановлен", self.output)
        self.assertFalse(self.log.exists(), "строки посадки с упавшим хуком нет, red не мерилась")

    def test_the_hook_runs_after_a_timeout(self) -> None:
        hooked = {**hanging(ORPHAN + uuid.uuid4().hex), "after": "note"}
        self.assertEqual(self.run_plants([hooked]), 1, self.output)
        calls = self.aside / "hooks.jsonl"
        self.assertTrue(calls.is_file(), "хук после предела времени не вызван")
        self.assertEqual(calls.read_text(encoding="utf-8"), "hang TIMEOUT True\n")

    def test_the_hook_repairs_the_stand_before_the_full_baseline(self) -> None:
        touch = "tests/test_stand.py::test_touch"
        dirty = planting(
            "dirty",
            'return "clean"',
            'return "dirty"',
            [touch],
            subset=[touch],
            after="clean_stand",
        )
        extra = "FULL = ['tests/test_stand.py']\n"
        self.assertEqual(self.run_plants([dirty], extra=extra), 1, self.output)
        self.assertEqual(self.verdicts(), {"dirty": "GREEN"})
        calls = (self.aside / "hooks.jsonl").read_text(encoding="utf-8")
        self.assertEqual(calls, "dirty GREEN\ndirty GREEN\n", "хук после каждого прогона")


class Selection(Harness):
    def test_affected_measures_only_what_compare_selects(self) -> None:
        self.assertEqual(self.run_plants([RED, DATA]), 0, self.output)
        path = self.aside / "plants.py"
        args = ["run", "--plantings", str(path), "--log", str(self.log), "--affected"]
        code, out, _ = self.main(*args)
        self.assertEqual(
            (code, out.strip()),
            (0, "перемерить: 0\nсухо: 2 — red data\nобразец не совпал: 0\nпосадок к прогону нет"),
        )
        calc = self.repo / CALC
        calc.write_text(
            calc.read_text(encoding="utf-8").replace(SIGN, "y = x\n    " + SIGN), encoding="utf-8"
        )
        code, out, err = self.main(*args)
        self.assertEqual(code, 0, out + err)
        self.assertRegex(out, r"\n  red +код sign\n")
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["label"] for line in lines], ["red", "data", "red"])

    def test_labels_are_process_arguments(self) -> None:
        self.assertEqual(self.run_plants([RED, DATA], "data"), 0, self.output)
        self.assertEqual(list(self.verdicts()), ["data"])

    def test_an_unknown_label_is_bad_input(self) -> None:
        self.assertEqual(self.run_plants([RED], "nope"), 2, self.output)
        self.assertIn("nope", self.output)

    def dry(self, plants: list[dict[str, Any]]) -> tuple[int, str]:
        code, out, _ = self.main("dry", "--plantings", str(self.harness(plants)))
        return code, out

    def test_a_stale_pattern_fails_dry_and_run_before_measuring(self) -> None:
        path = self.harness([RED, planting("stale", "return 7", "return 8", [T + "test_sign"])])
        code, out, _ = self.main("dry", "--plantings", str(path))
        self.assertEqual(code, 1)
        self.assertIn("образец не совпал: stale: правка 1 (proj/calc.py): образец 0 раз", out)
        self.assertIn("сухо: 1 из 2", out)
        code, _, err = self.main("run", "--plantings", str(path), "--log", str(self.log), "--all")
        self.assertEqual(code, 1, err)
        self.assertFalse(self.log.exists())

    def test_dry_checks_every_edit_and_its_count(self) -> None:
        stale_extra = {**RED, "extra": [{"file": FMT, "old": "return 7", "new": "return 8"}]}
        self.assertEqual(self.dry([stale_extra])[0], 1)
        twice = planting("twice", "return", "yield", [T + "test_sign"])
        code, out = self.dry([twice])
        self.assertEqual(code, 1)
        self.assertIn("образец 6 раз вместо 1", out)  # sign, zero_sign, double, slow, mode, checked

    def test_an_edit_that_breaks_the_next_one_fails_dry(self) -> None:
        clash = {
            **RED,
            "new": "return 1  # " + DOUBLE,
            "extra": [{"old": DOUBLE, "new": "return 0"}],
        }
        code, out = self.dry([clash])
        self.assertEqual(code, 1)
        self.assertIn("после предыдущих правок образец 2 раз вместо 1", out)
        made = {**RED, "new": "return 1  # mark", "extra": [{"old": "# mark", "new": "# z"}]}
        code, out = self.dry([made])  # образец второй правки внесла первая: compare его не найдёт
        self.assertEqual(code, 1)
        self.assertIn("правка 2 (proj/calc.py): образец 0 раз вместо 1", out)


class Isolation(Harness):
    def test_the_working_copy_is_untouched(self) -> None:
        before = pa.snapshot(self.repo)
        status = self.git("status", "--porcelain")
        self.assertEqual(self.run_plants([RED, MULTI, DATA]), 0, self.output)
        self.assertEqual(
            (pa.snapshot(self.repo), self.git("status", "--porcelain")), (before, status)
        )

    def test_bad_work_directories_are_refused(self) -> None:
        path = self.harness([RED])
        busy = self.aside / "busy"
        (busy / "keep").mkdir(parents=True)
        cases = {
            "вне репозитория": (self.repo / "w", self.log),
            "не над журналом": (self.aside, self.log),
            "не пуст": (busy, self.aside / "elsewhere/runs.jsonl"),
        }
        for needle, (work, log) in cases.items():
            with self.subTest(needle):
                code, _, err = self.main(
                    "run", "--plantings", str(path), "--log", str(log), "--work", str(work), "--all"
                )
                self.assertEqual(code, 2, err)
                self.assertIn(needle, err)
        self.assertFalse((self.repo / "w").exists())
        self.assertTrue((busy / "keep").is_dir(), "чужой каталог не тронут")

    def test_copies_are_removed_unless_kept(self) -> None:
        path = self.harness([RED])
        args = ["run", "--plantings", str(path), "--log", str(self.log), "--work", str(self.work)]
        self.assertEqual(self.main(*args, "--all")[0], 0)
        self.assertFalse(self.work.exists(), "созданный харнессом каталог убран")
        self.work.mkdir()
        self.assertEqual(self.main(*args, "--keep", "--all")[0], 0)
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ["probe", "tree"])
        tree = self.work / "tree/proj"
        self.assertTrue((tree / "data.txt").is_file(), "неотслеживаемый файл")
        self.assertFalse((self.work / "tree/other").exists(), "вне COPY")
        self.assertTrue((tree / "export.txt").is_file(), "export-ignore не применяется")
        self.assertEqual((tree / "lines.crlf").read_bytes(), b"a\nb\n", "eol не применяется")
        self.assertTrue((self.work / "probe/sitecustomize.py").is_file(), "наблюдатель")

    def test_line_endings_survive_a_planting(self) -> None:
        crlf = self.repo / "proj/crlf.txt"
        crlf.write_bytes(b"a\r\nb\r\n")
        copy = self.aside / "copy"
        pr.export(self.repo, pa.snapshot(self.repo), ["proj"], copy)
        self.assertEqual((copy / "proj/crlf.txt").read_bytes(), b"a\r\nb\r\n")
        raw = {"label": "c", "file": "proj/crlf.txt", "old": "a\r\n", "new": "z\r\n"}
        path = self.aside / "plants.json"
        path.write_text(json.dumps([{**raw, "expect": [T + "test_sign"]}]), encoding="utf-8")
        pr.plant(copy, pa.load_plantings(path)[0])
        self.assertEqual((copy / "proj/crlf.txt").read_bytes(), b"z\r\nb\r\n")


def _report(**tests: dict[str, Any]) -> dict[str, Any]:
    return {
        "collected": list(tests),
        "tests": tests,
        "collect_errors": [],
        "modules": {},
        "rootdir": str(Judge.RUN_DIR),
        "watch": True,
    }


def _failed(*types: str, when: str = "call", **raised: list[str]) -> dict[str, Any]:
    return {
        "outcome": "failed",
        "failures": [{"when": when, "types": list(types), "message": ""}],
        "raised": raised,
    }


PASSED: dict[str, Any] = {"outcome": "passed", "failures": [], "raised": {}}
SKIPPED: dict[str, Any] = {"outcome": "skipped", "failures": [], "raised": {}}
ASSERT = "builtins.AssertionError"
NAME_ERROR = ["builtins.NameError", "builtins.Exception"]


class Judge(unittest.TestCase):
    """Вердикт по отчёту плагина без прогона: случаи, которые на настоящем pytest не собрать."""

    RUN_DIR = Path(tempfile.gettempdir()).resolve()

    def judge(
        self,
        rc: int | None,
        report: dict[str, Any] | None,
        expect: list[str],
        raises: list[str] | None = None,
        base: dict[str, Any] | None = None,
    ) -> str:
        result = pr.Result(rc, report, 1.0, Path("out.txt"))
        return pr.judge(result, expect, raises or [], self.RUN_DIR, base).verdict

    def test_failed_from_pytest_and_assertion_subclasses_are_checks(self) -> None:
        failed = _failed("_pytest.outcomes.Failed", "builtins.BaseException")
        self.assertEqual(self.judge(1, _report(**{"t.py::a": failed}), ["t.py::a"]), "RED")
        sub = _failed("lib.CheckError", ASSERT, "builtins.Exception")
        self.assertEqual(self.judge(1, _report(**{"t.py::a": sub}), ["t.py::a"]), "RED")

    def test_raises_names_builtins_short_and_others_with_a_module(self) -> None:
        redis = [
            "redis.exceptions.TimeoutError",
            "redis.exceptions.RedisError",
            "builtins.Exception",
        ]
        report = _report(**{"t.py::a": _failed(*redis)})
        for raises, verdict in (
            (["exceptions.RedisError"], "RED"),
            (["redis.exceptions.TimeoutError"], "RED"),
            (["TimeoutError"], "EXCEPTION"),  # встроенного TimeoutError в MRO нет
            (["s.RedisError"], "EXCEPTION"),  # суффикс — только по точке
        ):
            with self.subTest(raises=raises):
                self.assertEqual(self.judge(1, report, ["t.py::a"], raises), verdict)
        refused = _failed("builtins.ConnectionRefusedError", "builtins.ConnectionError")
        report = _report(**{"t.py::a": refused})
        raises = ["redis.exceptions.ConnectionError"]
        self.assertEqual(self.judge(1, report, ["t.py::a"], raises), "EXCEPTION")

    def test_a_failure_outside_the_test_body_is_not_a_check(self) -> None:
        for when in ("setup", "teardown"):
            with self.subTest(when=when):
                report = _report(**{"t.py::a": _failed(ASSERT, when=when)})
                self.assertEqual(self.judge(1, report, ["t.py::a"]), "EXCEPTION")

    def test_one_undeclared_exception_in_an_entry_outranks_its_checks(self) -> None:
        sql = _failed("asyncpg.exceptions.IndeterminateDatatypeError")
        report = _report(**{"t.py::a[1]": _failed(ASSERT), "t.py::a[2]": sql})
        self.assertEqual(self.judge(1, report, ["t.py::a"]), "EXCEPTION")
        report = _report(
            **{"t.py::a": _failed(ASSERT), "t.py::b": _failed(*NAME_ERROR), "t.py::c": PASSED}
        )
        self.assertEqual(self.judge(1, report, ["t.py::a", "t.py::b", "t.py::c"]), "EXCEPTION")

    def test_a_swallowed_error_counts_only_when_new_and_undeclared(self) -> None:
        key = "builtins.NameError @ web.py:compute"
        report = _report(**{"t.py::a": _failed(ASSERT, **{key: NAME_ERROR})})
        self.assertEqual(self.judge(1, report, ["t.py::a"]), "EXCEPTION")
        base = _report(**{"t.py::a": {**PASSED, "raised": {key: NAME_ERROR}}})
        self.assertEqual(self.judge(1, report, ["t.py::a"], base=base), "RED")
        self.assertEqual(self.judge(1, report, ["t.py::a"], ["NameError"]), "RED")
        elsewhere = _report(**{"t.py::a": {**PASSED, "raised": {"x @ web.py:legacy": NAME_ERROR}}})
        self.assertEqual(self.judge(1, report, ["t.py::a"], base=elsewhere), "EXCEPTION")

    def test_states_mix_into_one_verdict(self) -> None:
        red_skipped = _report(**{"t.py::a": _failed(ASSERT), "t.py::b": SKIPPED})
        self.assertEqual(self.judge(1, red_skipped, ["t.py::a", "t.py::b"]), "PARTIAL")
        session = _report()
        session["collect_errors"] = [{"nodeid": "", "types": [], "message": "conftest"}]
        self.assertEqual(self.judge(1, session, ["t.py::a"]), "COLLECT")
        elsewhere = _report(**{"t.py::a": PASSED})
        elsewhere["collect_errors"] = [{"nodeid": "u.py", "types": [], "message": "import"}]
        self.assertEqual(self.judge(1, elsewhere, ["t.py::a"]), "OTHER")
        self.assertEqual(self.judge(5, _report(), ["t.py::a"]), "MISSING")

    def test_runs_that_mean_nothing_are_errors(self) -> None:
        self.assertEqual(self.judge(1, None, ["t.py::a"]), "ERROR")
        self.assertEqual(self.judge(3, _report(), ["t.py::a"]), "ERROR")
        self.assertEqual(self.judge(1, _report(**{"t.py::a": PASSED}), ["t.py::a"]), "ERROR")
        unwatched = {**_report(**{"t.py::a": _failed(ASSERT)}), "watch": False}
        self.assertEqual(self.judge(1, unwatched, ["t.py::a"]), "ERROR")
        elsewhere = {**_report(**{"t.py::a": _failed(ASSERT)}), "rootdir": "/elsewhere"}
        self.assertEqual(self.judge(1, elsewhere, ["t.py::a"]), "ERROR")

    def test_a_module_imported_from_outside_the_copy_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp).resolve() / "copy"
            (run_dir / "app").mkdir(parents=True)
            (run_dir / "app/__init__.py").write_text("", encoding="utf-8")
            (run_dir / "single.py").write_text("", encoding="utf-8")
            (run_dir / "plantings_probe.py").write_text("", encoding="utf-8")  # прогон скриптов
            report = {**_report(**{"t.py::a": _failed(ASSERT)}), "rootdir": str(run_dir)}
            report["modules"] = {
                "app": str(run_dir / "app/__init__.py"),
                "single": "/repo/single.py",
                "os": "/lib/os.py",
                "plantings_probe_harness": "/work/probe/plantings_probe_harness.py",  # плагин
                "sitecustomize": "/work/probe/sitecustomize.py",
            }
            result = pr.Result(1, report, 1.0, Path("out.txt"))
            outcome = pr.judge(result, ["t.py::a"], [], run_dir)
            self.assertEqual(
                (outcome.verdict, outcome.detail),
                ("ERROR", "импорт не из копии: single из /repo/single.py"),
            )
            report["modules"]["single"] = str(run_dir / "single.py")
            report["modules"]["app"] = "/repo/app/__init__.py"
            outcome = pr.judge(result, ["t.py::a"], [], run_dir)
            self.assertEqual(outcome.detail, "импорт не из копии: app из /repo/app/__init__.py")
            report["modules"]["app"] = str(run_dir / "app/__init__.py")
            self.assertEqual(pr.judge(result, ["t.py::a"], [], run_dir).verdict, "RED")

    def test_baseline_problems(self) -> None:
        clean = _report(**{"t.py::a": PASSED})
        self.assertEqual(
            pr.baseline_problems(pr.Result(0, clean, 1.0, Path("o")), self.RUN_DIR), []
        )
        self.assertEqual(
            pr.baseline_problems(pr.Result(3, clean, 1.0, Path("o")), self.RUN_DIR),
            ["pytest завершился с кодом 3"],
        )
        self.assertEqual(
            pr.baseline_problems(pr.Result(5, _report(), 1.0, Path("o")), self.RUN_DIR),
            ["pytest не собрал ни одного теста"],
        )


class Watch(unittest.TestCase):
    """Наблюдатель исключений — в отдельном процессе: импорт с ``PLANTINGS_PROBE_ROOT`` включает
    ``sys.monitoring`` в процессе, который его импортировал."""

    def test_only_files_of_the_run_outside_tests_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "app").mkdir()
            names = [
                "<frozen importlib._bootstrap>",
                "<string>",
                str(root / "app/web.py"),
                str(root / "tests/t.py"),
                str(root / "conftest.py"),
                "/elsewhere/web.py",
            ]
            probe = f"import plantings_watch as w; print(w.ACTIVE, *map(w.app_path, {names!r}))"
            env = {
                **os.environ,
                "PLANTINGS_PROBE_ROOT": str(root),
                "PYTHONPATH": str(HARNESS.parent),
            }
            # Аргументы — фиксированный список без оболочки.
            done = subprocess.run(  # noqa: S603
                [sys.executable, "-c", probe],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
        expected = ["True", "None", "None", "app/web.py", "None", "None", "None"]
        self.assertEqual(done.stdout.split(), expected, done.stderr)


class Load(Harness):
    def refused(self, plants: list[dict[str, Any]], extra: str = "") -> str:
        path = self.harness(plants, extra)
        code, _, err = self.main("dry", "--plantings", str(path))
        self.assertEqual(code, 2, err)
        return err

    def test_bad_descriptions_are_refused(self) -> None:
        cases: dict[str, list[dict[str, Any]]] = {
            "неизвестные поля ['expected']": [{**RED, "expected": []}],
            "без -x и --maxfail": [{**RED, "subset": ["tests", "-x"]}],
            "подмножества 'nope' нет": [{**RED, "subset": "nope"}],
            "замена совпадает с образцом": [{**RED, "new": RED["old"]}],
            "правка 2 без замены new": [{**RED, "extra": [{"old": DOUBLE}]}],
            "хука 'absent' нет": [{**RED, "after": "absent"}],
            "пустой expect": [{**RED, "expect": []}],
            "предел времени": [{**RED, "timeout": 0}],
            "'RedisError' — не встроенное исключение": [{**RED, "raises": ["RedisError"]}],
            "метка совпадает с red без регистра": [RED, {**RED, "label": "Red"}],
        }
        for needle, plants in cases.items():
            with self.subTest(needle):
                self.assertIn(needle, self.refused(plants))
        for label in ("a/b", "../x", ".hidden", "x y", "_base"):
            with self.subTest(label=label):
                self.assertIn("метка — буквы", self.refused([{**RED, "label": label}]))

    def test_named_subsets_and_the_full_set_take_no_flags(self) -> None:
        named = 'SUBSETS["calc"] = ["tests/test_calc.py", "--maxfail=1"]\n'
        self.assertIn("подмножество calc: ключи pytest", self.refused([RED], extra=named))
        full = 'FULL = ["tests", "-x"]\n'
        self.assertIn("FULL: ключи pytest", self.refused([RED], extra=full))

    def test_a_function_inside_a_planting_is_refused(self) -> None:
        path = self.aside / "plants.py"
        fields = ", ".join(f"{k}={v!r}" for k, v in RED.items())
        path.write_text(HEADER + f"\nPLANTS = [dict({fields}, hook=note)]\n", encoding="utf-8")
        code, _, err = self.main("dry", "--plantings", str(path))
        self.assertEqual(code, 2)
        self.assertIn("неизвестные поля ['hook']", err)
        path.write_text(HEADER + f"\nPLANTS = [dict({fields}, raises=[note])]\n", encoding="utf-8")
        code, _, err = self.main("dry", "--plantings", str(path))
        self.assertEqual(code, 2)
        self.assertIn("значения посадки — только JSON", err)

    def test_the_planting_module_runs_once(self) -> None:
        counter = self.aside / "loads.txt"
        extra = f"with open({str(counter)!r}, 'a') as _fh:\n    _fh.write('x')\n"
        self.assertEqual(self.main("dry", "--plantings", str(self.harness([RED], extra)))[0], 0)
        self.assertEqual(counter.read_text(encoding="utf-8"), "x")

    def test_json_plantings_have_settings_but_no_hooks(self) -> None:
        settings = {
            "copy": ["proj"],
            "run_dir": "proj",
            "python": sys.executable,
            "subsets": {"calc": ["tests/test_calc.py"]},
        }
        path = self.aside / "plants.json"
        path.write_text(
            json.dumps({**settings, "plantings": [{**RED, "after": "note"}]}), encoding="utf-8"
        )
        code, _, err = self.main("dry", "--plantings", str(path))
        self.assertEqual(code, 2)
        self.assertIn("только в модуле посадок", err)
        path.write_text(json.dumps({**settings, "plantings": [RED]}), encoding="utf-8")
        code, out, err = self.main("run", "--plantings", str(path), "--log", str(self.log), "--all")
        self.assertEqual(code, 0, out + err)
        path.write_text(
            json.dumps({**settings, "plantings": [RED], "copies": []}), encoding="utf-8"
        )
        self.assertIn("неизвестные ключи ['copies']", self.main("dry", "--plantings", str(path))[2])

    def test_run_dir_outside_copy_is_refused(self) -> None:
        self.assertIn("вне каталогов COPY", self.refused([RED], extra='RUN_DIR = "other"\n'))

    def test_a_foreign_line_in_the_log_is_refused_before_running(self) -> None:
        self.log.write_text('{"repair": "стенд"}\n', encoding="utf-8")
        self.assertEqual(self.run_plants([RED]), 2, self.output)
        self.assertIn("без label или verdict", self.output)


if __name__ == "__main__":
    unittest.main()
