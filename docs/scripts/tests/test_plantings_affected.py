"""Тесты отбора посадок по правилу 3 «Цикла задачи» (``docs/scripts/plantings_affected.py``,
WI-40). Вход — репозиторий git с раскладкой проекта (``control-plane/tests/e2e``, корень тестов по
умолчанию, ``conftest.py`` на трёх уровнях, ``pytest_plugins`` вложенные и вне пакета тестов,
``*``-импорт и импортированные фикстуры, модули с кодом при импорте), коммит с отслеживаемым
игнорируемым файлом, журнал с деревом и spec прогона каждой посадки; правка рабочей копии, затем
отбор."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plantings_affected as pa  # noqa: E402

GIT = shutil.which("git") or "git"
CP = "control-plane/"

AUTO_FIXTURE = "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef {name}():\n    yield\n"

TREE: dict[str, str | bytes] = {
    ".gitignore": "ignored/\n*.local\n",
    ".gitattributes": "*.crlf text=auto\n",
    "pyproject.toml": '[tool.pytest]\naddopts = ["-ra"]\n',
    "contracts/agent.json": '{"v": 1}\n',
    "contracts/[x].json": '{"x": 1}\n',
    CP + "pyproject.toml": (
        '[tool.pytest]  # родная форма pytest 9\naddopts = ["-q"]\n\n'
        '[tool.pytest.ini_options]\nasyncio_mode = "auto"\n\n[tool.ruff]\nline-length = 100\n'
    ),
    CP + "pytest.ini": "[pytest]\nminversion = 9.0\n",
    CP + "pytest.toml": "[pytest]\nxfail_strict = true\n",
    CP + ".pytest.toml": '[pytest]\nconsole_output_style = "classic"\n',
    CP + ".pytest.ini": "[pytest]\nempty_parameter_set_mark = skip\n",
    CP + "setup.cfg": "[tool:pytest]\nlog_level = INFO\n\n[metadata]\nname = x\n",
    CP + "tox.ini": "[pytest]\nlog_cli = false\n\n[tox]\nenvlist = py\n",
    CP + "tests/pytest.ini": "[pytest]\nfilterwarnings = error\n",
    CP + "config.conf": "first\nPATTERN\nlast\n",
    CP + "conftest.py": """import pytest

pytest_plugins = "tests._plug_str"


@pytest.fixture(autouse=True)
def root_auto():
    yield
""",
    CP + "support/plug.py": """import pytest


@pytest.fixture
def supported():
    return 31
""",
    CP + "app/__init__.py": "",
    CP + "app/schema.sql": "create table t (id int);\ngrant select on t to app;\n",
    CP + "app/mod2.py": "def ok():\n    return 1\n",
    CP + "app/mod.py": '''def guarded(x):
    """Вентиль."""
    # проверка знака
    return x > 0


def noisy():
    log("a")
    return 2


class Box:
    KIND = "box"

    @property
    def value(self):
        """Чтение."""
        return 1  # getter

    @value.setter
    def value(self, v):
        self._v = v


def first():
    LIMIT = 10
    return LIMIT


def second():
    LIMIT = 10
    return LIMIT


def other():
    return 1


def mf(x):
    return x * 2  # mark


def mg(x):
    return x * 2


def fm(x):
    a = x > 1  # guard2
    b = x > 1
    return a and b


a = 1; b = 2
пометка = "ёёёё"; zz = log(2)

QUERY = (
    "select 1"
    " from t"
)


def log(message):
    return message
''',
    CP + "tests/__init__.py": "",
    CP + "tests/conftest.py": """import pytest
from pytest import fixture

pytest_plugins: list[str] = ["tests.plugin", "support.plug", "tests._envplug"]
pytest_plugins += ["tests._plug_aug"]

collect_ignore = ["test_never.py"]


@pytest.fixture
def redis_url():
    return "redis://a"


@pytest.fixture
def stand():
    return 11


@pytest.fixture
def env_dep():
    return 71


@pytest.fixture(autouse=True)
def env(env_dep):
    yield


@fixture
def bare():
    return 12


def pytest_collection_modifyitems(items):
    return None
""",
    CP + "tests/plugin.py": """import pytest

pytest_plugins = ["tests.plugin_nested"]


@pytest.fixture
def plugged():
    return 14
""",
    CP + "tests/plugin_nested.py": """import pytest


@pytest.fixture
def nested():
    return 61
""",
    CP + "tests/shared.py": """import pytest


@pytest.fixture
def shared_fixture():
    return 13
""",
    CP + "tests/_fx.py": """import pytest


@pytest.fixture(autouse=True)
def isolate():
    yield


@pytest.fixture(name="renamed")
def renamed_impl():
    return 41
""",
    CP + "tests/_star.py": """import pytest


@pytest.fixture
def starred():
    return 51
""",
    CP + "tests/_far.py": "def far():\n    return 1\n",
    CP + "tests/_fast.py": "def h():\n    return 1\n",
    CP + "tests/_slow.py": "def h():\n    return 2\n",
    CP + "tests/_starnames2.py": "STARRED2 = 7\n",
    CP + "tests/_plug_str.py": AUTO_FIXTURE.format(name="plug_str"),
    CP + "tests/_plug_aug.py": AUTO_FIXTURE.format(name="plug_aug"),
    CP + "tests/_envplug.py": 'import os\n\nos.environ.setdefault("PLUG_MODE", "1")\n',
    CP + "tests/_env.py": 'import os\n\nos.environ.setdefault("SIDE_MODE", "1")\n',
    CP + "tests/envcase/__init__.py": 'import os\n\nos.environ.setdefault("PKG_MODE", "1")\n',
    CP + "tests/envcase/conftest.py": 'import os\n\nos.environ.setdefault("CONF_MODE", "1")\n',
    CP + "tests/envcase/_flag.py": 'import os\n\nos.environ["FLAG"] = "1"\n',
    CP + "tests/envcase/_attr.py": 'import os\n\nos.sep_alias = "/"\n',
    CP + "tests/envcase/_under.py": 'import os\n\n_ = os.path.join("a")\n',
    CP + "tests/envcase/_registry.py": """CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def big():
    return 10
""",
    CP + "tests/envcase/_classbody.py": """import os


class Setup:
    os.environ["LIMIT"] = "1"


def h():
    return 1
""",
    CP + "tests/envcase/_subreg.py": """class Base:
    registry = []

    def __init_subclass__(cls):
        Base.registry.append(cls)


class Big(Base):
    value = 10
""",
    CP + "tests/envcase/_tc.py": """TYPE_CHECKING = False
if TYPE_CHECKING:
    pass
else:
    VALUE = 1
""",
    CP + "tests/envcase/_ne.py": """import os

if __name__ != "__main__":
    os.environ["NE"] = "1"
""",
    CP + "tests/envcase/_with.py": """import contextlib

with contextlib.suppress(Exception):
    VALUE = 1
