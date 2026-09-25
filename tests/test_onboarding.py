"""Tests for tools/onboarding.py (init/setup/doctor).

Uses only tempfile-based directories for anything written to; Path.home(),
env vars, the setx call and the Ollama reachability check are all patched so
no test ever touches the real user environment. Run with:
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
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

# Never let a test reach the real user's data repo through the environment
# (a missing patch then fails loudly instead of writing into it).
for _var in ("VAULT_DATA", "VAULT_HOME"):
    os.environ.pop(_var, None)
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAPH_PATH = REPO_ROOT / "tools" / "graph.py"
TOOLS_DIR = REPO_ROOT / "tools"

# Load graph.py the same way graph.py's own tests and the CLI do.
_spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)

sys.path.insert(0, str(TOOLS_DIR))
import onboarding  # noqa: E402  (path must be set up first)


def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                           text=True, encoding="utf-8")


class TestInit(unittest.TestCase):
    def setUp(self):
        # Resolved so short (8.3) Windows path forms never mismatch a later .resolve().
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _args(self, target: Path, **kw):
        base = dict(dir=str(target), feedback=None, feedback_mode=None,
                    project_root=None, yes=True)
        base.update(kw)
        return Namespace(**base)

    def test_creates_files_config_and_hookspath(self):
        target = self.tmp / "example-data"
        with mock.patch("sys.stdin.isatty", return_value=False), redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))

        self.assertTrue((target / "AGENTS.md").exists())
        self.assertTrue((target / "profile" / "language.md").exists())

        config = json.loads((target / "vault.config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["project_roots"], [str(graph.ENGINE.parent)])
        self.assertEqual(config["feedback"], {"level": "off", "mode": "ask"})

        hooks_path = git(["config", "core.hooksPath"], target).stdout.strip()
        self.assertEqual(hooks_path, (graph.ENGINE / "tools" / "hooks").as_posix())

    def test_default_feedback_is_off(self):
        target = self.tmp / "example-data2"
        with mock.patch("sys.stdin.isatty", return_value=False), redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))
        config = json.loads((target / "vault.config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["feedback"]["level"], "off")

    def test_ci_workflow_names_the_engine_repo(self):
        import feedback
        target = self.tmp / "example-data6"
        workflow = target / ".github" / "workflows" / "vault.yml"
        with mock.patch.object(feedback, "_derive_repo", return_value="example/vault-engine"), \
                redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("uses: example/vault-engine@main", text)
        self.assertNotIn(onboarding.ENGINE_REPO_PLACEHOLDER, text)

    def test_ci_workflow_placeholder_kept_without_remote(self):
        import feedback
        target = self.tmp / "example-data7"
        with mock.patch.object(feedback, "_derive_repo", return_value=None), \
                redirect_stdout(StringIO()) as buf:
            onboarding.cmd_init(self._args(target))
        text = (target / ".github" / "workflows" / "vault.yml").read_text(encoding="utf-8")
        self.assertIn(onboarding.ENGINE_REPO_PLACEHOLDER, text)
        self.assertIn("vault.yml", buf.getvalue())

    def test_explicit_feedback_level_honoured(self):
        target = self.tmp / "example-data3"
        with redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target, feedback="metrics", feedback_mode="auto"))
        config = json.loads((target / "vault.config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["feedback"], {"level": "metrics", "mode": "auto"})

    def test_does_not_overwrite_existing_files(self):
        target = self.tmp / "example-data4"
        target.mkdir(parents=True)
        (target / "AGENTS.md").write_text("custom content\n", encoding="utf-8")
        with redirect_stdout(StringIO()) as buf:
            onboarding.cmd_init(self._args(target))
        self.assertEqual((target / "AGENTS.md").read_text(encoding="utf-8"), "custom content\n")
        self.assertIn("skipped", buf.getvalue())

    def test_refuses_inside_engine(self):
        target = graph.ENGINE / "example-data-inside-engine"
        with self.assertRaises(SystemExit), redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))
        self.assertFalse(target.exists())

    def test_second_run_is_a_noop_on_git_init(self):
        target = self.tmp / "example-data5"
        with redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))
            onboarding.cmd_init(self._args(target))  # must not fail on an existing repo
        self.assertTrue((target / ".git").is_dir())


class TestSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "example-data"
        self.data.mkdir()
        (self.data / "AGENTS.md").write_text("root\n", encoding="utf-8")

    def _args(self, **kw):
        base = dict(data=str(self.data), user_level=False, no_env=True,
                    no_agents=True, no_routing=True, yes=True)
        base.update(kw)
        return Namespace(**base)

    def test_agents_copied_and_idempotent(self):
        with mock.patch("pathlib.Path.home", return_value=self.home), redirect_stdout(StringIO()):
            onboarding.cmd_setup(self._args(no_agents=False))
        dest_dir = self.home / ".claude" / "agents"
        src_names = sorted(p.name for p in (graph.ENGINE / "tools" / "claude-agents").glob("*.md"))
        self.assertEqual(sorted(p.name for p in dest_dir.glob("*.md")), src_names)

        with mock.patch("pathlib.Path.home", return_value=self.home), redirect_stdout(StringIO()) as buf:
            onboarding.cmd_setup(self._args(no_agents=False))
        self.assertIn("unchanged", buf.getvalue())
        self.assertNotIn("copied", buf.getvalue())

    def test_routing_written_outside_git_repo(self):
        root = self.tmp / "projects"
        root.mkdir()
        (self.data / "vault.config.json").write_text(
            json.dumps({"project_roots": [str(root)]}), encoding="utf-8")
        with mock.patch("pathlib.Path.home", return_value=self.home), redirect_stdout(StringIO()):
            onboarding.cmd_setup(self._args(no_routing=False))
        claude_md = root / "CLAUDE.md"
        self.assertTrue(claude_md.exists())
        self.assertIn(f"@{self.data.as_posix()}/AGENTS.md", claude_md.read_text(encoding="utf-8"))

    def test_routing_skips_git_repo(self):
        root = self.tmp / "projects-git"
        root.mkdir()
        git(["init"], root)
        (self.data / "vault.config.json").write_text(
            json.dumps({"project_roots": [str(root)]}), encoding="utf-8")
        with mock.patch("pathlib.Path.home", return_value=self.home), redirect_stdout(StringIO()) as buf:
            onboarding.cmd_setup(self._args(no_routing=False))
        self.assertFalse((root / "CLAUDE.md").exists())
        self.assertIn("skipped", buf.getvalue())

    def test_routing_respects_existing_file_without_line(self):
        root = self.tmp / "projects-existing"
        root.mkdir()
        (root / "CLAUDE.md").write_text("something else\n", encoding="utf-8")
        (self.data / "vault.config.json").write_text(
            json.dumps({"project_roots": [str(root)]}), encoding="utf-8")
        with mock.patch("pathlib.Path.home", return_value=self.home), redirect_stdout(StringIO()):
            onboarding.cmd_setup(self._args(no_routing=False))
        self.assertEqual((root / "CLAUDE.md").read_text(encoding="utf-8"), "something else\n")

    def test_relative_routing_import_counts(self):
        # A hand-written relative import (e.g. `@<data folder name>/AGENTS.md`) routes too.
        root = self.tmp
        (root / "CLAUDE.md").write_text(f"@{self.data.name}/AGENTS.md\n", encoding="utf-8")
        self.assertTrue(onboarding._routes_to(root / "CLAUDE.md", self.data))
        (root / "CLAUDE.md").write_text("@elsewhere/AGENTS.md\n", encoding="utf-8")
        self.assertFalse(onboarding._routes_to(root / "CLAUDE.md", self.data))

    def test_user_level_append_once(self):
        args = self._args(user_level=True)
        with mock.patch("pathlib.Path.home", return_value=self.home), redirect_stdout(StringIO()):
            onboarding.cmd_setup(args)
            onboarding.cmd_setup(args)  # second run must not duplicate the line
        line = f"@{self.data.as_posix()}/AGENTS.md"
        claude_md_text = (self.home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(claude_md_text.count(line), 1)
        codex_text = (self.home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(codex_text.count(self.data.as_posix()), 1)

    def test_env_uses_setx_when_unset(self):
        calls = []
        with mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding._set_user_env_var", side_effect=lambda n, v: calls.append((n, v))), \
             mock.patch("onboarding._is_windows", return_value=True), \
             mock.patch.dict(os.environ, {}, clear=True), \
             redirect_stdout(StringIO()):
            onboarding.cmd_setup(self._args(no_env=False))
        self.assertEqual({n for n, _ in calls}, {"VAULT_ENGINE", "VAULT_DATA"})

    def test_env_skipped_when_already_correct(self):
        calls = []
        env = {"VAULT_ENGINE": str(graph.ENGINE), "VAULT_DATA": str(self.data)}
        with mock.patch("onboarding._set_user_env_var", side_effect=lambda n, v: calls.append(n)), \
             mock.patch.dict(os.environ, env, clear=True), \
             redirect_stdout(StringIO()) as buf:
            onboarding.cmd_setup(self._args(no_env=False))
        self.assertEqual(calls, [])
        self.assertIn("already set", buf.getvalue())


class TestDoctor(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "example-data"
        self.data.mkdir()
        (self.data / "AGENTS.md").write_text("root\n", encoding="utf-8")
        git(["init"], self.data)
        git(["config", "core.hooksPath", (graph.ENGINE / "tools" / "hooks").as_posix()], self.data)

    def _env(self):
        # PATH must survive the clear=True patches below, or the `git` subprocess
        # calls inside cmd_doctor can't find the git executable at all.
        return {"VAULT_ENGINE": str(graph.ENGINE), "VAULT_DATA": str(self.data),
                "PATH": os.environ.get("PATH", "")}

    def test_ok(self):
        with mock.patch("pathlib.Path.home", return_value=self.home), redirect_stdout(StringIO()):
            onboarding.cmd_setup(Namespace(data=str(self.data), user_level=False, no_env=True,
                                           no_agents=False, no_routing=True, yes=True))
        with mock.patch.dict(os.environ, self._env(), clear=True), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding.urllib.request.urlopen"), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit) as ctx:
                onboarding.cmd_doctor(Namespace())
        self.assertEqual(ctx.exception.code, 0)
        self.assertNotIn("FAIL", buf.getvalue())

    def test_fail_missing_agents_md(self):
        (self.data / "AGENTS.md").unlink()
        with mock.patch.dict(os.environ, self._env(), clear=True), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding.urllib.request.urlopen", side_effect=OSError("no ollama")), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit) as ctx:
                onboarding.cmd_doctor(Namespace())
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("FAIL", buf.getvalue())

    def test_fail_on_lint_errors(self):
        (self.data / "bad.md").write_text(
            '---\nlinks:\n  - "[[missing-note]]"\n---\n# Bad\n', encoding="utf-8")
        with mock.patch.dict(os.environ, self._env(), clear=True), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding.urllib.request.urlopen", side_effect=OSError("no ollama")), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit) as ctx:
                onboarding.cmd_doctor(Namespace())
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("note lint", buf.getvalue())

    def test_warn_when_vault_engine_unset(self):
        env = self._env()
        del env["VAULT_ENGINE"]
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding.urllib.request.urlopen", side_effect=OSError("no ollama")), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit):
                onboarding.cmd_doctor(Namespace())
        self.assertIn("WARN", buf.getvalue())
        self.assertIn("VAULT_ENGINE", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
