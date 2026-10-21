"""Tests for the sentence-length warning in `graph.py lint`."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_core import graph, note, write  # noqa: E402

LONG = " ".join(["word"] * 35) + "."
SHORT = "This sentence is short."


class TestSentenceLint(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engine = self.tmp / "engine"
        self.data = self.tmp / "data"
        self.paths = graph.Paths(self.engine, self.data)

    def lint(self, body: str) -> tuple[str, int]:
        write(self.data / "a.md", note("A", body, extra="keywords: [a]\n"))
        out = StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            graph.cmd_lint(Namespace(), self.paths)
        return out.getvalue(), ctx.exception.code

    def test_long_sentence_warns_without_failing(self):
        out, code = self.lint(LONG)
        self.assertIn("a: 1 sentence(s) over 30 words (longest 35)", out)
        self.assertEqual(code, 0)

    def test_short_sentences_pass(self):
        out, _ = self.lint(f"{SHORT} {SHORT}\n- {SHORT}")
        self.assertNotIn("sentence(s)", out)

    def test_code_table_heading_comment_ignored(self):
        body = f"```\n{LONG}\n```\n| {LONG} |\n## {LONG}\n<!-- {LONG} -->\n{SHORT}"
        out, _ = self.lint(body)
        self.assertNotIn("sentence(s)", out)

    def test_links_code_and_urls_count_as_one_word(self):
        body = ("See [[some-long-note-name]] and `a b c d e f g h` and https://example.com/a/b "
                + " ".join(["w"] * 15) + ".")
        out, _ = self.lint(body)
        self.assertNotIn("sentence(s)", out)

    def test_one_warning_per_note(self):
        out, _ = self.lint(f"{LONG} {LONG}\n- {LONG}")
        self.assertEqual(out.count("sentence(s)"), 1)
        self.assertIn("3 sentence(s)", out)

    def test_lines_split_sentences(self):
        half = " ".join(["w"] * 15)
        out, _ = self.lint(f"{half}\n{half}")
        self.assertNotIn("sentence(s)", out)

    def test_task_notes_are_skipped(self):
        write(self.data / "t.md", note("T", LONG, extra="keywords: [t]\ntype: task\n"))
        write(self.data / "inbox" / "p" / "0001-x.md", note("X", LONG, extra="keywords: [x]\n"))
        out = StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            graph.cmd_lint(Namespace(), self.paths)
        self.assertNotIn("sentence(s)", out.getvalue())

    def test_limit_is_module_constant(self):
        with mock.patch.object(graph, "MAX_SENTENCE_WORDS", 50):
            out, _ = self.lint(LONG)
        self.assertNotIn("sentence(s)", out)


class TestEntryNoteLint(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.paths = graph.Paths(self.tmp / "engine", self.tmp / "data")

    def lint(self, rel: str, size: int) -> tuple[str, int]:
        body = (SHORT + " ") * (size // (len(SHORT) + 1))
        write(self.paths.data / rel, note("N", body, extra="keywords: [n]\n"))
        out = StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            graph.cmd_lint(Namespace(), self.paths)
        return out.getvalue(), ctx.exception.code

    def test_big_status_is_an_error(self):
        out, code = self.lint("projects/p/p-status.md", 6000)
        self.assertIn("ERROR  projects/p/p-status: project entry note has", out)
        self.assertIn("max 5000", out)
        self.assertIn("rotate p", out)
        self.assertEqual(code, 1)

    def test_small_status_is_silent(self):
        out, code = self.lint("projects/p/p-status.md", 4000)
        self.assertNotIn("project entry note", out)
        self.assertEqual(code, 0)

    def test_big_overview_is_an_error_to_fix_by_hand(self):
        out, code = self.lint("projects/p/p-overview.md", 6000)
        self.assertIn("ERROR  projects/p/p-overview: project entry note has", out)
        self.assertIn("move older content to p-history", out)
        self.assertNotIn("rotate", out)
        self.assertEqual(code, 1)

    def test_other_note_is_not_checked(self):
        out, _ = self.lint("notes/x.md", 6000)
        self.assertNotIn("project entry note", out)

    def test_limit_is_module_constant(self):
        with mock.patch.object(graph, "MAX_ENTRY_CHARS", 10000):
            out, _ = self.lint("projects/p/p-status.md", 6000)
        self.assertNotIn("project entry note", out)


if __name__ == "__main__":
    unittest.main()