""",
    CP + "tests/envcase/_flagif.py": "DEBUG = False\nif DEBUG:\n    VALUE = 2\n",
    CP + "tests/envcase/test_env.py": """import tests._env
import tests.envcase._attr
import tests.envcase._flag
import tests.envcase._flagif
import tests.envcase._ne
import tests.envcase._tc
import tests.envcase._under
import tests.envcase._with
from tests.envcase._classbody import h
from tests.envcase._registry import CASES
from tests.envcase._subreg import Base


def test_e():
    assert True


def test_registry():
    assert CASES and Base.registry and h()
""",
    CP + "tests/envcase/test_multi.py": """try:
    from tests._fast import h
except ImportError:
    from tests._slow import h


def test_m():
    assert h()
""",
    CP + "tests/integration/_sib.py": "def s():\n    return 1\n",
    CP + "tests/integration/test_nopkg.py": """from _sib import s
from tests._far import far


def test_n():
    assert s() and far()
""",
    CP + "tests/_starnames.py": "STARRED = 5\n",
    CP + "tests/e2e/__init__.py": "",
    CP + "tests/e2e/conftest.py": """import pytest

from tests._fx import isolate, renamed_impl
from tests._star import *
from tests.shared import shared_fixture


@pytest.fixture
def stand(stand):
    return stand + 1


@pytest.fixture
def limiter(redis_url):
    return redis_url


@pytest.fixture(name="named")
def _named():
    return 15


def pytest_runtest_setup(item):
    return None


def pytest_configure(config):
    config.limiter_ref = limiter
""",
    CP + "tests/e2e/_deep.py": "def deep():\n    return 1\n",
    CP + "tests/e2e/_helpers.py": """from tests.e2e._deep import deep

LIMIT_HELPER = 1

CASES = [1, 2]


def helper():
    return deep()


class BaseMixin:
    def test_mixed(self, stand):
        assert stand
""",
    CP + "tests/e2e/_conditional.py": """import os

if os.environ.get("X_FLAG"):
    CONDITIONAL = 1
else:
    CONDITIONAL = 2
""",
    CP + "tests/e2e/_fallback.py": """try:
    FAST = 1
except ImportError:
    FAST = 0
""",
    CP + "tests/e2e/golden/doc.json": '{"a": 1}\n',
    CP + "tests/e2e/golden/cert.der": b"\x30\x82\xff\xfe\x00",
    CP + "tests/e2e/golden/keep.local": "kept\n",
    CP + "tests/e2e/golden/win.crlf": "a\r\nb\r\n",
    CP + "tests/e2e/test_contract.sh": "echo ok\n",
    CP + "tests/e2e/test_override.py": """import pytest


@pytest.fixture
def stand():
    return 81


def test_o(stand):
    assert stand


def test_kw(*, renamed):
    assert renamed
""",
    CP + "tests/e2e/test_skipmod.py": """import pytest

pytest.importorskip("json")


def test_s():
    assert True
