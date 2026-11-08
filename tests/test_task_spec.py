"""Task spec: ready/review statuses, acceptance block and budget lint rules."""
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

ACCEPT = "## Acceptance\nPass when this exits 0.\n```bash\npython -I -m unittest -q\n```\n"
HUMAN = "## Acceptance\n```bash\nhuman\n```\n"


class TestTaskSpec(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.paths = graph.Paths(self.tmp / "engine", self.tmp / "data")

    def task(self, name: str, status: str, body: str = "", budget: str | None = None) -> None:
        extra = f"type: task\nproject: p\nstatus: {status}\nkeywords: [{name}]\n"
        if budget:
            extra += f"budget: {budget}\n"
        write(self.paths.data / "inbox" / "p" / f"{name}.md", note(name, body, extra=extra))

    def lint(self) -> tuple[str, int]:
        out = StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            graph.cmd_lint(Namespace(), self.paths)
        return out.getvalue(), ctx.exception.code

    def test_ready_without_acceptance_is_error(self):
        self.task("t1", "ready", "No block here.", budget="S")
        out, code = self.lint()
        self.assertIn("ready task needs an acceptance command", out)
        self.assertNotEqual(code, 0)

    def test_ready_with_acceptance_and_budget_passes(self):
        self.task("t1", "ready", ACCEPT, budget="S")
        out, code = self.lint()
        self.assertNotIn("ready task needs", out)
        self.assertEqual(code, 0)

    def test_ready_without_budget_is_error(self):
        self.task("t1", "ready", ACCEPT)
        out, code = self.lint()
        self.assertIn("ready task needs budget: S|M|L", out)
        self.assertNotEqual(code, 0)

    def test_open_without_acceptance_is_fine(self):
        self.task("t1", "open", "No block here.")
        out, code = self.lint()
        self.assertNotIn("ready task needs", out)
        self.assertEqual(code, 0)

    def test_bad_budget_warns_on_any_status(self):
        self.task("t1", "open", budget="XL")
        out, code = self.lint()
        self.assertIn("budget must be one of", out)
        self.assertEqual(code, 0)

    def test_tasks_status_ready_filters(self):
        self.task("t-ready", "ready", ACCEPT, budget="S")
        self.task("t-open", "open")
        out = StringIO()
        with mock.patch.object(graph, "default_paths", return_value=self.paths), redirect_stdout(out):
            graph.cmd_tasks(Namespace(status="ready", project=None))
        self.assertIn("t-ready", out.getvalue())
        self.assertNotIn("t-open", out.getvalue())

    def test_statuses_include_ready_and_review(self):
        self.assertEqual(graph.TASK_STATUSES,
                         ("open", "ready", "in-progress", "review", "blocked", "done"))

    def test_acceptance_command_helper(self):
        self.assertEqual(graph.task_acceptance_command(ACCEPT), "python -I -m unittest -q")
        self.assertEqual(graph.task_acceptance_command(HUMAN), "human")
        self.assertIsNone(graph.task_acceptance_command("## Acceptance\nno block\n"))
        self.assertIsNone(graph.task_acceptance_command("## Acceptance\n```bash\n\n```\n"))
        self.assertIsNone(graph.task_acceptance_command("## Other\n```bash\nls\n```\n"))
        self.assertIsNone(graph.task_acceptance_command(
            "## Acceptance criteria\n```bash\nls\n```\n"))
        self.assertEqual(graph.task_acceptance_command(
            "## Kabul\n```bash\npytest -q\n```\n"), "pytest -q")
        self.assertEqual(graph.task_acceptance_command(
            "## Acceptance criteria\n```bash\nwrong\n```\n"
            "## Acceptance\n```bash\nright\n```\n"), "right")

    def test_files_section_helper(self):
        self.assertEqual(graph.task_files_section(
            "## Scope\nx\n## Files\n- a.py: f, 1-5\n## Acceptance\n"), "- a.py: f, 1-5")
        self.assertEqual(graph.task_files_section("## Dosyalar\n- b.py\n"), "- b.py")
        self.assertIsNone(graph.task_files_section("## Goal\nx\n"))
        self.assertIsNone(graph.task_files_section("## Files\n<!-- hint -->\n-\n"))

    def test_template_has_files_section_before_acceptance(self):
        text = (Path(__file__).resolve().parent.parent / "templates/inbox/_template.md"
                ).read_text(encoding="utf-8")
        self.assertLess(text.index("## Scope"), text.index("\n## Files"))
        self.assertLess(text.index("\n## Files"), text.index("\n## Acceptance\n"))
        self.assertIn("Do not touch", graph.task_files_section(text))


if __name__ == "__main__":
    unittest.main()
