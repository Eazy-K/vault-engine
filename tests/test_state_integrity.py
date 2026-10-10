"""State files under .graph/ that are corrupt (conflict markers, truncated, empty)
are skipped with a WARN, never crash, and are written atomically."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
import _isolation  # noqa: E402,F401
REPO_ROOT = TESTS_DIR.parent
_spec = importlib.util.spec_from_file_location("graph", REPO_ROOT / "tools" / "graph.py")
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)

CONFLICTED = '<<<<<<< HEAD\n{"a|b": 0.1}\n=======\n{"a|b": 0.2}\n>>>>>>> other\n'


def make_vault(root: Path) -> graph.Paths:
    (root / "proj").mkdir(parents=True)
    (root / "proj" / "a.md").write_text("---\ntype: note\nkeywords: [alpha]\n---\n# A\nalpha body\n",
                                        encoding="utf-8")
    (root / "proj" / "b.md").write_text("---\ntype: note\nkeywords: [beta]\n---\n# B\nbeta body\n",
                                        encoding="utf-8")
    return graph.Paths(REPO_ROOT, root)


class TestStateIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        graph._WARNED_STATE.clear()
        self.paths = make_vault(self.root)
        self.env = mock.patch.dict(os.environ, {"VAULT_MACHINE": "mine"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _graph(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            g = graph.Graph(self.paths)
        return g, err.getvalue()

    def test_conflicted_learned_file_is_skipped_with_warning(self):
        d = self.paths.learned_dir
        d.mkdir(parents=True)
        (d / "other.json").write_text(CONFLICTED, encoding="utf-8")
        (d / "mine.json").write_text('{"proj/a|proj/b": 0.3}\n', encoding="utf-8")
        g, err = self._graph()
        self.assertIn("WARN", err)
        self.assertIn("other.json", err)
        self.assertIn("conflict markers", err)
        self.assertAlmostEqual(g.own_learned[("proj/a", "proj/b")], 0.3)  # own file still read

    def test_truncated_and_empty_learned_files(self):
        d = self.paths.learned_dir
        d.mkdir(parents=True)
        (d / "cut.json").write_text('{"proj/a|pro', encoding="utf-8")
        (d / "empty.json").write_text("", encoding="utf-8")
        (d / "list.json").write_text("[1, 2]", encoding="utf-8")
        g, err = self._graph()
        self.assertIn("cut.json", err)
        self.assertIn("list.json", err)
        self.assertNotIn("empty.json", err)  # empty is a valid empty file
        self.assertEqual(g.learned, {})

    def test_conflicted_machine_json_warns_and_falls_back(self):
        (self.root / ".graph").mkdir()
        (self.root / ".graph" / "machine.json").write_text(CONFLICTED, encoding="utf-8")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"VAULT_MACHINE": ""}), contextlib.redirect_stderr(err):
            name = graph.machine_name(self.paths)
        self.assertTrue(name)
        self.assertIn("machine.json", err.getvalue())

    def test_truncated_embeddings_moved_aside_and_rebuilt(self):
        g, _ = self._graph()
        cache_file = self.paths.embed_cache
        cache_file.parent.mkdir(exist_ok=True)
        cache_file.write_text('{"model": "x", "notes": {"proj/a": {"ha', encoding="utf-8")
        err = io.StringIO()
        with mock.patch.object(graph, "_embed", side_effect=OSError("down")), \
                contextlib.redirect_stderr(err), self.assertRaises(OSError):
            graph.refresh_embeddings(g, "alpha")
        self.assertFalse(cache_file.exists())
        aside = list(cache_file.parent.glob("embeddings.json.corrupt-*"))
        self.assertEqual(len(aside), 1)
        self.assertIn("WARN", err.getvalue())
        # next call with Ollama reachable rebuilds the cache
        fake = lambda texts: [[1.0, 0.0] for _ in texts]  # noqa: E731
        with mock.patch.object(graph, "_embed", side_effect=fake):
            cache, qvec = graph.refresh_embeddings(g, "alpha")
        self.assertTrue(cache_file.exists())
        self.assertEqual(set(json.loads(cache_file.read_text(encoding="utf-8"))["notes"]), set(g.notes))
        self.assertEqual(qvec, [1.0, 0.0])

    def test_empty_embeddings_file_is_not_an_error(self):
        g, _ = self._graph()
        self.paths.embed_cache.parent.mkdir(exist_ok=True)
        self.paths.embed_cache.write_text("", encoding="utf-8")
        err = io.StringIO()
        with mock.patch.object(graph, "_embed", side_effect=lambda t: [[1.0] for _ in t]), \
                contextlib.redirect_stderr(err):
            graph.refresh_embeddings(g)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(list(self.paths.embed_cache.parent.glob("*.corrupt-*")), [])

    def test_conflicted_embeddings_quarantined(self):
        g, _ = self._graph()
        self.paths.embed_cache.parent.mkdir(exist_ok=True)
        self.paths.embed_cache.write_text(CONFLICTED, encoding="utf-8")
        with mock.patch.object(graph, "_embed", side_effect=lambda t: [[1.0] for _ in t]), \
                contextlib.redirect_stderr(io.StringIO()):
            graph.refresh_embeddings(g)
        self.assertEqual(len(list(self.paths.embed_cache.parent.glob("*.corrupt-*"))), 1)

    def test_atomic_write_leaves_no_partial_or_temp_file(self):
        target = self.root / "state.json"
        target.write_text("old", encoding="utf-8")
        with mock.patch.object(graph.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                graph.atomic_write_text(target, "new content")
        self.assertEqual(target.read_text(encoding="utf-8"), "old")
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith(".tmp")], [])
        graph.atomic_write_text(target, "new content")
        self.assertEqual(target.read_text(encoding="utf-8"), "new content")
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith(".tmp")], [])

    def test_save_learned_is_atomic(self):
        g, _ = self._graph()
        g.add_learned(("proj/a", "proj/b"), 0.1)
        g.save_learned()
        saved = self.paths.learned_dir / "mine.json"
        self.assertIn("proj/a|proj/b", json.loads(saved.read_text(encoding="utf-8")))
        self.assertEqual(list(self.paths.learned_dir.glob("*.tmp")), [])

    def test_doctor_helper_reports_bad_files(self):
        sys.path.insert(0, str(REPO_ROOT / "tools"))
        spec = importlib.util.spec_from_file_location("onboarding", REPO_ROOT / "tools" / "onboarding.py")
        onboarding = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(onboarding)
        d = self.paths.learned_dir
        d.mkdir(parents=True)
        (d / "other.json").write_text(CONFLICTED, encoding="utf-8")
        (d / "ok.json").write_text("{}", encoding="utf-8")
        found = onboarding.state_file_problems(self.paths)
        self.assertEqual([p.name for p, _ in found], ["other.json"])


if __name__ == "__main__":
    unittest.main()