""",
    CP + "tests/e2e/test_mod.py": '''import pytest

import tests.e2e._deep
import tests.e2e._deep as deep_alias
from app.mod import guarded, other
from tests.e2e import _helpers as helpers_module
from tests.e2e._conditional import CONDITIONAL
from tests.e2e._fallback import FAST
from tests.e2e._helpers import CASES
from tests.e2e._helpers import deep as reexported
from tests._starnames import *
from tests._starnames2 import *

from .._far import far
from ._helpers import helper

pytestmark = pytest.mark.usefixtures("plugged")

VALUES = (1, 2)

COUNTER = 1
COUNTER += 1

deep = None

CASES += [3]


def local():
    return helper()


@pytest.fixture
def local_fixture():
    return 16


@pytest.fixture(autouse=True)
def module_auto():
    yield


@pytest.mark.parametrize("v", VALUES)
def test_guard(stand, limiter, v):
    """Вентиль."""
    assert guarded(v) and local()


def test_other():
    assert other() == 1


def test_local_import():
    from tests.e2e._deep import deep

    assert deep()


def test_chain():
    assert tests.e2e._deep.deep() and helpers_module.LIMIT_HELPER


def test_alias():
    assert deep_alias.deep()


def test_far():
    assert far()


def test_reexport():
    assert reexported()


def test_cases():
    assert CASES


def test_starname():
    assert STARRED and STARRED2


def test_named(named, shared_fixture, bare):
    assert named


def test_module_state():
    assert CONDITIONAL and FAST and COUNTER


def test_local_fixture(local_fixture):
    assert True


def test_dyn(request):
    assert request.getfixturevalue("bare")


def test_vars():
    assert vars(helpers_module)


def test_reassigned():
    assert True


test_reassigned = pytest.mark.skipif(False, reason="x")(test_reassigned)


def test_plugins(supported, nested, starred, renamed):
    assert supported


class Base:
    def test_inherited(self, redis_url):
        assert redis_url


class TestGroup(Base):
    def test_one(self):
        assert True

    def test_two(self):
        assert True


class TestOuter:
    class TestInner:
        def test_x(self):
            assert True


class TestAttr(helpers_module.BaseMixin):
    pass


@pytest.mark.usefixtures("stand")
def test_uses():
    assert True
''',
}

MOD = CP + "app/mod.py"
TEST = CP + "tests/e2e/test_mod.py"
E = "tests/e2e/test_mod.py::"
OTHER = "def other():"


def plant(label: str, expect: list[str], old: str = OTHER, **extra: Any) -> dict[str, Any]:
    return {"label": label, "file": MOD, "old": old, "expect": expect, **extra}


PLANTS = [
    plant("guard", [E + "test_guard[1]"], old="return x > 0"),
    plant("noisy", [E + "test_other"], old="return 2"),
    plant("getter", [E + "test_other"], old="return 1  # getter"),
    plant("setter", [E + "test_other"], old="        self._v = v"),
    plant("limit", [E + "test_other"], old="LIMIT = 10", count=2),
    plant("span", [E + "test_other"], old="    return LIMIT\n\n\ndef second():"),
    plant("semi", [E + "test_other"], old="b = 2"),
    plant("bytes", [E + "test_other"], old="zz = log(2)"),
    plant("query", [E + "test_other"], old='"select 1"'),
    plant("kind", [E + "test_other"], old='KIND = "box"'),
    plant("nl", [E + "test_other"], old="    return message\n"),
    plant("deco", [E + "test_other"], old="@value.setter"),
    plant("moved", [E + "test_other"], old="x * 2  # mark"),
    plant("inblock", [E + "test_other"], old="x > 1  # guard2", new="x < 1  # guard2"),
    {**plant("conf", [E + "TestGroup::test_one"], old="PATTERN"), "file": CP + "config.conf"},
    {**plant("sql", [E + "test_other"], old="create table t"), "file": CP + "app/schema.sql"},
    {**plant("broken", [E + "test_other"], old="return 1"), "file": CP + "app/mod2.py"},
    plant("other", [E + "test_other"]),
    plant("contract", [E + "test_other"], inputs=["contracts"]),
    plant("local_import", [E + "test_local_import"]),
    plant("chain", [E + "test_chain"]),
    plant("alias", [E + "test_alias"]),
    plant("far", [E + "test_far"]),
    plant("reexport", [E + "test_reexport"]),
    plant("cases", [E + "test_cases"]),
    plant("starname", [E + "test_starname"]),
    plant("named", [E + "test_named"]),
    plant("module_state", [E + "test_module_state"]),
    plant("local_fixture", [E + "test_local_fixture"]),
    plant("dyn", [E + "test_dyn"]),
    plant("vars", [E + "test_vars"]),
    plant("reassigned", [E + "test_reassigned"]),
    plant("plugins", [E + "test_plugins"]),
    plant("inherited", [E + "TestGroup::test_inherited"]),
    plant("nested", [E + "TestOuter::TestInner::test_x"]),
    plant("attrbase", [E + "TestAttr::test_mixed"]),
    plant("uses", [E + "test_uses"]),
    plant("override", ["tests/e2e/test_override.py::test_o"]),
    plant("kw", ["tests/e2e/test_override.py::test_kw"]),
    plant("skipmod", ["tests/e2e/test_skipmod.py::test_s"]),
    plant("wholefile", ["tests/e2e/test_skipmod.py"]),
    plant("shell", ["tests/e2e/test_contract.sh"]),
    plant("env", ["tests/envcase/test_env.py::test_e"]),
    plant("registry", ["tests/envcase/test_env.py::test_registry"]),
    plant("multi", ["tests/envcase/test_multi.py::test_m"]),
    plant("nopkg", ["tests/integration/test_nopkg.py::test_n"]),
    plant("bracket", [E + "test_other"], inputs=["contracts/[x].json"]),
]


def write(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")


class Repo(unittest.TestCase):
    """Репозиторий с деревом ``TREE`` в коммите; все посадки измерены на нём (RED)."""

    def git(self, *args: str) -> str:
        # Аргументы — литералы теста, без оболочки.
        return subprocess.run(  # noqa: S603
            [GIT, "-C", str(self.repo), *args], capture_output=True, text=True, check=True
        ).stdout

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo, self.aside = Path(tmp.name) / "repo", Path(tmp.name) / "aside"
        self.repo.mkdir()
        self.aside.mkdir()
        self.git("init", "-q")
        write(self.repo, TREE)
        self.git("add", "-A")
        self.git("add", "-f", CP + "tests/e2e/golden/keep.local")
        self.git("-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "t")
        self.runs: dict[str, pa.Run] = {}
        self.measure(PLANTS)

    def plantings(self, raws: list[dict[str, Any]]) -> list[pa.Planting]:
        path = self.aside / "plants.json"
        path.write_text(json.dumps(raws, ensure_ascii=False))
        return pa.load_plantings(path)

    def measure(self, raws: list[dict[str, Any]], verdict: str = "RED") -> None:
        tree = pa.snapshot(self.repo)
        for p in self.plantings(raws):
            self.runs[p.label] = pa.Run(verdict, tree, p.spec)

    def edit(self, rel: str, old: str, new: str) -> None:
        path = self.repo / rel
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, f"образец правки {old!r} в {rel}")
        path.write_text(text.replace(old, new), encoding="utf-8")

    def reasons(
        self, raws: list[dict[str, Any]] | None = None, tests_root: str = "control-plane"
    ) -> dict[str, list[str]]:
        plantings = self.plantings(PLANTS if raws is None else raws)
        verdicts = pa.compare(plantings, self.repo, tests_root, self.runs)
        return {v.label: v.reasons for v in verdicts}

    def joined(self, label: str) -> str:
        return " | ".join(self.reasons()[label])

    def changed(self, label: str) -> set[str]:
        """Имена частей теста из причин посадки: изменено, добавлено, убрано."""
        names: set[str] = set()
        for reason in self.reasons()[label]:
            for group in reason.split(": ", 1)[-1].split("; "):
                title, _, rest = group.partition(" ")
                if title in ("изменено", "добавлено", "убрано"):
                    names |= set(rest.split(", "))
        return names


class CodeAroundThePattern(Repo):
    def test_an_unchanged_tree_leaves_every_planting_dry(self) -> None:
        self.assertEqual(self.reasons(), {p["label"]: [] for p in PLANTS})

    def test_executable_code_around_the_pattern_is_a_change(self) -> None:
        self.edit(MOD, "    return x > 0", "    x = abs(x)\n    return x > 0")
        self.assertEqual(self.reasons()["guard"], ["код guarded"])

    def test_docstrings_comments_and_formatting_are_not(self) -> None:
        self.edit(MOD, '"""Вентиль."""', '"""Вентиль: знак."""')
        self.edit(MOD, "# проверка знака", "# знак, а не модуль")
        self.edit(MOD, "def guarded(x):\n", "def guarded(\n    x,\n):\n")
        self.edit(MOD, '"""Чтение."""', '"""Чтение значения."""')  # вложен в блок Box посадки kind
        reasons = self.reasons()
        self.assertEqual((reasons["guard"], reasons["getter"], reasons["kind"]), ([], [], []))

    def test_a_leading_expression_that_is_not_a_string_is_code(self) -> None:
        self.edit(MOD, 'log("a")', 'log("b")')
        self.assertEqual(self.reasons()["noisy"], ["код noisy"])

    def test_another_function_of_the_file_is_not_around_the_pattern(self) -> None:
        self.edit(MOD, "def other():\n    return 1", "def other():\n    return 3")
        reasons = self.reasons()
        self.assertEqual((reasons["guard"], reasons["nl"]), ([], []))  # nl оканчивается на \n

    def test_a_module_assignment_is_the_block_of_its_pattern(self) -> None:
        self.edit(MOD, '" from t"', '" from u"')
        reasons = self.reasons()
        self.assertEqual((reasons["query"], reasons["guard"]), (["код QUERY"], []))

    def test_every_occurrence_of_a_repeated_pattern_has_its_block(self) -> None:
        self.edit(MOD, "    return LIMIT\n\n\ndef other", "    return LIMIT + 0\n\n\ndef other")
        self.assertEqual(self.reasons()["limit"], ["код second"])
        self.measure(PLANTS)
        self.edit(MOD, "    return LIMIT\n\n\ndef second", "    return LIMIT + 0\n\n\ndef second")
        self.assertEqual(self.reasons()["limit"], ["код first"])

    def test_a_pattern_spanning_two_functions_is_the_whole_module(self) -> None:
        self.edit(MOD, "    return LIMIT\n\n\ndef other", "    return LIMIT + 0\n\n\ndef other")
        self.assertEqual(self.reasons()["span"], ["код <модуль>"])

    def test_same_named_definitions_are_told_apart_by_order(self) -> None:
        self.edit(MOD, "        self._v = v", "        self.w = 1\n        self._v = v")  # сеттер
        reasons = self.reasons()
        self.assertEqual((reasons["getter"], reasons["setter"]), ([], ["код Box.value#1"]))
        self.measure(PLANTS)
        getter = "        return 1  # getter"
        self.edit(MOD, getter, "        self.read = True\n" + getter)
        reasons = self.reasons()
        self.assertEqual((reasons["getter"], reasons["setter"]), (["код Box.value"], []))
        self.assertEqual(reasons["deco"], [])  # декоратор — часть сеттера, не класса

    def test_statements_on_one_line_are_told_apart_by_byte_column(self) -> None:
        self.edit(MOD, "a = 1; b = 2", "a = 3; b = 2")
        self.edit(MOD, 'пометка = "ёёёё"', 'пометка = "ёё"')
        reasons = self.reasons()
        self.assertEqual((reasons["semi"], reasons["bytes"]), ([], []))

    def test_a_pattern_missing_from_the_base_is_new(self) -> None:
        self.edit(MOD, "x > 0", "x >= 0")
        self.measure([PLANTS[0]])
        self.edit(MOD, "x >= 0", "x > 0")
        self.assertEqual(self.reasons()["guard"], ["образец новый или изменён"])

    def test_a_pattern_moved_to_another_block_by_a_comment_is_a_change(self) -> None:
        self.edit(
            MOD,
            "    return x * 2  # mark\n\n\ndef mg(x):\n    return x * 2\n",
            "    return x * 2\n\n\ndef mg(x):\n    return x * 2  # mark\n",
        )
        self.assertEqual(self.reasons()["moved"], ["образец переехал в другой блок"])

    def test_a_block_missing_from_the_base_is_a_change(self) -> None:
        self.edit(
            MOD,
            'def guarded(x):\n    """Вентиль."""\n    # проверка знака\n    return x > 0\n',
            "class Gate:\n    def guarded(x):\n        return x > 0\n",
        )
        self.assertEqual(
            self.reasons()["guard"],
            ["образец переехал в другой блок", "код Gate.guarded нет в базе"],
        )

    def test_a_pattern_moved_inside_its_block_changes_what_the_replacement_hits(self) -> None:
        self.edit(
            MOD,
            "    a = x > 1  # guard2\n    b = x > 1\n",
            "    a = x > 1\n    b = x > 1  # guard2\n",
        )
        self.assertEqual(self.reasons()["inblock"], ["код fm после замены"])

    def test_a_file_missing_from_the_base_is_a_change(self) -> None:
        raws = [{**plant("late", [E + "test_other"], old="return 9"), "file": CP + "app/late.py"}]
        self.measure(raws)
        write(self.repo, {CP + "app/late.py": "def late():\n    return 9\n"})
        self.assertEqual(self.reasons(raws)["late"], [f"файла {CP}app/late.py нет в базе"])

    def test_a_file_that_is_not_python_or_does_not_parse_is_compared_whole(self) -> None:
        self.edit(CP + "app/schema.sql", "grant select", "grant insert")
        self.edit(CP + "app/mod2.py", "    return 1\n", "    return 1\n\n\ndef (\n")
        reasons = self.reasons()
        self.assertEqual(reasons["sql"], [f"файл {CP}app/schema.sql изменён (не Python — целиком)"])
        self.assertEqual(
            reasons["broken"], [f"файл {CP}app/mod2.py изменён (не разбирается — целиком)"]
        )

    def test_a_pattern_count_off_in_the_tree_fails_the_dry_check(self) -> None:
        self.edit(MOD, "    return 2", "    return x > 0")
        self.assertEqual(self.reasons()["guard"][0], "образец в дереве 2 раз вместо 1")


class ExpectedTest(Repo):
    def test_the_test_body_is_part_of_it(self) -> None:
        self.edit(TEST, "assert guarded(v) and local()", "assert local() and guarded(v)")
        self.assertEqual(self.reasons()["guard"], ["тест test_guard: изменено test_guard"])

    def test_added_and_removed_parts_are_changes(self) -> None:
        self.edit(TEST, "assert other() == 1", "assert other() == helper()")
        self.assertIn("добавлено", self.joined("other"))
        self.assertIn("helper", self.changed("other"))
        self.edit(TEST, "assert guarded(v) and local()", "assert guarded(v)")
        self.assertIn("убрано", self.joined("guard"))
        self.assertIn("local", self.changed("guard"))

    def test_a_new_test_and_a_missing_test_are_changes(self) -> None:
        fresh = [
            plant("fresh", [E + "test_fresh"]),
            plant("gone", [E + "test_gone"]),
            plant("newsh", ["tests/e2e/test_new.sh"]),
        ]
        self.measure(fresh)
        self.edit(
            TEST, "def test_uses():", "def test_fresh():\n    assert True\n\n\ndef test_uses():"
        )
        write(self.repo, {CP + "tests/e2e/test_new.sh": "echo new\n"})
        self.assertEqual(
            self.reasons(fresh),
            {
                "fresh": ["тест test_fresh: новый"],
                "gone": ["тест test_gone: нет в дереве"],
                "newsh": ["тест tests/e2e/test_new.sh: новый"],
            },
        )

    def test_helpers_of_the_file_and_of_test_modules_belong_to_it(self) -> None:
        self.edit(TEST, "    return helper()", "    return helper() + 0")
        self.assertIn("local", self.changed("guard"))
        self.measure(PLANTS)
        self.edit(CP + "tests/e2e/_helpers.py", "    return deep()", "    return deep() + 0")
        self.assertIn("helper", self.changed("guard"))

    def test_a_chain_of_imports_is_followed_to_its_end(self) -> None:
        self.edit(CP + "tests/e2e/_deep.py", "    return 1", "    return 2")
        self.assertIn("deep", self.changed("guard"))  # тест → local → helper → deep

    def test_an_import_inside_the_test_shadows_the_module_name(self) -> None:
        self.edit(CP + "tests/e2e/_deep.py", "    return 1", "    return 2")
        self.assertEqual(self.reasons()["local_import"], ["тест test_local_import: изменено deep"])

    def test_attribute_chains_and_aliases_of_imported_modules_are_followed(self) -> None:
        self.edit(CP + "tests/e2e/_deep.py", "    return 1", "    return 2")
        self.assertIn("deep", self.changed("chain"))
        self.assertIn("deep", self.changed("alias"))  # import tests.e2e._deep as deep_alias
        self.measure(PLANTS)
        self.edit(CP + "tests/e2e/_helpers.py", "LIMIT_HELPER = 1", "LIMIT_HELPER = 2")
        self.assertIn("LIMIT_HELPER", self.changed("chain"))

    def test_relative_imports_of_any_level_and_reexports_are_followed(self) -> None:
        self.edit(CP + "tests/_far.py", "    return 1", "    return 2")
        self.assertIn("far", self.changed("far"))  # from .._far import far
        self.edit(CP + "tests/e2e/_deep.py", "    return 1", "    return 2")
        self.assertIn("deep", self.changed("reexport"))

    def test_an_imported_name_rebound_in_the_module_keeps_its_import(self) -> None:
        self.edit(CP + "tests/e2e/_helpers.py", "CASES = [1, 2]", "CASES = [1, 4]")
        self.assertIn("CASES", self.changed("cases"))  # from … import CASES; CASES += [3]

    def test_a_name_from_a_star_import_is_followed(self) -> None:
        self.edit(CP + "tests/_starnames.py", "STARRED = 5", "STARRED = 6")
        self.assertIn("STARRED", self.changed("starname"))

    def test_the_import_binding_is_part_of_it_even_for_application_code(self) -> None:
        self.edit(
            TEST,
            "from app.mod import guarded, other",
            "from app.mod import guarded, noisy as other",
        )
        self.assertEqual(self.reasons()["other"], ["тест test_other: изменено other (импорт)"])

    def test_application_code_is_not_followed(self) -> None:
        self.edit(MOD, "def other():\n    return 1", "def other():\n    return 3")
        self.assertEqual(self.reasons()["query"], [])  # тот же тест; код приложения не в нём

    def test_a_module_passed_as_an_object_is_taken_whole(self) -> None:
        self.edit(
            CP + "tests/e2e/_helpers.py", "LIMIT_HELPER = 1\n", "LIMIT_HELPER = 1\nEXTRA = 2\n"
        )
        self.assertIn("<модуль>", self.changed("vars"))

    def test_fixtures_by_argument_override_and_chain(self) -> None:
        self.edit(CP + "tests/e2e/conftest.py", "stand + 1", "stand + 2")  # ближний conftest
        self.assertIn("stand", self.changed("guard"))
        self.measure(PLANTS)
        self.edit(CP + "tests/conftest.py", "    return 11", "    return 21")  # переопределённая
        self.assertIn("stand", self.changed("guard"))
        self.assertEqual(self.reasons()["override"], [])  # модуль теста переопределил stand
        self.measure(PLANTS)
        self.edit(CP + "tests/e2e/test_override.py", "    return 81", "    return 82")
        self.assertIn("stand", self.changed("override"))
        self.measure(PLANTS)
        self.edit(CP + "tests/conftest.py", '"redis://a"', '"redis://b"')  # фикстура фикстуры
        self.assertIn("redis_url", self.changed("guard"))
        self.assertIn("redis_url", self.changed("inherited"))  # метод базового класса

    def test_fixtures_of_the_module_named_imported_bare_dynamic_and_keyword_only(self) -> None:
        self.edit(TEST, "    return 16", "    return 26")
        self.assertIn("local_fixture", self.changed("local_fixture"))
        self.edit(CP + "tests/e2e/conftest.py", "    return 15", "    return 25")
        self.edit(CP + "tests/shared.py", "    return 13", "    return 23")
        self.edit(CP + "tests/conftest.py", "    return 12", "    return 22")
        self.assertLessEqual({"named", "shared_fixture", "bare"}, self.changed("named"))
        self.assertIn("bare", self.changed("dyn"))  # request.getfixturevalue("bare")
        self.edit(CP + "tests/_fx.py", "    return 41", "    return 42")
        self.assertIn("renamed_impl", self.changed("kw"))  # def test_kw(*, renamed)

    def test_fixtures_of_plugins_star_imports_and_imported_names(self) -> None:
        self.edit(CP + "tests/plugin.py", "    return 14", "    return 24")  # pytestmark
        self.edit(CP + "support/plug.py", "    return 31", "    return 32")
        self.edit(CP + "tests/plugin_nested.py", "    return 61", "    return 62")
        self.edit(CP + "tests/_star.py", "    return 51", "    return 52")
        self.edit(CP + "tests/_fx.py", "    return 41", "    return 42")
        self.assertIn("plugged", self.changed("other"))
        self.assertLessEqual(
            {"supported", "nested", "starred", "renamed_impl"}, self.changed("plugins")
        )

    def test_autouse_fixtures_hooks_special_names_and_usefixtures(self) -> None:
        self.edit(CP + "tests/conftest.py", "    return 71", "    return 72")  # аргумент env
        self.edit(TEST, "def module_auto():\n    yield", "def module_auto():\n    yield None")
        self.edit(CP + "conftest.py", "    yield", "    yield None")
        self.edit(
            CP + "tests/_fx.py", "def isolate():\n    yield", "def isolate():\n    yield None"
        )
        self.edit(CP + "tests/conftest.py", "    return None", "    return items")
        self.edit(CP + "tests/e2e/conftest.py", "    return None", "    return item")
        self.edit(CP + "tests/conftest.py", '["test_never.py"]', '["test_never.py", "x.py"]')
        expected = {
            "env_dep",
            "module_auto",
            "root_auto",
            "isolate",
            "pytest_collection_modifyitems",
            "pytest_runtest_setup",
            "collect_ignore",
        }
        self.assertLessEqual(expected, self.changed("other"))
        self.measure(PLANTS)
        self.edit(CP + "tests/conftest.py", "    return 11", "    return 21")
        self.assertIn("stand", self.changed("uses"))

    def test_classes_whole_nested_and_inherited_from_attributes(self) -> None:
        self.edit(
            TEST,
            "    def test_two(self):\n        assert True",
            "    def test_two(self):\n        assert 1",
        )
        self.assertEqual(self.reasons()["conf"], ["тест TestGroup::test_one: изменено TestGroup"])
        self.edit(
            TEST,
            "        def test_x(self):\n            assert True",
            "        def test_x(self):\n            assert 2",
        )
        self.assertIn("TestOuter", self.changed("nested"))
        self.edit(CP + "tests/e2e/_helpers.py", "        assert stand", "        assert stand or 1")
        self.assertIn("BaseMixin", self.changed("attrbase"))

    def test_a_hook_naming_a_fixture_keeps_the_fixture_requests(self) -> None:
        self.edit(CP + "tests/conftest.py", '"redis://a"', '"redis://b"')
        self.assertIn("redis_url", self.changed("guard"))  # limiter(redis_url), limiter — в хуке

    def test_two_star_imports_are_both_followed(self) -> None:
        self.edit(CP + "tests/_starnames2.py", "STARRED2 = 7", "STARRED2 = 8")
        self.assertIn("STARRED2", self.changed("starname"))

    def test_a_reassigned_test_name_counts_with_all_its_bindings(self) -> None:
        self.edit(TEST, 'reason="x"', 'reason="y"')
        self.assertIn("test_reassigned", self.changed("reassigned"))

    def test_parametrization_values_constant_and_inline(self) -> None:
        self.edit(TEST, "VALUES = (1, 2)", "VALUES = (1, 2, 3)")
        self.assertEqual(self.reasons()["guard"], ["тест test_guard: изменено VALUES"])
        self.edit(TEST, '"v", VALUES)', '"v", (1, 2))')
        self.measure(PLANTS)
        self.edit(TEST, '"v", (1, 2))', '"v", (1, 2, 3))')
        self.assertEqual(self.reasons()["guard"], ["тест test_guard: изменено test_guard"])

    def test_every_binding_of_a_module_name_counts(self) -> None:
        self.edit(TEST, "COUNTER = 1\n", "COUNTER = 5\n")  # первая привязка
        self.assertIn("COUNTER", self.changed("module_state"))
        self.measure(PLANTS)
        self.edit(TEST, "COUNTER += 1", "COUNTER += 2")
        self.assertIn("COUNTER", self.changed("module_state"))

    def test_a_module_with_code_the_script_does_not_model_is_taken_whole(self) -> None:
        self.edit(CP + "tests/e2e/_conditional.py", 'get("X_FLAG")', 'get("Y_FLAG")')
        self.assertIn("<модуль>", self.changed("module_state"))  # условие if
        self.measure(PLANTS)
        self.edit(CP + "tests/e2e/_fallback.py", "    FAST = 0", "    FAST = -1")
        self.assertIn("<модуль>", self.changed("module_state"))  # ветка except
        self.edit(CP + "tests/e2e/test_skipmod.py", '"json"', '"yaml"')
        self.assertIn("<модуль>", self.changed("skipmod"))  # код модуля при импорте

    def test_an_expect_without_a_name_is_the_whole_module(self) -> None:
        self.edit(CP + "tests/e2e/test_skipmod.py", "    assert True", "    assert 1")
        self.assertIn("<модуль>", self.changed("wholefile"))

    def test_data_files_of_the_test_directory_belong_to_it_by_bytes(self) -> None:
        self.edit(CP + "tests/e2e/golden/doc.json", '"a": 1', '"a": 2')
        (self.repo / CP / "tests/e2e/golden/cert.der").write_bytes(b"\x30\x82\xff\xfd\x00")
        self.assertLessEqual({"golden/doc.json", "golden/cert.der"}, self.changed("guard"))

    def test_pytest_settings_of_every_form_belong_to_it_other_sections_do_not(self) -> None:
        self.edit(CP + "pyproject.toml", "line-length = 100", "line-length = 120")
        self.assertEqual(self.reasons()["guard"], [])
        self.edit(CP + "pyproject.toml", 'addopts = ["-q"]', 'addopts = ["-x"]')  # [tool.pytest]
        self.assertIn(CP + "pyproject.toml", self.changed("guard"))
        self.measure(PLANTS)
        self.edit(CP + "pyproject.toml", 'asyncio_mode = "auto"', 'asyncio_mode = "strict"')
        self.assertIn(CP + "pyproject.toml", self.changed("guard"))
        self.measure(PLANTS)
        self.edit(CP + "pytest.ini", "minversion = 9.0", "minversion = 9.1")
        self.assertIn(CP + "pytest.ini", self.changed("guard"))
        for rel, old, new in [
            (CP + "pytest.toml", "true", "false"),
            (CP + ".pytest.toml", '"classic"', '"count"'),
            (CP + ".pytest.ini", "skip", "xfail"),
            (CP + "setup.cfg", "INFO", "DEBUG"),
            (CP + "tox.ini", "log_cli = false", "log_cli = true"),
            (CP + "tests/pytest.ini", "= error", "= ignore"),  # ближе к тесту
            ("pyproject.toml", '"-ra"', '"-rA"'),  # выше корня тестов
        ]:
            self.measure(PLANTS)
            self.edit(rel, old, new)
            self.assertIn(rel, self.changed("guard"), rel)
        self.measure(PLANTS)
        self.edit(CP + "setup.cfg", "name = x", "name = y")
        self.edit(CP + "tox.ini", "envlist = py", "envlist = py314")
        self.assertEqual(self.reasons()["guard"], [])

    def test_a_non_python_test_is_compared_whole(self) -> None:
        self.edit(CP + "tests/e2e/test_contract.sh", "echo ok", "echo fine")
        self.assertEqual(self.reasons()["shell"], ["тест tests/e2e/test_contract.sh: файл изменён"])
        missing = [plant("missing", ["tests/e2e/missing.sh"])]
        self.measure(missing)
        self.assertEqual(
            self.reasons(missing)["missing"], ["тест tests/e2e/missing.sh: нет в дереве"]
        )

    def test_the_tests_root_may_be_the_repository_root(self) -> None:
        rooted = [plant("rooted", [CP + E + "test_far"]), plant("rguard", [CP + E + "test_guard"])]
        self.measure(rooted)
        self.edit(CP + "tests/_far.py", "    return 1", "    return 2")
        self.edit(CP + "tests/conftest.py", "    return 11", "    return 21")
        reasons = self.reasons(rooted, tests_root=".")
        self.assertEqual(reasons["rooted"], ["тест test_far: изменено far"])
        self.assertIn("изменено", reasons["rguard"][0])
        self.assertIn("stand", reasons["rguard"][0].split("изменено ", 1)[1])


class ImportTimeCode(Repo):
    """Код, который pytest исполняет при импорте, без ссылки теста на имя: запасной путь берёт
    такой модуль целиком, где бы он ни стоял в цепочке."""

    CASES = [
        (CP + "tests/envcase/conftest.py", '"CONF_MODE", "1"', '"CONF_MODE", "2"'),
        (CP + "tests/envcase/__init__.py", '"PKG_MODE", "1"', '"PKG_MODE", "2"'),
        (CP + "tests/_envplug.py", '"PLUG_MODE", "1"', '"PLUG_MODE", "2"'),
        (CP + "tests/_env.py", '"SIDE_MODE", "1"', '"SIDE_MODE", "2"'),
        (CP + "tests/envcase/_flag.py", '["FLAG"] = "1"', '["FLAG"] = "2"'),
        (CP + "tests/envcase/_attr.py", 'sep_alias = "/"', 'sep_alias = "\\\\"'),
        (CP + "tests/envcase/_under.py", 'join("a")', 'join("b")'),
        (CP + "tests/envcase/_tc.py", "    VALUE = 1", "    VALUE = 2"),
        (CP + "tests/envcase/_ne.py", '["NE"] = "1"', '["NE"] = "2"'),
        (CP + "tests/envcase/_with.py", "suppress(Exception)", "suppress(OSError)"),
        (CP + "tests/envcase/_flagif.py", "DEBUG = False", "DEBUG = True"),
    ]

    def test_every_module_executed_at_import_counts_whole(self) -> None:
        for rel, old, new in self.CASES:
            self.edit(rel, old, new)
            self.assertIn("<модуль>", self.changed("env"), rel)
            self.measure(PLANTS)

    def test_registries_and_class_bodies_count_whole(self) -> None:
        for rel, old, new in [
            (CP + "tests/envcase/_registry.py", "    return 10", "    return 11"),
            (CP + "tests/envcase/_classbody.py", '["LIMIT"] = "1"', '["LIMIT"] = "2"'),
            (CP + "tests/envcase/_subreg.py", "    value = 10", "    value = 11"),
        ]:
            self.edit(rel, old, new)
            self.assertIn("<модуль>", self.changed("registry"), rel)
            self.measure(PLANTS)

    def test_a_name_from_two_imports_follows_both(self) -> None:
        self.edit(CP + "tests/_fast.py", "    return 1", "    return 3")
        self.assertIn("h", self.changed("multi"))  # из _fast, хотя последний импорт — _slow

    def test_plugins_named_by_a_string_an_annotation_or_an_addition(self) -> None:
        self.edit(CP + "tests/_plug_str.py", "    yield", "    yield None")
        self.edit(CP + "tests/_plug_aug.py", "    yield", "    yield None")
        self.assertLessEqual({"plug_str", "plug_aug"}, self.changed("other"))

    def test_a_test_outside_a_package_follows_its_path_and_siblings(self) -> None:
        self.edit(CP + "tests/_far.py", "    return 1", "    return 2")
        self.assertIn("far", self.changed("nopkg"))
        self.measure(PLANTS)
        self.edit(CP + "tests/integration/_sib.py", "    return 1", "    return 2")
        self.assertIn("s", self.changed("nopkg"))


class Inputs(Repo):
    def test_files_the_test_reads_outside_its_directory_are_its_inputs(self) -> None:
        self.edit("contracts/agent.json", '"v": 1', '"v": 2')
        write(self.repo, {"contracts/new.json": "{}\n"})
        self.assertEqual(
            self.reasons()["contract"],
            ["вход contracts: изменено contracts/agent.json; добавлено contracts/new.json"],
        )

    def test_an_input_path_is_literal_not_a_glob(self) -> None:
        write(self.repo, {"contracts/x.json": "{}\n"})  # шаблон [x].json совпал бы и с ним
        self.measure(PLANTS)
        self.edit("contracts/x.json", "{}", '{"y": 0}')
        self.assertEqual(self.reasons()["bracket"], [])
        self.edit("contracts/[x].json", '"x": 1', '"x": 2')
        self.assertEqual(
            self.reasons()["bracket"], ["вход contracts/[x].json: изменено contracts/[x].json"]
        )

    def test_a_missing_input_is_measured(self) -> None:
        raws = [plant("lost", [E + "test_other"], inputs=["nowhere"])]
        self.measure(raws)
        self.assertEqual(self.reasons(raws)["lost"], ["вход nowhere: нет в дереве"])


class RunLog(Repo):
    def test_the_last_red_run_of_each_planting_is_its_base(self) -> None:
        self.edit(CP + "tests/e2e/_deep.py", "    return 1", "    return 2")
        self.measure([PLANTS[0]])  # guard перемерена после правки, chain — нет
        self.assertEqual(self.reasons()["guard"], [])
        self.assertIn("deep", self.changed("chain"))

    def test_a_planting_without_a_red_run_with_a_tree_and_a_spec_is_measured(self) -> None:
        tree = self.runs["guard"].tree
        spec = {k: self.runs[k].spec for k in self.runs}
        self.runs.pop("guard")
        self.runs["noisy"] = pa.Run("GREEN!", tree, spec["noisy"])
        self.runs["getter"] = pa.Run("RED-OTHER", tree, spec["getter"])
        self.runs["setter"] = pa.Run("COLLECT?", tree, spec["setter"])
        self.runs["limit"] = pa.Run("RED", None, spec["limit"])
        self.runs["span"] = pa.Run("RED", "0" * 40, spec["span"])
        self.runs["semi"] = pa.Run("RED", tree, spec["guard"])  # чужой spec
        self.runs["bytes"] = pa.Run("RED", tree, None)
        reasons = self.reasons()
        self.assertEqual(reasons["guard"], ["не измерялась"])
        self.assertEqual(reasons["noisy"], ["последний прогон GREEN!"])
        self.assertEqual(reasons["getter"], ["последний прогон RED-OTHER"])
        self.assertEqual(reasons["setter"], ["последний прогон COLLECT?"])
        self.assertEqual(reasons["limit"], ["в журнале нет дерева прогона"])
        self.assertEqual(reasons["span"], ["дерева прогона 000000000000 нет в репозитории"])
        self.assertEqual(reasons["semi"], ["описание посадки изменено"])
        self.assertEqual(reasons["bytes"], ["в журнале нет spec"])

    def test_a_changed_replacement_expectation_or_input_changes_the_spec(self) -> None:
        changed = [
            {**PLANTS[0], "new": "return x < 0"},
            {**PLANTS[1], "expect": [E + "test_uses"]},
            {**PLANTS[2], "inputs": ["contracts"]},
        ]
        self.assertEqual(
            self.reasons(changed),
            {label: ["описание посадки изменено"] for label in ("guard", "noisy", "getter")},
        )

    def test_the_log_file_keeps_the_last_row_of_a_label(self) -> None:
        log = self.aside / "runs.jsonl"
        tree, spec = self.runs["guard"].tree, self.runs["guard"].spec
        rows = [
            {"label": "guard", "verdict": "GREEN!", "tree": tree, "spec": spec},
            {"label": "guard", "verdict": "RED", "tree": tree, "spec": spec, "rc": 1},
            {"label": "getter", "verdict": "RED", "tree": tree, "spec": spec},
            {"label": "getter", "verdict": "RED-OTHER", "tree": tree, "spec": spec},
        ]
        log.write_text("\n".join(json.dumps(r) for r in rows) + "\n\n")
        runs = pa.load_log(log)
        self.assertEqual((runs["guard"].verdict, runs["getter"].verdict), ("RED", "RED-OTHER"))
        log.write_text('{"label": "guard"}\n')
        with self.assertRaises(pa.InputError):
            pa.load_log(log)  # без verdict
        log.write_text("")
        self.assertEqual(pa.load_log(log), {})


class CommandLine(Repo):
    def run_main(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pa.main(["--repo-root", str(self.repo), *args])
        return code, out.getvalue(), err.getvalue()

    def files(self, raws: list[dict[str, Any]]) -> tuple[str, str]:
        plants, log = self.aside / "cli.json", self.aside / "cli.jsonl"
        plants.write_text(json.dumps(raws, ensure_ascii=False))
        log.write_text(
            "\n".join(
                json.dumps({"label": k, "verdict": r.verdict, "tree": r.tree, "spec": r.spec})
                for k, r in self.runs.items()
            )
        )
        return str(plants), str(log)

    def test_the_text_report_lists_remeasures_dry_and_mismatches(self) -> None:
        plants, log = self.files(PLANTS[:3])
        self.edit(MOD, "    return x > 0", "    x = abs(x)\n    return x > 0")
        self.edit(MOD, "    return 2", "    return 2  # return 2")
        code, out, _ = self.run_main("compare", "--plantings", plants, "--log", log)
        self.assertEqual(code, 1)
        self.assertEqual(
            out.splitlines(),
            [
                "перемерить: 1",
                "  guard   код guarded",
                "сухо: 1 — getter",
                "образец не совпал: 1",
                "  noisy   образец в дереве 2 раз вместо 1",
            ],
        )

    def test_the_json_report_carries_found_and_expected(self) -> None:
        plants, log = self.files(PLANTS[:1])
        self.edit(MOD, "    return 2", "    return x > 0")
        code, out, _ = self.run_main("compare", "--plantings", plants, "--log", log, "--json")
        self.assertEqual(code, 1)
        row = json.loads(out)[0]
        self.assertEqual((row["found"], row["expected"]), (2, 1))

    def test_bad_input_exits_two(self) -> None:
        plants, log = self.files(PLANTS[:1])
        # Существующие каталоги вне репозитория: путь отклоняется по форме, а не по наличию.
        for root in ("nowhere", str(self.aside), "../aside"):
            code, _, err = self.run_main(
                "compare", "--plantings", plants, "--log", log, "--tests-root", root
            )
            self.assertEqual(code, 2, root)
            self.assertIn("--tests-root", err)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pa.main(["--repo-root", str(self.repo), "compare", "--plantings", plants])  # без --log

    def test_a_clean_run_exits_zero_and_reports_reasons_in_json(self) -> None:
        plants, log = self.files(PLANTS[:2])
        self.edit(MOD, "    return x > 0", "    x = abs(x)\n    return x > 0")
        code, out, _ = self.run_main("compare", "--plantings", plants, "--log", log, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(
            [(r["label"], r["reasons"]) for r in json.loads(out)],
            [("guard", ["код guarded"]), ("noisy", [])],
        )

    def test_a_missing_or_broken_plantings_file_exits_two(self) -> None:
        _, log = self.files(PLANTS[:1])
        broken = self.aside / "broken.json"
        broken.write_text("[{")
        for plants in (str(self.aside / "absent.json"), str(broken)):
            code, _, err = self.run_main("compare", "--plantings", plants, "--log", log)
            self.assertEqual(code, 2, plants)
            self.assertIn("plantings_affected", err)

    def test_spec_prints_the_hash_the_log_must_carry(self) -> None:
        plants, _ = self.files(PLANTS[:2])
        code, out, _ = self.run_main("spec", "--plantings", plants)
        self.assertEqual(code, 0)
        self.assertEqual(
            out.split(), ["guard", pa.spec_hash(PLANTS[0]), "noisy", pa.spec_hash(PLANTS[1])]
        )


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
        loaded = pa.load_plantings(harness)
        self.assertEqual([p.label for p in loaded], [p["label"] for p in PLANTS])
        self.assertEqual(loaded[4].count, 2)
        self.assertEqual(loaded[0].spec, pa.spec_hash(PLANTS[0]))

    def test_a_json_file_supplies_a_list_or_a_plantings_object(self) -> None:
        listed, wrapped = self.dir / "a.json", self.dir / "b.json"
        listed.write_text(json.dumps(PLANTS))
        wrapped.write_text(json.dumps({"plantings": PLANTS}))
        self.assertEqual(len(pa.load_plantings(listed)), len(pa.load_plantings(wrapped)))

    def test_a_malformed_planting_is_refused(self) -> None:
        bad = self.dir / "bad.json"
        broken = [
            {**PLANTS[0], "expect": "tests/x.py::t"},
            {**PLANTS[0], "old": ""},
            {**PLANTS[0], "count": 0},
            {**PLANTS[0], "file": "/etc/passwd"},
            {**PLANTS[0], "file": "../outside.py"},
            {**PLANTS[0], "expect": ["/abs/test.py::t"]},
            {**PLANTS[0], "inputs": "contracts"},
            {**PLANTS[0], "inputs": ["/etc"]},
            {**PLANTS[0], "file": "a/../../outside.py"},
        ]
        for raw in broken:
            bad.write_text(json.dumps([raw]))
            with self.assertRaises(pa.InputError, msg=repr(raw)):
                pa.load_plantings(bad)
        bad.write_text(json.dumps([PLANTS[0], PLANTS[0]]))
        with self.assertRaises(pa.InputError):
            pa.load_plantings(bad)  # метки повторяются


class Snapshot(Repo):
    def test_the_snapshot_is_the_working_copy_and_leaves_the_index_alone(self) -> None:
        self.git("add", MOD)
        write(self.repo, {CP + "tests/new_helper.py": "X = 1\n", "ignored/junk.txt": "x\n"})
        (self.repo / MOD).write_text(str(TREE[MOD]) + "\n# правка\n")
        (self.repo / CP / "app/mod2.py").unlink()
        status = self.git("status", "--porcelain")
        tree = pa.snapshot(self.repo)
        self.assertEqual(self.git("status", "--porcelain"), status)
        files = self.git("ls-tree", "-r", "--name-only", tree).split()
        self.assertIn(CP + "tests/new_helper.py", files)
        self.assertIn(CP + "tests/e2e/golden/keep.local", files)  # отслеживаемый игнорируемый
        self.assertNotIn("ignored/junk.txt", files)
        self.assertNotIn(CP + "app/mod2.py", files)  # удалённый
        self.assertIn("# правка", pa.Source(self.repo, tree).read(MOD) or "")
        (self.repo / CP / "tests/e2e/golden/keep.local").unlink()  # отслеживаемый игнорируемый
        later = pa.snapshot(self.repo)
        self.assertNotIn(
            CP + "tests/e2e/golden/keep.local",
            self.git("ls-tree", "-r", "--name-only", later).split(),
        )
        source = pa.Source(self.repo, later)
        self.assertIsNone(source.read_bytes("no file"))  # путь с одним пробелом
        source.close()

    def test_an_edit_of_the_same_size_in_the_same_second_is_not_lost(self) -> None:
        # Индекс с отметками сверял бы только размер и время изменения: правка того же размера с
        # прежним временем для него — неизменный файл (ctime и прочие отметки git не сверяет).
        self.git("config", "core.trustctime", "false")
        self.git("config", "core.checkStat", "minimal")
        path = self.repo / CP / "tests/e2e/_deep.py"
        past = path.stat().st_mtime_ns - 100 * 10**9
        os.utime(path, ns=(past, past))
        self.git("update-index", "--refresh")  # запись индекса старше его самого — не «гоночная»
        path.write_text(path.read_text().replace("return 1", "return 2"))
        os.utime(path, ns=(past, past))  # размер и время — прежние
        tree = pa.snapshot(self.repo)
        self.assertIn("return 2", pa.Source(self.repo, tree).read(CP + "tests/e2e/_deep.py") or "")


if __name__ == "__main__":
    unittest.main()
