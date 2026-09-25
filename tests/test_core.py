"""Tests for the engine/data split in tools/graph.py.

Uses only tempfile-based directories; never touches the real engine's
defaults folder or any user's notes. Run with:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

# Never let a test reach the real user's data repo through the environment
# (a missing patch then fails loudly instead of writing into it).
for _var in ("VAULT_DATA", "VAULT_HOME"):
    os.environ.pop(_var, None)
from unittest import mock

GRAPH_PATH = Path(__file__).resolve().parent.parent / "tools" / "graph.py"

# Load graph.py as a standalone module (not a package import) the same way the
# CLI runs it, and register it in sys.modules before exec so @dataclass can
# resolve its own module (needed on newer Pythons).
_spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def note(title: str, body: str = "", core: bool = False, extra: str = "") -> str:
    fm = "---\n"
    if core:
        fm += "core: true\n"
    fm += extra
    fm += "---\n"
    return f"{fm}# {title}\n{body}\n"


class TestDataDirResolution(unittest.TestCase):
    def test_vault_data_wins_over_vault_home(self):
        with mock.patch.dict("os.environ", {"VAULT_DATA": "/data/a", "VAULT_HOME": "/data/b"},
                             clear=True):
            self.assertEqual(graph.resolve_data_dir(), Path("/data/a").expanduser().resolve())

    def test_vault_home_fallback(self):
        with mock.patch.dict("os.environ", {"VAULT_HOME": "/data/b"}, clear=True):
            self.assertEqual(graph.resolve_data_dir(), Path("/data/b").expanduser().resolve())

    def test_missing_env_exits(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit):
                graph.resolve_data_dir()

    def test_import_does_not_require_env(self):
        # The module was already imported above with no env vars guaranteed set;
        # reaching this line at all proves import time did not resolve VAULT_DATA.
        self.assertTrue(hasattr(graph, "ENGINE"))


class TestDefaultsOverlay(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engine = self.tmp / "engine"
        self.data = self.tmp / "data"
        write(self.engine / "defaults" / "standards" / "shared.md",
              note("Shared Default", "default body", core=True))
        write(self.engine / "defaults" / "standards" / "override-me.md",
              note("Override Me (default)", "default version"))
        write(self.data / "standards" / "override-me.md",
              note("Override Me (data)", "data version"))
        write(self.data / "standards" / "data-only.md", note("Data Only", "only in data"))

    def test_default_only_note_is_flagged_default(self):
        notes = graph.load_notes(self.data, self.engine / "defaults")
        self.assertEqual(notes["standards/shared"].source, "default")

    def test_data_overrides_default_by_id(self):
        notes = graph.load_notes(self.data, self.engine / "defaults")
        overridden = notes["standards/override-me"]
        self.assertEqual(overridden.source, "data")
        self.assertIn("data version", overridden.body)

    def test_data_only_note_is_flagged_data(self):
        notes = graph.load_notes(self.data, self.engine / "defaults")
        self.assertEqual(notes["standards/data-only"].source, "data")

    def test_missing_defaults_folder_is_fine(self):
        notes = graph.load_notes(self.data, self.engine / "no-such-defaults")
        self.assertIn("standards/data-only", notes)

    def test_defaults_none_skips_overlay_entirely(self):
        notes = graph.load_notes(self.data)
        self.assertNotIn("standards/shared", notes)
        self.assertIn("standards/data-only", notes)


class TestPathsUnderData(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engine = self.tmp / "engine"
        self.data = self.tmp / "data"
        write(self.data / "standards" / "a.md", note("A", "hello", core=True))
        self.paths = graph.Paths(self.engine, self.data)

    def test_learned_embed_usage_under_data_not_engine(self):
        self.assertEqual(self.paths.learned_dir, self.data / ".graph" / "learned")
        self.assertEqual(self.paths.embed_cache, self.data / ".graph" / "embeddings.json")
        self.assertEqual(self.paths.usage_log, self.data / ".graph" / "usage.log")

    def test_graph_accepts_explicit_paths_and_saves_learned_under_data(self):
        g = graph.Graph(self.paths)
        g.add_learned(("x", "y"), 0.2)
        g.save_learned()
        files = list((self.data / ".graph" / "learned").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertFalse((self.engine / ".graph").exists())

    def test_log_and_read_usage_under_data(self):
        graph.log_usage(self.paths, {"event": "context", "task": "abc123"})
        events = graph.read_usage(self.paths)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["task"], "abc123")
        self.assertFalse((self.engine / ".graph").exists())


class TestDetectProject(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dev = self.tmp / "dev"  # default project_roots = [engine.parent] = dev
        self.engine = self.dev / "vault-engine"
        self.data = self.dev / "vault-data"
        for d in (self.dev, self.engine, self.data):
            d.mkdir(parents=True, exist_ok=True)
        self.paths = graph.Paths(self.engine, self.data)

    def test_inside_project_root(self):
        example = self.dev / "example-project" / "src"
        example.mkdir(parents=True)
        self.assertEqual(graph.detect_project(example, self.paths), "example-project")

    def test_outside_any_root(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        self.assertIsNone(graph.detect_project(outside, self.paths))

    def test_inside_engine_excluded(self):
        inner = self.engine / "tools"
        inner.mkdir()
        self.assertIsNone(graph.detect_project(inner, self.paths))

    def test_inside_data_excluded(self):
        inner = self.data / "standards"
        inner.mkdir()
        self.assertIsNone(graph.detect_project(inner, self.paths))

    def test_config_overrides_default_root(self):
        other_root = self.tmp / "other-root"
        (other_root / "example-project").mkdir(parents=True)
        write(self.paths.config_file,
              json.dumps({"project_roots": [str(other_root)]}))
        self.assertEqual(
            graph.detect_project(other_root / "example-project", self.paths),
            "example-project")
        # The old default root no longer counts once configured explicitly.
        default_root_child = self.dev / "example-project"
        default_root_child.mkdir(exist_ok=True)
        self.assertIsNone(graph.detect_project(default_root_child, self.paths))


class TestProjectSeedingAndHint(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dev = self.tmp / "dev"
        self.engine = self.dev / "vault-engine"
        self.data = self.dev / "vault-data"
        self.project_dir = self.dev / "example-project"
        for d in (self.engine, self.data, self.project_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.paths = graph.Paths(self.engine, self.data)

    def _run_context(self, **overrides):
        args = Namespace(text="", seed=[], threshold=graph.DEFAULT_THRESHOLD,
                         depth=graph.DEFAULT_DEPTH, json=False, no_semantic=True,
                         project=None, no_project=False, core=False,
                         budget=graph.DEFAULT_BUDGET, no_log=True)
        for k, v in overrides.items():
            setattr(args, k, v)
        out = StringIO()
        with mock.patch.object(graph, "default_paths", return_value=self.paths), \
             mock.patch("pathlib.Path.cwd", return_value=self.project_dir), \
             redirect_stdout(out):
            graph.cmd_query(args, content=True)
        return out.getvalue()

    def test_no_notes_prints_hint(self):
        output = self._run_context()
        self.assertIn("project example-project has no notes", output)
        self.assertIn("projects/example-project/example-project-overview.md", output)
        self.assertIn("projects/example-project/example-project-status.md", output)

    def test_project_note_is_seeded_and_no_hint(self):
        write(self.data / "projects" / "example-project" / "example-project-overview.md",
              note("Example Project Overview", "some project content", core=False))
        output = self._run_context()
        self.assertNotIn("has no notes", output)
        self.assertIn("projects/example-project/example-project-overview", output)

    def test_no_project_flag_disables_detection(self):
        output = self._run_context(no_project=True)
        self.assertNotIn("has no notes", output)

    def test_reinforce_hint_has_quoted_absolute_graph_path(self):
        write(self.data / "standards" / "a.md", note("A", "hello", core=True))
        output = self._run_context(no_project=True, no_log=False)
        expected = f'"{GRAPH_PATH}"'
        self.assertIn(expected, output)
        self.assertIn("reinforce --task", output)


if __name__ == "__main__":
    unittest.main()
