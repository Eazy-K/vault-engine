"""Tests for tools/usage_metrics.py (transcript-based usage metrics).

Synthetic transcript trees in a temp dir only. Run with:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = str(Path(__file__).resolve().parent.parent / "tools")
sys.path.insert(0, TOOLS_DIR)

import usage_metrics  # noqa: E402


def turn(msg_id, *tools):
    """One assistant JSONL line; tools are (tool_use_id, name, input) tuples."""
    return json.dumps({"type": "assistant", "message": {
        "id": msg_id, "model": "m", "content": [
            {"type": "tool_use", "id": t, "name": n, "input": i} for t, n, i in tools]}})


def bash(tid, cmd):
    return (tid, "Bash", {"command": cmd})


class UsageMetricsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.projects = self.tmp / "projects"
        self.projects.mkdir()

    def write(self, slug, name, lines):
        d = self.projects / slug
        d.mkdir(exist_ok=True)
        (d / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_opened_after_context_bash_and_read(self):
        read_path = str(self.vault / "projects" / "p" / "p-plan.md")
        outside = str(self.tmp / "other" / "x.md")
        self.write("proj", "s1", [
            turn("m0", bash("t0", 'python "$VAULT_ENGINE/tools/graph.py" show --body early-note')),
            turn("m1", bash("t1", 'python "$VAULT_ENGINE/tools/graph.py" context "task"')),
            turn("m2", bash("t2", 'python "$VAULT_ENGINE/tools/graph.py" show --body a/one b/two')),
            turn("m3", ("t3", "Read", {"file_path": read_path}),
                 ("t4", "Read", {"file_path": outside})),
            # same tool_use id repeated on another line: counted once
            turn("m3", ("t3", "Read", {"file_path": read_path})),
        ])
        self.write("proj", "s2", [
            turn("n1", bash("u1", "python graph.py context x")),
            turn("n2", bash("u2", "python graph.py show --body a/one")),
            turn("n3", bash("u3", "ls")),
        ])
        res = usage_metrics.collect(self.projects, self.vault)
        self.assertEqual(res["opened"], {"a/one": 2, "b/two": 1, "projects/p/p-plan": 1})
        self.assertNotIn("early-note", res["opened"])

    def test_read_path_mapping(self):
        f = usage_metrics.note_id_from_path
        self.assertEqual(f(str(self.vault / "a" / "b.md"), self.vault), "a/b")
        self.assertIsNone(f(str(self.vault / "a" / "b.txt"), self.vault))
        self.assertIsNone(f(str(self.vault / ".graph" / "x.md"), self.vault))
        self.assertIsNone(f(str(self.tmp / "elsewhere.md"), self.vault))

    def test_sessions_with_context_and_min_turn_filter(self):
        ctx = bash("c", "python graph.py context q")
        self.write("proj", "a", [turn("1", ctx), turn("2"), turn("3")])
        self.write("proj", "b", [turn("1"), turn("2"), turn("3")])
        self.write("proj", "trivial", [turn("1", ctx), turn("2")])  # 2 turns: skipped
        self.write("x-claude-lab-runs-1", "l", [turn("1"), turn("2"), turn("3")])
        res = usage_metrics.collect(self.projects, self.vault)
        rows = {r["slug"]: r for r in res["slugs"]}
        self.assertEqual((rows["proj"]["sessions"], rows["proj"]["with_context"]), (2, 1))
        self.assertTrue(rows["x-claude-lab-runs-1"]["lab"])
        self.assertEqual(res["skipped_trivial"], 1)
        self.assertEqual(res["total"], {"sessions": 2, "with_context": 1, "ratio": 0.5})
        text = usage_metrics.format_sessions(res)
        self.assertIn("1 trivial skipped", text)
        self.assertIn("(lab)", text)


if __name__ == "__main__":
    unittest.main()
