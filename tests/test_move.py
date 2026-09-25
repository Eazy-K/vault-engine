"""Tests for tools/move.py (`mv`: move or rename a note).

Uses only tempfile-based directories; never touches the real engine or any
user's notes. Run with:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

for _var in ("VAULT_DATA", "VAULT_HOME"):
    os.environ.pop(_var, None)

GRAPH_PATH = Path(__file__).resolve().parent.parent / "tools" / "graph.py"
TOOLS_DIR = str(Path(__file__).resolve().parent.parent / "tools")

_spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)

sys.path.insert(0, TOOLS_DIR)
import move  # noqa: E402


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def note(title: str, links: dict[str, float] | None = None, body: str = "") -> str:
    links = links or {}
    fm = "---\n"
    if links:
        fm += "links:\n" + "".join(f'  - "[[{t}]]"\n' for t in links)
        fm += "weights:\n" + "".join(f"  {t}: {w}\n" for t, w in links.items())
    fm += "---\n"
    return f"{fm}# {title}\n{body}\n"


class TestMoveNote(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engine = self.tmp / "vault-engine"
        self.data = self.tmp / "vault-data"
        self.engine.mkdir()
        self.data.mkdir()
        self.paths = graph.Paths(self.engine, self.data)
        write(self.data / "projects" / "old" / "old-status.md", note("Status"))
        write(self.data / "notes" / "by-name.md",
              note("By name", {"old-status": 0.8}, "see [[old-status|the status]]"))
        write(self.data / "notes" / "by-path.md",
              note("By path", {"projects/old/old-status": 0.6}))
        write(self.data / "notes" / "similar.md", note("Similar", body="[[old-status-archive]]"))
        write(self.data / "notes" / "old-status-archive.md", note("Archive"))
        learned = self.paths.learned_dir
        write(learned / "a.json", json.dumps({
            "notes/by-name|projects/old/old-status": 0.1,
            "notes/by-path|notes/similar": 0.2}))
        write(learned / "b.json", json.dumps({"notes/by-path|projects/old/old-status": 0.3}))

    def read(self, rel: str) -> str:
        return (self.data / rel).read_text(encoding="utf-8")

    def test_moves_file_and_rewrites_links_weights_and_learned_edges(self):
        result = move.move_note(self.paths, "old-status", "projects/new/new-status")
        self.assertEqual(result["to"], "projects/new/new-status")
        self.assertFalse((self.data / "projects" / "old").exists())
        self.assertTrue((self.data / "projects" / "new" / "new-status.md").exists())

        by_name = self.read("notes/by-name.md")
        self.assertIn('"[[new-status]]"', by_name)
        self.assertIn("  new-status: 0.8", by_name)
        self.assertIn("[[new-status|the status]]", by_name)
        by_path = self.read("notes/by-path.md")
        self.assertIn('"[[projects/new/new-status]]"', by_path)
        self.assertIn("  projects/new/new-status: 0.6", by_path)
        self.assertIn("[[old-status-archive]]", self.read("notes/similar.md"))

        a = json.loads((self.paths.learned_dir / "a.json").read_text(encoding="utf-8"))
        self.assertEqual(a, {"notes/by-name|projects/new/new-status": 0.1,
                             "notes/by-path|notes/similar": 0.2})
        b = json.loads((self.paths.learned_dir / "b.json").read_text(encoding="utf-8"))
        self.assertEqual(b, {"notes/by-path|projects/new/new-status": 0.3})

        g = graph.Graph(self.paths)
        self.assertEqual(g.problems, [])
        self.assertGreater(g.weight("notes/by-name", "projects/new/new-status"), 0.8)

    def test_bare_name_renames_in_place(self):
        result = move.move_note(self.paths, "old-status", "old-state")
        self.assertEqual(result["to"], "projects/old/old-state")

    def test_refuses_existing_destination(self):
        with self.assertRaises(ValueError):
            move.move_note(self.paths, "old-status", "notes/old-status-archive")

    def test_refuses_missing_source(self):
        with self.assertRaises(ValueError):
            move.move_note(self.paths, "nope", "notes/nope2")

    def test_uses_git_mv_in_a_repo(self):
        subprocess.run(["git", "init", "-q"], cwd=self.data, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.data, check=True)
        move.move_note(self.paths, "old-status", "projects/new/new-status")
        staged = subprocess.run(["git", "diff", "--cached", "--name-status"], cwd=self.data,
                                capture_output=True, text=True, check=True).stdout
        self.assertIn("projects/new/new-status.md", staged)
        self.assertNotIn("??", subprocess.run(["git", "status", "--short"], cwd=self.data,
                                              capture_output=True, text=True).stdout)


if __name__ == "__main__":
    unittest.main()
