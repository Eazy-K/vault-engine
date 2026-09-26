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
import types
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


# Nor through the Windows registry, where graph finds the VAULT_DATA that setup
# saved for the user: every test sees an empty HKCU\Environment instead.
def _no_registry(*_args):
    raise OSError("tests never read the real registry")


sys.modules["winreg"] = types.SimpleNamespace(HKEY_CURRENT_USER=None, OpenKey=_no_registry)
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

    def _saved(self, values: dict):
        return mock.patch.object(graph, "user_env_var", side_effect=lambda name: values.get(name))

    def test_falls_back_to_value_saved_for_the_user_with_a_note(self):
        # Windows: setx saved VAULT_DATA, but the agent that ran setup started
        # before that and still has no VAULT_DATA in its environment.
        with mock.patch.dict("os.environ", {}, clear=True), \
             self._saved({"VAULT_DATA": "/saved/data"}), \
             mock.patch.object(graph, "_warned_saved_data", False), \
             mock.patch("sys.stderr", new_callable=StringIO) as err:
            self.assertEqual(graph.resolve_data_dir(), Path("/saved/data").expanduser().resolve())
            graph.resolve_data_dir()
        self.assertIn("Restart the terminal and the agent", err.getvalue())
        self.assertEqual(err.getvalue().count("note:"), 1)  # once per process

    def test_saved_vault_home_is_used_too(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
             self._saved({"VAULT_HOME": "/saved/home"}), \
             mock.patch("sys.stderr", new_callable=StringIO):
            self.assertEqual(graph.resolve_data_dir(), Path("/saved/home").expanduser().resolve())

    def test_environment_wins_over_saved_value(self):
        with mock.patch.dict("os.environ", {"VAULT_DATA": "/data/a"}, clear=True), \
             self._saved({"VAULT_DATA": "/saved/data"}), \
             mock.patch("sys.stderr", new_callable=StringIO) as err:
            self.assertEqual(graph.resolve_data_dir(), Path("/data/a").expanduser().resolve())
        self.assertEqual(err.getvalue(), "")

    def test_missing_on_windows_suggests_setup_not_export(self):
        with mock.patch.dict("os.environ", {}, clear=True), self._saved({}), \
             mock.patch.object(graph, "_is_windows", return_value=True):
            with self.assertRaises(SystemExit) as ctx:
                graph.resolve_data_dir()
        message = str(ctx.exception.code)
        self.assertIn("setup --data", message)
        self.assertIn("restart the terminal and the agent", message)
        self.assertNotIn("export", message)
        self.assertNotIn("VAULT_HOME", message)


class _FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_winreg(values: dict):
    """A stand-in for winreg holding `values` in HKCU\\Environment."""
    def query(_key, name):
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1

    return types.SimpleNamespace(HKEY_CURRENT_USER=object(),
                                 OpenKey=lambda *_a: _FakeKey(), QueryValueEx=query)


class TestUserEnvVar(unittest.TestCase):
    def test_reads_hkcu_environment(self):
        with mock.patch.dict(sys.modules, {"winreg": _fake_winreg({"VAULT_DATA": r"C:\notes"})}):
            self.assertEqual(graph.user_env_var("VAULT_DATA"), r"C:\notes")
            self.assertIsNone(graph.user_env_var("VAULT_ENGINE"))

    def test_empty_value_counts_as_unset(self):
        # `setx VAULT_DATA ""` leaves an empty value instead of deleting it.
        with mock.patch.dict(sys.modules, {"winreg": _fake_winreg({"VAULT_DATA": ""})}):
            self.assertIsNone(graph.user_env_var("VAULT_DATA"))

    def test_expandable_value_is_expanded(self):
        fake = _fake_winreg({})
        fake.QueryValueEx = lambda _key, _name: ("%EXAMPLE_ROOT%/notes", 2)  # REG_EXPAND_SZ
        # os.path.expandvars only knows %VAR% on Windows; fake it everywhere.
        with mock.patch.dict(sys.modules, {"winreg": fake}), \
             mock.patch("os.path.expandvars",
                        side_effect=lambda v: v.replace("%EXAMPLE_ROOT%", "/example")):
            self.assertEqual(graph.user_env_var("VAULT_DATA"), "/example/notes")

    def test_none_without_winreg(self):
        with mock.patch.dict(sys.modules, {"winreg": None}):  # import raises ImportError
            self.assertIsNone(graph.user_env_var("VAULT_DATA"))


class _Stdin:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        if isinstance(self._tty, Exception):
            raise self._tty
        return self._tty


class TestStdinIsInteractive(unittest.TestCase):
    def _check(self, tty, windows=False, console=True) -> bool:
        with mock.patch("sys.stdin", _Stdin(tty)), \
             mock.patch.object(graph, "_is_windows", return_value=windows), \
             mock.patch.object(graph, "_is_console", return_value=console):
            return graph.stdin_is_interactive()

    def test_not_a_tty(self):
        self.assertFalse(self._check(False))

    def test_tty_off_windows(self):
        self.assertTrue(self._check(True))

    def test_closed_stdin(self):
        self.assertFalse(self._check(ValueError("I/O operation on closed file")))

    def test_windows_needs_a_real_console(self):
        # NUL claims to be a tty on Windows; only a console handle counts.
        self.assertFalse(self._check(True, windows=True, console=False))
        self.assertTrue(self._check(True, windows=True, console=True))

    @unittest.skipUnless(os.name == "nt", "the NUL device quirk is Windows-only")
    def test_nul_device_is_a_tty_but_not_a_console(self):
        with open(os.devnull, encoding="utf-8") as nul:
            self.assertTrue(nul.isatty())  # the quirk this guards against
            self.assertFalse(graph._is_console(nul))


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

    def test_engine_with_notes_is_a_project(self):
        inner = self.engine / "tools"
        inner.mkdir()
        write(self.data / "projects" / "vault-engine" / "vault-engine-status.md", "# status\n")
        self.assertEqual(graph.detect_project(inner, self.paths), "vault-engine")
        self.assertEqual(graph.detect_project(self.engine, self.paths), "vault-engine")

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

    def test_status_and_overview_load_before_larger_project_notes(self):
        folder = self.data / "projects" / "example-project"
        write(folder / "example-project-aaa-research.md", note("Research", "filler " * 2000))
        write(folder / "example-project-overview.md", note("Overview", "what it is"))
        write(folder / "example-project-status.md", note("Status", "where we are"))
        output = self._run_context()
        self.assertIn("note: projects/example-project/example-project-status", output)
        self.assertIn("note: projects/example-project/example-project-overview", output)
        # The whole budget is left for it, so it is truncated rather than omitted.
        self.assertLess(output.index("example-project-status |"),
                        output.index("example-project-aaa-research |"))
        self.assertIn("<!-- truncated -->", output)

    def test_large_status_and_overview_leave_the_budget_to_task_notes(self):
        folder = self.data / "projects" / "example-project"
        write(folder / "example-project-overview.md", note("Overview", "what it is\n" * 600))
        write(folder / "example-project-status.md", note("Status", "where we are\n" * 600))
        write(folder / "example-project-design.md", note("Design", "how it works"))
        output = self._run_context()
        self.assertIn("note: projects/example-project/example-project-design", output)
        self.assertNotIn("omitted over budget", output)

    def test_large_core_notes_leave_the_budget_to_task_notes(self):
        write(self.data / "standards" / "big-core.md", note("Core", "rule\n" * 2000, core=True))
        write(self.data / "projects" / "example-project" / "example-project-design.md",
              note("Design", "how it works"))
        output = self._run_context()
        self.assertIn("note: projects/example-project/example-project-design", output)
        self.assertNotIn("omitted over budget", output)

    def test_status_and_overview_bypass_the_budget(self):
        folder = self.data / "projects" / "example-project"
        write(self.data / "standards" / "big-core.md", note("Core", "rule " * 2000, core=True))
        write(folder / "example-project-overview.md", note("Overview", "what it is"))
        write(folder / "example-project-status.md", note("Status", "where we are"))
        output = self._run_context()
        self.assertIn("note: projects/example-project/example-project-status", output)
        self.assertIn("note: projects/example-project/example-project-overview", output)

    def test_other_projects_entry_notes_stay_in_budget(self):
        self.assertFalse(graph.is_project_entry("projects/other/example-project-status"))
        self.assertFalse(graph.is_project_entry("notes/example-status"))
        self.assertTrue(graph.is_project_entry("projects/example-project/example-project-status"))

    def test_task_relevance_orders_other_project_notes(self):
        folder = self.data / "projects" / "example-project"
        write(folder / "example-project-aaa.md", note("Aaa", "unrelated"))
        write(folder / "example-project-zzz.md", note("Zzz", "release checklist"))
        output = self._run_context(text="release checklist")
        self.assertLess(output.index("example-project-zzz"), output.index("example-project-aaa"))

    def test_no_project_flag_disables_detection(self):
        output = self._run_context(no_project=True)
        self.assertNotIn("has no notes", output)

    def test_reinforce_hint_has_quoted_absolute_graph_path(self):
        write(self.data / "standards" / "a.md", note("A", "hello", core=True))
        output = self._run_context(no_project=True, no_log=False)
        expected = f'"{GRAPH_PATH}"'
        self.assertIn(expected, output)
        self.assertIn("reinforce --task", output)




class TestDetectAgent(unittest.TestCase):
    def test_codex_wins_over_inherited_claude_code(self):
        env = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "outer",
               "CODEX_THREAD_ID": "thread-1", "CODEX_SESSION_ID": "inner"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(graph.detect_agent(), ("codex", "inner"))

    def test_claude_code(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s"}, clear=True):
            self.assertEqual(graph.detect_agent(), ("claude-code", "s"))

    def test_codex_install_variables_alone_are_not_codex(self):
        # Set by the npm launcher; not proof that Codex is running this command.
        with mock.patch.dict(os.environ, {"CODEX_MANAGED_BY_NPM": "1"}, clear=True):
            self.assertEqual(graph.detect_agent(), ("unknown", None))


if __name__ == "__main__":
    unittest.main()
