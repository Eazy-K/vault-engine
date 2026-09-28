"""Tests for project selection: the per-computer skip list, `projects --ask`,
the `context` hints that point at it, and that setup/doctor use it too.

Uses only tempfile-based directories with throwaway git repos; never touches
the real engine or any user's notes. Run with:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

for _var in ("VAULT_DATA", "VAULT_HOME"):
    os.environ.pop(_var, None)


def _no_registry(*_args):
    raise OSError("tests never read the real registry")


sys.modules["winreg"] = types.SimpleNamespace(HKEY_CURRENT_USER=None, OpenKey=_no_registry)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
GRAPH_PATH = TOOLS_DIR / "graph.py"

if "graph" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
    _graph = importlib.util.module_from_spec(_spec)
    sys.modules["graph"] = _graph
    _spec.loader.exec_module(_graph)

sys.path.insert(0, str(TOOLS_DIR))
import discovery  # noqa: E402
import onboarding  # noqa: E402

# discovery.py/onboarding.py may already be cached (imported by another test
# module) and bound to a `graph` module instance other than sys.modules
# ["graph"] at this point (each test file that (re)creates the "graph" module
# leaves its own instance behind). Use discovery's own binding everywhere
# here, so patches on it (e.g. `default_paths`) reach the calls it makes.
graph = discovery.g


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", cwd=path)
    write(path / "README.md", "# Title\n\nFirst real line.\n")
    git("add", "-A", cwd=path)
    git("-c", "user.name=Example", "-c", "user.email=example@example.com",
        "commit", "-q", "-m", "init", cwd=path)


class TestSkipList(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engine = self.tmp / "dev" / "vault-engine"
        self.data = self.tmp / "data"
        self.engine.mkdir(parents=True)
        self.data.mkdir()
        self.paths = graph.Paths(self.engine, self.data)

    def test_empty_when_no_machine_json(self):
        self.assertEqual(discovery.skip_list(self.paths), [])

    def test_empty_when_machine_json_is_bad_json(self):
        write(self.data / ".graph" / "machine.json", "{not json")
        self.assertEqual(discovery.skip_list(self.paths), [])

    def test_set_and_read_back(self):
        discovery.set_skip_list(self.paths, ["alpha", "beta"])
        self.assertEqual(discovery.skip_list(self.paths), ["alpha", "beta"])

    def test_other_machine_json_keys_are_preserved(self):
        discovery._write_machine_json(self.paths, {"machine": "pc-1", "project_roots": ["/x"]})
        discovery.set_skip_list(self.paths, ["alpha"])
        data = json.loads((self.data / ".graph" / "machine.json").read_text(encoding="utf-8"))
        self.assertEqual(data["machine"], "pc-1")
        self.assertEqual(data["project_roots"], ["/x"])
        self.assertEqual(data["skip_projects"], ["alpha"])


class TestCachedOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "dev"
        self.root.mkdir(parents=True)
        self.engine = self.root / "vault-engine"
        self.engine.mkdir()
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.paths = graph.Paths(self.engine, self.data)

    def test_empty_without_a_cache_file(self):
        self.assertEqual(discovery.cached_only(self.paths), [])

    def test_empty_with_a_corrupt_cache_file(self):
        write(self.data / ".graph" / "projects.json", "{not json")
        self.assertEqual(discovery.cached_only(self.paths), [])

    def test_reads_the_cache_without_scanning(self):
        init_repo(self.root / "alpha")
        discovery.load_cached(self.paths)
        with mock.patch.object(discovery, "discover") as scan:
            result = discovery.cached_only(self.paths)
        scan.assert_not_called()
        self.assertEqual([p["name"] for p in result], ["alpha"])

    def test_ignores_ttl_unlike_load_cached(self):
        init_repo(self.root / "alpha")
        discovery.load_cached(self.paths)
        cache_file = self.data / ".graph" / "projects.json"
        old = 0  # far in the past, well past CACHE_TTL
        os.utime(cache_file, (old, old))
        with mock.patch.object(discovery, "discover") as scan:
            result = discovery.cached_only(self.paths)
        scan.assert_not_called()
        self.assertEqual([p["name"] for p in result], ["alpha"])


class TestProjectsSkipCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "dev"
        self.root.mkdir(parents=True)
        self.engine = self.root / "vault-engine"
        self.engine.mkdir()
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.paths = graph.Paths(self.engine, self.data)
        init_repo(self.root / "alpha")
        init_repo(self.root / "beta")

    def _run(self, **kwargs):
        args = Namespace(refresh=True, json=False, missing=False, ask=False, skip=None,
                         unskip=None)
        for k, v in kwargs.items():
            setattr(args, k, v)
        with mock.patch.object(graph, "default_paths", return_value=self.paths):
            buf = StringIO()
            with redirect_stdout(buf):
                discovery.cmd_projects(args)
            return buf.getvalue()

    def test_skip_persists_and_is_reported(self):
        out = self._run(skip=["alpha"])
        self.assertIn("skipped: alpha", out)
        self.assertEqual(discovery.skip_list(self.paths), ["alpha"])

    def test_unskip_removes_from_list(self):
        self._run(skip=["alpha", "beta"])
        out = self._run(unskip=["alpha"])
        self.assertIn("unskipped: alpha", out)
        self.assertEqual(discovery.skip_list(self.paths), ["beta"])

    def test_skip_of_already_skipped_reports_no_change(self):
        self._run(skip=["alpha"])
        out = self._run(skip=["alpha"])
        self.assertIn("no change", out)

    def test_missing_excludes_skipped_projects(self):
        self._run(skip=["beta"])
        out = self._run(missing=True)
        self.assertNotIn("beta", out)

    def test_json_includes_skipped_flag(self):
        self._run(skip=["alpha"])
        out = self._run(json=True)
        data = {p["name"]: p for p in json.loads(out)}
        self.assertTrue(data["alpha"]["skipped"])
        self.assertFalse(data["beta"]["skipped"])

    def test_table_shows_skipped_status(self):
        self._run(skip=["alpha"])
        out = self._run()
        alpha_line = next(l for l in out.splitlines() if l.startswith("alpha"))
        self.assertIn("skipped", alpha_line)

    def test_skip_and_missing_never_write_project_notes(self):
        self._run(skip=["alpha"])
        self._run(missing=True)
        self._run(ask=True)
        self.assertFalse((self.data / "projects").exists())


class TestProjectsAsk(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "dev"
        self.root.mkdir(parents=True)
        self.engine = self.root / "vault-engine"
        self.engine.mkdir()
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.paths = graph.Paths(self.engine, self.data)

    def _run(self, **kwargs):
        args = Namespace(refresh=True, json=False, missing=False, ask=True, skip=None,
                         unskip=None)
        for k, v in kwargs.items():
            setattr(args, k, v)
        with mock.patch.object(graph, "default_paths", return_value=self.paths):
            buf = StringIO()
            with redirect_stdout(buf):
                discovery.cmd_projects(args)
            return buf.getvalue()

    def test_nothing_to_ask_when_no_projects(self):
        out = self._run()
        self.assertIn("Nothing to ask", out)

    def test_nothing_to_ask_when_all_have_notes_or_are_skipped(self):
        init_repo(self.root / "alpha")
        write(self.data / "projects" / "alpha" / "alpha-overview.md", "# Alpha\n")
        init_repo(self.root / "beta")
        discovery.set_skip_list(self.paths, ["beta"])
        out = self._run()
        self.assertIn("Nothing to ask", out)

    def test_lists_candidates_and_the_not_in_list_hint(self):
        init_repo(self.root / "alpha")
        out = self._run()
        self.assertIn("alpha", out)
        self.assertIn("not in this list", out)
        self.assertIn("never index code", out)
        self.assertIn("projects --skip alpha", out)

    def test_never_writes_project_notes(self):
        init_repo(self.root / "alpha")
        self._run()
        self.assertFalse((self.data / "projects").exists())


class TestContextProjectHints(unittest.TestCase):
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

    def _context(self, cwd: Path):
        args = Namespace(text="", seed=[], threshold=graph.DEFAULT_THRESHOLD,
                         depth=graph.DEFAULT_DEPTH, json=False, no_semantic=True,
                         project=None, no_project=False, core=False,
                         budget=graph.DEFAULT_BUDGET, no_log=True)
        out = StringIO()
        with mock.patch.object(graph, "default_paths", return_value=self.paths), \
             mock.patch("pathlib.Path.cwd", return_value=cwd), \
             redirect_stdout(out):
            graph.cmd_query(args, content=True)
        return out.getvalue()

    def test_skipped_cwd_project_prints_nothing(self):
        discovery.set_skip_list(self.paths, ["example-project"])
        out = self._context(self.project_dir)
        self.assertNotIn("has no notes", out)

    def test_unskipped_cwd_project_points_at_ask(self):
        out = self._context(self.project_dir)
        self.assertIn("projects --ask", out)

    def test_outside_any_project_reports_missing_from_cache_only(self):
        other = self.dev / "other-project"
        other.mkdir()
        init_repo(other)
        discovery.load_cached(self.paths, refresh=True)
        with mock.patch.object(discovery, "discover") as scan:
            out = self._context(self.tmp)  # outside dev entirely, not a project
        scan.assert_not_called()
        self.assertIn("other-project", out)
        self.assertIn("projects --ask", out)

    def test_outside_any_project_prints_nothing_without_a_cache(self):
        out = self._context(self.tmp)
        self.assertNotIn("discovered projects", out)

    def test_outside_any_project_skips_a_skipped_one(self):
        other = self.dev / "other-project"
        other.mkdir()
        init_repo(other)
        discovery.load_cached(self.paths, refresh=True)
        discovery.set_skip_list(self.paths, ["other-project"])
        out = self._context(self.tmp)
        self.assertNotIn("discovered projects", out)


class TestSetupProjectsSection(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.dev = self.tmp / "dev"
        self.engine = self.dev / "vault-engine"
        self.data = self.dev / "vault-data"
        self.engine.mkdir(parents=True)
        self.data.mkdir()
        (self.data / "AGENTS.md").write_text("root\n", encoding="utf-8")

    def _args(self, **kw):
        base = dict(data=str(self.data), user_level=False, no_env=True, no_machine=True,
                    no_agents=True, no_routing=True, yes=True)
        base.update(kw)
        return Namespace(**base)

    def _setup(self, **kw) -> str:
        with mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch.object(onboarding.g, "ENGINE", self.engine), \
             redirect_stdout(StringIO()) as buf:
            onboarding.cmd_setup(self._args(**kw))
        return buf.getvalue()

    def test_next_step_printed_when_a_project_is_missing_notes(self):
        init_repo(self.dev / "alpha")
        out = self._setup()
        self.assertIn("next step", out)
        self.assertIn("projects --ask", out)
        self.assertIn("alpha", out)

    def test_ok_line_when_nothing_missing(self):
        out = self._setup()
        self.assertIn("every discovered project has notes", out)

    def test_no_projects_flag_skips_the_section(self):
        out = self._setup(no_projects=True)
        self.assertNotIn("Projects:", out)

    def test_setup_never_writes_project_notes(self):
        init_repo(self.dev / "alpha")
        self._setup()
        self.assertFalse((self.data / "projects").exists())


class TestDoctorProjectWarning(unittest.TestCase):
    """`missing_projects` (what doctor's WARN is built from) excludes skipped
    projects; the WARN text itself is exercised end-to-end here too."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.projects_root = self.tmp / "projects"
        self.projects_root.mkdir()
        self.data = self.tmp / "example-data"
        self.data.mkdir()
        (self.data / "AGENTS.md").write_text("root\n", encoding="utf-8")
        git("init", cwd=self.data)
        git("config", "core.hooksPath", (graph.ENGINE / "tools" / "hooks").as_posix(), cwd=self.data)
        write(self.data / ".graph" / "machine.json",
              json.dumps({"project_roots": [str(self.projects_root)]}))
        init_repo(self.projects_root / "alpha")

    def _env(self):
        return {"VAULT_ENGINE": str(graph.ENGINE), "VAULT_DATA": str(self.data),
                "PATH": os.environ.get("PATH", "")}

    def _doctor(self) -> str:
        with mock.patch.dict(os.environ, self._env(), clear=True), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding.urllib.request.urlopen", side_effect=OSError("no ollama")), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit):
                onboarding.cmd_doctor(Namespace())
        return buf.getvalue()

    def test_warn_text_points_at_ask_and_skip(self):
        out = self._doctor()
        self.assertIn("projects without notes: alpha", out)
        self.assertIn("projects --ask", out)
        self.assertIn("projects --skip <name>", out)

    def test_skipped_projects_are_not_counted(self):
        paths = graph.Paths(graph.ENGINE, self.data)
        discovery.set_skip_list(paths, ["alpha"])
        out = self._doctor()
        self.assertIn("every discovered project has notes", out)
        self.assertNotIn("projects without notes", out)


if __name__ == "__main__":
    unittest.main()
