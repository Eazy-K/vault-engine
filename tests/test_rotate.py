"""Tests for tools/rotate.py (`rotate`) and the status-note warning in `reinforce`."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_core import GRAPH_PATH, graph, note, write  # noqa: E402

sys.path.insert(0, str(GRAPH_PATH.parent))
import rotate  # noqa: E402

rotate.g = graph  # other test modules replace sys.modules["graph"]; bind to ours

ITEMS = [
    "- 2026-03-01 first, the oldest",
    "- 2026-03-02 second\n  continuation of second",
    "- 2026-03-03 third",
    "- 2026-03-04 fourth",
    "- 2026-03-05 fifth",
    "- 2026-03-06 sixth, the newest",
]
FILLER = "filler line here\n" * 400


def status_text(items=ITEMS, heading="Where we are", extra="") -> str:
    """A status note whose recent section lists `items` newest first."""
    body = ("Purpose line.\n\n## " + heading + "\n" + "\n".join(reversed(items)) +
            "\n- undated bullet stays\n\n## Open work\n- something\n" + extra)
    return note("p: status", body, extra="keywords: [p, status]\n")


class RotateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = self.tmp / "data"
        self.engine = self.tmp / "engine"
        self.engine.mkdir()
        self.paths = graph.Paths(self.engine, self.data)
        self.status = self.data / "projects" / "p" / "p-status.md"
        self.history = self.data / "projects" / "p" / "p-history.md"

    def run_rotate(self, *flags: str, project: str = "p") -> tuple[str, int]:
        args = Namespace(project=project, no_commit="--no-commit" in flags,
                         dry_run="--dry-run" in flags)
        out = StringIO()
        code = 0
        with mock.patch.object(graph, "default_paths", return_value=self.paths), \
                redirect_stdout(out):
            try:
                rotate.cmd_rotate(args)
            except SystemExit as exc:
                code = 1 if isinstance(exc.code, str) else (exc.code or 0)
                if isinstance(exc.code, str):
                    out.write(exc.code)
        return out.getvalue(), code

    def lint(self) -> tuple[str, int]:
        out = StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            graph.cmd_lint(Namespace(), self.paths)
        return out.getvalue(), ctx.exception.code


class TestRotate(RotateBase):
    def test_moves_oldest_and_keeps_five(self):
        write(self.status, status_text())
        out, code = self.run_rotate("--no-commit")
        self.assertEqual(code, 0)
        self.assertIn("rotated 1 item(s) from projects/p/p-status to projects/p/p-history "
                      "(2026-03-01..2026-03-01); 5 remain", out)
        text = self.status.read_text(encoding="utf-8")
        self.assertNotIn("the oldest", text)
        self.assertEqual(len(graph.dated_items(text)), 5)
        self.assertIn("- undated bullet stays", text)
        self.assertIn("## Open work\n- something\n", text)
        history = self.history.read_text(encoding="utf-8")
        self.assertIn("## Journal\n- 2026-03-01 first, the oldest\n", history)

    def test_only_removed_lines_change(self):
        original = status_text()
        write(self.status, original)
        self.run_rotate("--no-commit")
        expected = original.replace("- 2026-03-01 first, the oldest\n", "")
        self.assertEqual(self.status.read_text(encoding="utf-8"), expected)

    def test_two_items_move_in_chronological_order_with_continuation(self):
        items = ["- 2026-02-01 older\n  more about older"] + ITEMS
        write(self.status, status_text(items))
        out, _ = self.run_rotate("--no-commit")
        self.assertIn("rotated 2 item(s)", out)
        self.assertIn("(2026-02-01..2026-03-01)", out)
        history = self.history.read_text(encoding="utf-8")
        self.assertIn("## Journal\n- 2026-02-01 older\n  more about older\n"
                      "- 2026-03-01 first, the oldest\n", history)

    def test_same_date_lower_item_counts_as_older(self):
        # The section lists (top to bottom): sixth..second, then "upper", then "lower".
        items = ["- 2026-03-01 lower", "- 2026-03-01 upper"] + ITEMS[2:]
        write(self.status, status_text(items))
        self.run_rotate("--no-commit")
        history = self.history.read_text(encoding="utf-8")
        self.assertIn("lower", history)
        self.assertNotIn("upper", history)

    def test_second_run_is_a_noop(self):
        write(self.status, status_text())
        self.run_rotate("--no-commit")
        before = (self.status.read_bytes(), self.history.read_bytes())
        out, code = self.run_rotate("--no-commit")
        self.assertEqual(code, 0)
        self.assertIn("nothing to rotate: projects/p/p-status has 5 dated items (max 5)", out)
        self.assertEqual((self.status.read_bytes(), self.history.read_bytes()), before)

    def test_created_history_links_status_and_lints_clean(self):
        write(self.status, status_text())
        self.run_rotate("--no-commit")
        history = self.history.read_text(encoding="utf-8")
        self.assertTrue(history.startswith("---\nkeywords: [p, history, journal]\n"))
        self.assertIn("[[p-status]]", history)
        self.assertIn("# p: history\n\n## Journal\n", history)
        out, code = self.lint()
        self.assertEqual(code, 0, out)
        self.assertIn("0 errors", out)

    def test_existing_history_keeps_other_sections(self):
        write(self.status, status_text())
        write(self.history, "---\nkeywords: [p]\n---\n# p: history\n\n## Journal\n"
                            "- 2026-01-01 earlier entry\n\n## Notes\nkeep me\n")
        self.run_rotate("--no-commit")
        history = self.history.read_text(encoding="utf-8")
        self.assertEqual(history, "---\nkeywords: [p]\n---\n# p: history\n\n## Journal\n"
                                  "- 2026-01-01 earlier entry\n- 2026-03-01 first, the oldest\n"
                                  "\n## Notes\nkeep me\n")

    def test_history_without_journal_section_gets_one(self):
        write(self.status, status_text())
        write(self.history, "---\nkeywords: [p]\n---\n# p: history\n\nsome text\n")
        self.run_rotate("--no-commit")
        history = self.history.read_text(encoding="utf-8")
        self.assertTrue(history.endswith("some text\n\n## Journal\n- 2026-03-01 first, the oldest\n"))

    def test_custom_headings_from_config(self):
        write(self.data / "vault.config.json",
              json.dumps({"status_note": {"recent_heading": "Durum", "journal_heading": "Gunluk"}}))
        write(self.status, status_text(heading="durum"))
        write(self.history, "---\nkeywords: [p]\n---\n# h\n\n## Gunluk\n- 2026-01-01 x\n")
        out, code = self.run_rotate("--no-commit")
        self.assertEqual(code, 0, out)
        self.assertIn("- 2026-03-01 first, the oldest",
                      self.history.read_text(encoding="utf-8").split("## Gunluk")[1])

    def test_dry_run_writes_nothing(self):
        write(self.status, status_text())
        before = self.status.read_bytes()
        out, code = self.run_rotate("--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("dry-run: rotated 1 item(s)"))
        self.assertEqual(self.status.read_bytes(), before)
        self.assertFalse(self.history.exists())

    def test_missing_recent_section_fails(self):
        write(self.status, status_text(heading="Elsewhere"))
        out, code = self.run_rotate("--no-commit")
        self.assertEqual(code, 1)
        self.assertIn("status_note.recent_heading", out)
        self.assertIn("Where we are", out)

    def test_missing_status_note_fails(self):
        out, code = self.run_rotate()
        self.assertEqual(code, 1)
        self.assertIn("no status note projects/p/p-status", out)

    def test_nothing_to_rotate_but_oversized_prints_hint(self):
        write(self.status, status_text(ITEMS[1:], extra=FILLER))
        before = self.status.read_bytes()
        out, code = self.run_rotate("--no-commit")
        self.assertEqual(code, 0)
        self.assertIn("nothing to rotate", out)
        self.assertIn("still has", out)
        self.assertIn("by hand", out)
        self.assertEqual(self.status.read_bytes(), before)
        self.assertFalse(self.history.exists())

    def test_still_too_big_prints_second_line(self):
        write(self.status, status_text(extra=FILLER))
        out, _ = self.run_rotate("--no-commit")
        self.assertIn("still has", out)
        self.assertIn("move older sections to projects/p/p-history by hand", out)


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8")


class TestRotateCommit(RotateBase):
    def setUp(self):
        super().setUp()
        self.data.mkdir()
        try:
            git("init", "-q", cwd=self.data)
        except OSError:
            self.skipTest("git is not available")
        for key, value in (("user.email", "tester@example.com"), ("user.name", "Test Runner"),
                           ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            git("config", key, value, cwd=self.data)
        write(self.status, status_text())
        write(self.data / "other.md", "# other\n")
        git("add", "-A", cwd=self.data)
        git("commit", "-q", "-m", "init", cwd=self.data)
        write(self.data / "other.md", "# other changed\n")

    def test_default_commits_exactly_the_two_notes(self):
        out, _ = self.run_rotate()
        self.assertIn("committed in the data repo: chore: rotate p status note", out)
        files = git("show", "--name-only", "--format=", "HEAD", cwd=self.data).stdout.split()
        self.assertEqual(sorted(files), ["projects/p/p-history.md", "projects/p/p-status.md"])
        self.assertEqual(git("status", "--porcelain", cwd=self.data).stdout.strip(),
                         "M other.md")

    def test_no_commit_leaves_working_tree(self):
        self.run_rotate("--no-commit")
        self.assertEqual(git("log", "--format=%s", cwd=self.data).stdout.split("\n")[0], "init")
        self.assertIn("projects/p/p-status.md", git("status", "--porcelain", cwd=self.data).stdout)


class TestReinforceWarning(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dev = self.tmp / "dev"
        self.engine = self.dev / "vault-engine"
        self.data = self.dev / "vault-data"
        self.project_dir = self.dev / "p"
        for d in (self.engine, self.data, self.project_dir):
            d.mkdir(parents=True)
        self.paths = graph.Paths(self.engine, self.data)
        self.command = f'python "{GRAPH_PATH}" rotate p'

    def reinforce(self) -> list[str]:
        out = StringIO()
        with mock.patch.object(graph, "default_paths", return_value=self.paths), \
                mock.patch("pathlib.Path.cwd", return_value=self.project_dir), \
                mock.patch.dict(os.environ, {"VAULT_MACHINE": "pc-test"}), \
                mock.patch.dict(sys.modules, {"update": None, "feedback": None, "schema": None}), \
                redirect_stdout(out):
            graph.cmd_reinforce(Namespace(notes=[], task="t", rate=graph.LEARNING_RATE))
        return [line for line in out.getvalue().splitlines() if line.startswith("WARN: projects/p")]

    def context(self) -> str:
        args = Namespace(text="", seed=[], threshold=graph.DEFAULT_THRESHOLD,
                         depth=graph.DEFAULT_DEPTH, json=False, no_semantic=True,
                         project=None, no_project=False, core=False,
                         budget=graph.DEFAULT_BUDGET, no_log=True)
        out = StringIO()
        with mock.patch.object(graph, "default_paths", return_value=self.paths), \
                mock.patch("pathlib.Path.cwd", return_value=self.project_dir), \
                mock.patch("pathlib.Path.home", return_value=self.tmp / "home"), \
                redirect_stdout(out):
            graph.cmd_query(args, content=True)
        return out.getvalue()

    def test_silent_under_the_limits(self):
        write(self.data / "projects" / "p" / "p-status.md", status_text(ITEMS[1:]))
        self.assertEqual(self.reinforce(), [])

    def test_warns_about_too_many_items(self):
        write(self.data / "projects" / "p" / "p-status.md", status_text())
        self.assertEqual(self.reinforce(), [
            f'WARN: projects/p/p-status has 6 dated items under "Where we are" (max 5); '
            f"run: {self.command}"])
        self.assertNotIn("WARN: projects/p", self.context())

    def test_warns_about_size(self):
        write(self.data / "projects" / "p" / "p-status.md", status_text(ITEMS[1:], extra=FILLER))
        lines = self.reinforce()
        self.assertEqual(len(lines), 1)
        self.assertIn("chars (max 5000); run: " + self.command, lines[0])
        self.assertNotIn("dated items", lines[0])

    def test_warns_about_both_in_one_line(self):
        write(self.data / "projects" / "p" / "p-status.md", status_text(extra=FILLER))
        lines = self.reinforce()
        self.assertEqual(len(lines), 1)
        self.assertIn("6 dated items", lines[0])
        self.assertIn("chars (max 5000)", lines[0])

    def test_warns_about_big_overview(self):
        write(self.data / "projects" / "p" / "p-overview.md", note("o", FILLER))
        lines = self.reinforce()
        self.assertEqual(len(lines), 1)
        self.assertIn("WARN: projects/p/p-overview has", lines[0])
        self.assertIn("move older content to p-history by hand", lines[0])


if __name__ == "__main__":
    unittest.main()
