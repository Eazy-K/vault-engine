"""Tests for VAULT_SKIP_DIRS support in tools/graph.py.

Uses only tempfile-based directories; never touches the real engine's
defaults folder or any user's notes. Run with:
    python -m unittest tests.test_skip_dirs -v
"""
from __future__ import annotations

import os
import importlib.util
import sys
import types
import tempfile
import unittest
from pathlib import Path

for _var in ("VAULT_DATA", "VAULT_HOME"):
    os.environ.pop(_var, None)


def _no_registry(*_args):
    raise OSError("tests never read the real registry")


sys.modules["winreg"] = types.SimpleNamespace(HKEY_CURRENT_USER=None, OpenKey=_no_registry)
from unittest import mock

GRAPH_PATH = Path(__file__).resolve().parent.parent / "tools" / "graph.py"

_spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestSkipDirs(unittest.TestCase):
    def test_unset_var_gives_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VAULT_SKIP_DIRS", None)
            self.assertEqual(graph.skip_dirs(), graph.SKIP_DIRS)

    def test_empty_var_gives_defaults(self):
        with mock.patch.dict(os.environ, {"VAULT_SKIP_DIRS": ""}, clear=False):
            self.assertEqual(graph.skip_dirs(), graph.SKIP_DIRS)

    def test_extra_names_added_with_whitespace_and_empty_entries(self):
        with mock.patch.dict(
            os.environ, {"VAULT_SKIP_DIRS": " archive , , drafts ,archive"}, clear=False
        ):
            result = graph.skip_dirs()
            self.assertEqual(result, graph.SKIP_DIRS | {"archive", "drafts"})

    def test_load_notes_from_skips_extra_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "archive" / "old.md", "---\n---\n# Old\n")
            write(root / "kept.md", "---\n---\n# Kept\n")
            with mock.patch.dict(os.environ, {"VAULT_SKIP_DIRS": "archive"}, clear=False):
                notes = graph._load_notes_from(root, "test")
            titles = {n.title for n in notes.values()}
            self.assertIn("Kept", titles)
            self.assertNotIn("Old", titles)

    def test_claude_worktree_copy_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = "---\n---\n# Foo\n"
            write(root / "profile" / "foo.md", note)
            write(root / ".claude" / "worktrees" / "x" / "profile" / "foo.md", note)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("VAULT_SKIP_DIRS", None)
                notes = graph._load_notes_from(root, "test")
            self.assertEqual(len(notes), 1)
            self.assertFalse(any(".claude" in n.path.parts for n in notes.values()))


if __name__ == "__main__":
    unittest.main()
