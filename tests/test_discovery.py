"""Tests for tools/discovery.py (project discovery / note coverage).

Uses only tempfile-based directories with throwaway git repos; never touches
the real engine or any user's notes. Run with:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

# Never let a test reach the real user's data repo through the environment
# (a missing patch then fails loudly instead of writing into it).
for _var in ("VAULT_DATA", "VAULT_HOME"):
    os.environ.pop(_var, None)

GRAPH_PATH = Path(__file__).resolve().parent.parent / "tools" / "graph.py"
TOOLS_DIR = str(Path(__file__).resolve().parent.parent / "tools")

_spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)

sys.path.insert(0, TOOLS_DIR)
import discovery  # noqa: E402


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def init_repo(path: Path, commit: bool = True) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", cwd=path)
    if commit:
        write(path / "README.md", "# Title\n\nFirst real line of the readme.\n")
        git("add", "-A", cwd=path)
        git("-c", "user.name=Example", "-c", "user.email=example@example.com",
            "commit", "-q", "-m", "init", cwd=path)


class TestDiscover(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "dev"
        self.root.mkdir(parents=True)
        self.engine = self.root / "vault-engine"
        self.data = self.tmp / "data"
        self.engine.mkdir()
        self.data.mkdir()
        self.paths = graph.Paths(self.engine, self.data)

    def test_engine_and_data_are_skipped_even_if_git_repos(self):
        init_repo(self.engine)
        # Data folder lives outside root in this setup, so also test a case
        # where it is a sibling under the same root.
        data_sibling = self.root / "data"
        init_repo(data_sibling)
        paths = graph.Paths(self.engine, data_sibling)
        init_repo(self.root / "alpha")
        results = discovery.discover(paths)
        names = {p["name"] for p in results}
        self.assertNotIn("vault-engine", names)
        self.assertNotIn("data", names)
        self.assertIn("alpha", names)

    def test_non_repo_folder_is_skipped(self):
        (self.root / "not-a-repo").mkdir()
        init_repo(self.root / "alpha")
        results = discovery.discover(self.paths)
        names = {p["name"] for p in results}
        self.assertNotIn("not-a-repo", names)
        self.assertIn("alpha", names)

    def test_excluded_by_config(self):
        init_repo(self.root / "alpha")
        init_repo(self.root / "secret-proj")
        write(self.data / "vault.config.json",
              json.dumps({"exclude": ["secret-*"]}))
        results = discovery.discover(self.paths)
        names = {p["name"] for p in results}
        self.assertIn("alpha", names)
        self.assertNotIn("secret-proj", names)

    def test_excluded_by_vaultignore_file(self):
        init_repo(self.root / "alpha")
        ignored = self.root / "ignored-proj"
        init_repo(ignored)
        write(ignored / ".vaultignore", "")
        results = discovery.discover(self.paths)
        names = {p["name"] for p in results}
        self.assertIn("alpha", names)
        self.assertNotIn("ignored-proj", names)

    def test_metadata_fields(self):
        proj = self.root / "alpha"
        init_repo(proj)
        write(proj / "main.py", "print(1)\n")
        write(proj / "helper.py", "print(2)\n")
        write(proj / "index.js", "console.log(1);\n")
        git("add", "-A", cwd=proj)
        git("-c", "user.name=Example", "-c", "user.email=example@example.com",
            "commit", "-q", "-m", "add files", cwd=proj)
        results = discovery.discover(self.paths)
        alpha = next(p for p in results if p["name"] == "alpha")
        self.assertEqual(alpha["readme"], "First real line of the readme.")
        self.assertIn("py", alpha["languages"])
        self.assertIsNotNone(alpha["last_commit"])
        self.assertFalse(alpha["has_notes"])

    def test_has_notes_true_when_project_notes_exist(self):
        init_repo(self.root / "alpha")
        write(self.data / "projects" / "alpha" / "alpha-overview.md", "# Alpha\n")
        results = discovery.discover(self.paths)
        alpha = next(p for p in results if p["name"] == "alpha")
        self.assertTrue(alpha["has_notes"])

    def test_readme_skips_headings_and_blank_lines(self):
        proj = self.root / "alpha"
        proj.mkdir()
        git("init", "-q", cwd=proj)
        write(proj / "README.md", "# Heading\n\n\nActual first line.\nSecond line.\n")
        git("add", "-A", cwd=proj)
        git("-c", "user.name=Example", "-c", "user.email=example@example.com",
            "commit", "-q", "-m", "init", cwd=proj)
        results = discovery.discover(self.paths)
        alpha = next(p for p in results if p["name"] == "alpha")
        self.assertEqual(alpha["readme"], "Actual first line.")

    def test_remote_host_https_with_user_strips_to_host(self):
        proj = self.root / "alpha"
        init_repo(proj)
        git("remote", "add", "origin", "https://user123@github.com/user123/alpha.git", cwd=proj)  # guard:ignore
        results = discovery.discover(self.paths)
        alpha = next(p for p in results if p["name"] == "alpha")
        self.assertEqual(alpha["remote_host"], "github.com")

    def test_remote_host_ssh_form_strips_to_host(self):
        proj = self.root / "alpha"
        init_repo(proj)
        git("remote", "add", "origin", "git@github.com:user123/alpha.git", cwd=proj)  # guard:ignore
        results = discovery.discover(self.paths)
        alpha = next(p for p in results if p["name"] == "alpha")
        self.assertEqual(alpha["remote_host"], "github.com")

    def test_no_remote_gives_none(self):
        init_repo(self.root / "alpha")
        results = discovery.discover(self.paths)
        alpha = next(p for p in results if p["name"] == "alpha")
        self.assertIsNone(alpha["remote_host"])


class TestCache(unittest.TestCase):
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

    def test_cache_file_written(self):
        discovery.load_cached(self.paths)
        cache_file = self.data / ".graph" / "projects.json"
        self.assertTrue(cache_file.exists())
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertEqual(data["projects"][0]["name"], "alpha")

    def _poisoned_cache(self, roots: list[str] | None = None) -> str:
        if roots is None:
            roots = [str(r) for r in graph.project_roots(self.paths)]
        return json.dumps({"roots": roots, "projects": [{"name": "stale", "has_notes": False}]})

    def test_cache_for_other_roots_is_ignored(self):
        discovery.load_cached(self.paths)
        cache_file = self.data / ".graph" / "projects.json"
        cache_file.write_text(self._poisoned_cache(["/elsewhere"]), encoding="utf-8")
        names = {p["name"] for p in discovery.load_cached(self.paths)}
        self.assertIn("alpha", names)
        self.assertNotIn("stale", names)

    def test_cache_reused_when_fresh(self):
        discovery.load_cached(self.paths)
        cache_file = self.data / ".graph" / "projects.json"
        # Poison the cache directly; a fresh reuse should return this, not rescan.
        cache_file.write_text(self._poisoned_cache(),
                              encoding="utf-8")
        result = discovery.load_cached(self.paths)
        self.assertEqual(result[0]["name"], "stale")

    def test_refresh_ignores_cache(self):
        discovery.load_cached(self.paths)
        cache_file = self.data / ".graph" / "projects.json"
        cache_file.write_text(self._poisoned_cache(),
                              encoding="utf-8")
        result = discovery.load_cached(self.paths, refresh=True)
        names = {p["name"] for p in result}
        self.assertIn("alpha", names)
        self.assertNotIn("stale", names)

    def test_cache_stale_after_ttl(self):
        discovery.load_cached(self.paths)
        cache_file = self.data / ".graph" / "projects.json"
        cache_file.write_text(self._poisoned_cache(),
                              encoding="utf-8")
        old = time.time() - discovery.CACHE_TTL - 10
        import os
        os.utime(cache_file, (old, old))
        result = discovery.load_cached(self.paths)
        names = {p["name"] for p in result}
        self.assertIn("alpha", names)
        self.assertNotIn("stale", names)

    def test_missing_projects_uses_cache(self):
        write(self.data / "projects" / "alpha" / "alpha-overview.md", "# Alpha\n")
        init_repo(self.root / "beta")
        discovery.load_cached(self.paths, refresh=True)
        missing = discovery.missing_projects(self.paths)
        self.assertIn("beta", missing)
        self.assertNotIn("alpha", missing)


class TestProjectsCommand(unittest.TestCase):
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
        write(self.data / "projects" / "alpha" / "alpha-overview.md", "# Alpha\n")

    def _run(self, **kwargs):
        args = Namespace(refresh=True, json=False, missing=False)
        for k, v in kwargs.items():
            setattr(args, k, v)
        with mock_default_paths(self.paths):
            buf = StringIO()
            with redirect_stdout(buf):
                discovery.cmd_projects(args)
            return buf.getvalue()

    def test_table_lists_all_projects(self):
        out = self._run()
        self.assertIn("alpha", out)
        self.assertIn("beta", out)

    def test_missing_shows_only_projects_without_notes_and_suggests(self):
        out = self._run(missing=True)
        self.assertNotIn("alpha", out)
        self.assertIn("beta", out)
        self.assertIn("<name>-overview.md", out)
        self.assertIn("-status.md", out)
        self.assertIn("beta", out.splitlines()[-1])

    def test_json_output_is_valid(self):
        out = self._run(json=True)
        data = json.loads(out)
        names = {p["name"] for p in data}
        self.assertEqual(names, {"alpha", "beta"})


class _PatchedDefaultPaths:
    def __init__(self, paths):
        self.paths = paths
        self._orig = graph.default_paths

    def __enter__(self):
        graph.default_paths = lambda: self.paths
        return self

    def __exit__(self, *exc):
        graph.default_paths = self._orig


def mock_default_paths(paths):
    return _PatchedDefaultPaths(paths)


if __name__ == "__main__":
    unittest.main()
