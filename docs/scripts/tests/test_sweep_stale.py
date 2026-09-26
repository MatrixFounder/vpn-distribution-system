"""Тесты развёртки устаревших чисел (``docs/scripts/sweep_stale.py``, WI-30): числовая и
словесные формы числа, поиск без учёта регистра, разметка подписанной истории, CLI."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sweep_stale as sweep  # noqa: E402


class NumeralForms(unittest.TestCase):
    def test_simple_numbers_carry_the_cases_used_in_prose(self) -> None:
        self.assertEqual(sweep.numeral_forms(8)[:2], ["восемь", "восьми"])
        self.assertIn("пятнадцати", sweep.numeral_forms(15))
        self.assertIn("две", sweep.numeral_forms(2))
        self.assertIn("двух", sweep.numeral_forms(2))
        self.assertEqual(sweep.numeral_forms(100), ["сто", "ста"])

    def test_compound_numbers_combine_their_parts(self) -> None:
        self.assertIn("двадцати восьми", sweep.numeral_forms(28))
        self.assertIn("сто двадцать", sweep.numeral_forms(120))
        self.assertEqual(sweep.numeral_forms(1000), [])

    def test_digit_forms_include_thousand_separators(self) -> None:
        forms = sweep.number_forms(1000)
        self.assertIn("1 000", forms)
        self.assertIn("1 000", forms)
        self.assertFalse(any("тысяч" in f for f in forms))  # тысячи словом не разворачиваются


class Matching(unittest.TestCase):
    def setUp(self) -> None:
        self.pattern = sweep.compile_pattern(
            {"name": "частота", "value": 8, "forms": ["{n} r/s", "{n}r/s", "{n} в секунду"]}
        )

    def test_number_and_word_forms_match_and_other_numbers_do_not(self) -> None:
        for text in ("8 r/s", "8r/s", "8 в секунду", "восьми в секунду", "ВОСЕМЬ в секунду"):
            self.assertTrue(self.pattern.regex.search(text), text)
        for text in ("18 r/s", "28 в секунду", "8 запросов", "80 r/s"):
            self.assertFalse(self.pattern.regex.search(text), text)

    def test_regex_and_literal_patterns_ignore_case(self) -> None:
        rx = sweep.compile_pattern({"name": "x", "regex": "два сертификата[^.;]*целиком"})
        self.assertTrue(
            rx.regex.search("Два сертификата (2 × 5 > 15) выбирают частоту парка целиком")
        )
        lit = sweep.compile_pattern({"name": "y", "literal": "proxy_max_temp_file_size 0"})
        self.assertTrue(lit.regex.search("    PROXY_MAX_TEMP_FILE_SIZE 0;"))
        self.assertFalse(lit.regex.search("proxy_max_temp_file_size 8m;"))

    def test_decimal_values_are_literal(self) -> None:
        p = sweep.compile_pattern({"name": "z", "value": "16,2", "forms": ["{n} части"]})
        self.assertTrue(p.regex.search("16,2 части в секунду"))
        self.assertFalse(p.regex.search("116,2 части"))

    def test_a_pattern_without_a_source_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            sweep.compile_pattern({"name": "empty"})


class HistoryAndCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "task.md").write_text(
            "Частота парка — восемь в секунду.\n"
            "В раунде 7 стояло 8 r/s, теперь 15.\n"
            "Ничего устаревшего здесь нет.\n",
            encoding="utf-8",
        )
        (self.root / "docs" / "report.md").write_text(
            "Текущее: 8r/s у зоны.\n## Раунд 1 — роаст\nВ раунде 1 было 8 в секунду.\n",
            encoding="utf-8",
        )
        self.patterns = self.root / "sweep.json"
        self.patterns.write_text(
            json.dumps(
                {
                    "task": "t",
                    "history_sections": {"docs/report.md": "^## Раунд 1 — роаст"},
                    "patterns": [
                        {
                            "name": "частота",
                            "value": 8,
                            "current": "15 r/s",
                            "forms": ["{n} r/s", "{n}r/s", "{n} в секунду"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def run_cli(self, *extra: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = sweep.main(
                [
                    "--patterns",
                    str(self.patterns),
                    "--root",
                    str(self.root),
                    "docs/task.md",
                    "docs/report.md",
                    *extra,
                ]
            )
        return code, out.getvalue()

    def test_history_markers_and_sections_separate_history_from_live_hits(self) -> None:
        s = sweep.load_sweep(self.patterns)
        hits = sweep.run(s, self.root, ["docs/task.md", "docs/report.md"])
        by_line = {(h.path, h.line): h.history for h in hits}
        self.assertEqual(
            by_line,
            {
                ("docs/task.md", 1): False,
                ("docs/task.md", 2): True,
                ("docs/report.md", 1): False,
                ("docs/report.md", 3): True,
            },
        )

    def test_cli_reports_counts_and_strict_fails_on_live_hits(self) -> None:
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("совпадений 4, в истории 2, вне истории 2", out)
        self.assertIn("[история]", out)
        self.assertIn("сейчас: 15 r/s", out)
        code, out = self.run_cli("--strict", "--live-only")
        self.assertEqual(code, 1)
        self.assertNotIn("[история]", out)

    def test_json_output_lists_files_and_hits(self) -> None:
        code, out = self.run_cli("--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["files"], ["docs/report.md", "docs/task.md"])
        self.assertEqual(len(data["hits"]), 4)

    def test_an_explicit_file_that_does_not_exist_is_refused(self) -> None:
        """Явно названного файла нет — код 2 и его имя, а не «совпадений 0»: список файлов,
        переданный оболочкой одним словом, иначе давал ложную чистоту (001.25)."""
        err = io.StringIO()
        with redirect_stderr(err):
            code, out = self.run_cli("docs/no-such-file.md")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("docs/no-such-file.md", err.getvalue())

    def test_an_explicit_file_of_any_extension_is_swept(self) -> None:
        """Явно названный файл без расширения из списка (Dockerfile, .env.example) развёртывается,
        а не выпадает молча с «совпадений 0» (роаст 001.25, раунд 9)."""
        (self.root / "Dockerfile").write_text("ENV RATE=8r/s\n", encoding="utf-8")
        code, out = self.run_cli("Dockerfile", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("Dockerfile", data["files"])
        self.assertIn("Dockerfile", {hit["path"] for hit in data["hits"]})

    def test_an_explicit_file_that_is_not_text_is_refused(self) -> None:
        """Явно названный файл, который не читается как UTF-8, — код 2 и его имя (раунд 9)."""
        (self.root / "logo.bin").write_bytes(b"\xff\xfe\x00\x81")
        err = io.StringIO()
        with redirect_stderr(err):
            code, out = self.run_cli("logo.bin")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("logo.bin", err.getvalue())


class WrappedPhrases(unittest.TestCase):
    def test_a_phrase_wrapped_across_lines_is_found_once(self) -> None:
        """Устаревшая формулировка, перенесённая через строку в комментарии или абзаце, видна
        развёртке (роаст 001.25, раунд 3: «момент / транзакции» в комментарии теста не нашлась);
        фраза в одной строке не дублируется совпадением пары."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "code.py").write_text(
                "# лист от now (момент\n"
                "# транзакции enrollment) — прежняя формулировка\n"
                "x = 1  # момент транзакции в одной строке\n"
                "y = 2\n",
                encoding="utf-8",
            )
            patterns = root / "sweep.json"
            patterns.write_text(
                json.dumps(
                    {
                        "task": "t",
                        "patterns": [
                            {"name": "момент", "regex": "момент\\w* транзакции", "current": "x"}
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            hits = sweep.run(sweep.load_sweep(patterns), root, ["code.py"])
            self.assertEqual([(h.line, h.history) for h in hits], [(1, False), (3, False)])
            self.assertIn("момент транзакции", hits[0].text)

    def test_a_phrase_split_across_python_string_literals_is_found(self) -> None:
        """Неявная конкатенация литералов Python рвёт фразу кавычками (``"… момент "`` /
        ``"транзакции"``) — склейка пары строк снимает их (роаст 001.25, раунд 4)."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "code.py").write_text(
                'message = (\n    "лист от now (момент "\n    f"транзакции {name})",\n)\n',
                encoding="utf-8",
            )
            patterns = root / "sweep.json"
            patterns.write_text(
                json.dumps(
                    {
                        "task": "t",
                        "patterns": [
                            {"name": "момент", "regex": "момент\\w* транзакции", "current": "x"}
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            hits = sweep.run(sweep.load_sweep(patterns), root, ["code.py"])
            self.assertEqual([(h.line, h.history) for h in hits], [(2, False)])


if __name__ == "__main__":
    unittest.main()
