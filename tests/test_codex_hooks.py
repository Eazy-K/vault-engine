"""Codex adapter tests. Every installer path is under a temporary directory."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path

for _var in ("VAULT_DATA", "VAULT_HOME"):
    os.environ.pop(_var, None)


def _no_registry(*_args):
    raise OSError("tests never read the real registry")


sys.modules["winreg"] = types.SimpleNamespace(HKEY_CURRENT_USER=None, OpenKey=_no_registry)
REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
spec = importlib.util.spec_from_file_location("graph", TOOLS_DIR / "graph.py")
graph = importlib.util.module_from_spec(spec)
sys.modules["graph"] = graph
spec.loader.exec_module(graph)
sys.path.insert(0, str(TOOLS_DIR))
import codex_hooks  # noqa: E402


class TestMerge(unittest.TestCase):
    def test_merge_adds_both_hooks_and_preserves_unrelated_entries(self):
        original = {"description": "user config", "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "echo keep"}]}]}}
        merged, changed = codex_hooks.merge(original)
        self.assertTrue(changed)
        self.assertEqual(merged["description"], "user config")
        self.assertEqual(merged["hooks"]["PreToolUse"], original["hooks"]["PreToolUse"])
        self.assertIn("context-warn.py", merged["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"])
        post = merged["hooks"]["PostToolUse"][0]
        self.assertEqual(post["matcher"], "^(Bash|Read|Edit|Write|apply_patch)$")
        self.assertIn("delegation-warn.py", post["hooks"][0]["command"])

    def test_merge_is_idempotent(self):
        merged, _ = codex_hooks.merge({})
        merged_again, changed = codex_hooks.merge(merged)
        self.assertFalse(changed)
        self.assertEqual(merged_again, merged)

    def test_merge_refreshes_stale_paths_in_place(self):
        settings = {"hooks": {
            "UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": "python /claude/context-warn.py"}]}],
            "PostToolUse": [{"matcher": "Bash|Read|Edit|Write", "hooks": [
                {"type": "command", "command": "python /claude/delegation-warn.py"}]}],
        }}
        merged, changed = codex_hooks.merge(settings)
        self.assertTrue(changed)
        context_entries = merged["hooks"]["UserPromptSubmit"]
        delegation_entries = merged["hooks"]["PostToolUse"]
        self.assertEqual(len(context_entries), 1)
        self.assertEqual(context_entries[0]["hooks"][0]["command"],
                         codex_hooks._command(codex_hooks.HOOKS_DIR / "context-warn.py"))
        self.assertEqual(delegation_entries[0]["matcher"],
                         "^(Bash|Read|Edit|Write|apply_patch)$")
        self.assertEqual(delegation_entries[0]["hooks"][0]["command"],
                         codex_hooks._command(codex_hooks.HOOKS_DIR / "delegation-warn.py"))

    def test_merge_refuses_malformed_hooks_member(self):
        with self.assertRaises(ValueError):
            codex_hooks.merge({"hooks": "broken"})


class TestInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.hooks = self.tmp / ".codex" / "hooks.json"
        self.agents = self.tmp / ".codex" / "agents"

    def _run(self, install: bool):
        out = io.StringIO()
        with redirect_stdout(out):
            codex_hooks.cmd_codex_hooks(Namespace(install=install, hooks=str(self.hooks),
                                                  agents_dir=str(self.agents)))
        return out.getvalue()

    def test_install_creates_hooks_and_worker_files(self):
        output = self._run(True)
        self.assertIn("installed hooks", output)
        data = json.loads(self.hooks.read_text(encoding="utf-8"))
        self.assertIn("UserPromptSubmit", data["hooks"])
        self.assertIn("PostToolUse", data["hooks"])
        for role in ("worker-low", "worker-medium"):
            self.assertTrue((self.agents / f"{role}.toml").is_file())
        self.assertFalse(list(self.tmp.rglob("*.bak-*")))
        before = self.hooks.read_bytes()
        second = self._run(True)
        self.assertIn("hooks up to date", second)
        self.assertEqual(self.hooks.read_bytes(), before)
        self.assertFalse(list(self.tmp.rglob("*.bak-*")))

    def test_dry_run_does_not_write(self):
        output = self._run(False)
        self.assertIn("would update", output)
        self.assertFalse(self.hooks.exists())
        self.assertFalse(self.agents.exists())

    def test_install_backs_up_changed_hooks_and_keeps_custom_worker(self):
        self.hooks.parent.mkdir(parents=True)
        self.hooks.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash",
            "hooks": [{"type": "command", "command": "echo keep"}]}]}}), encoding="utf-8")
        self.agents.mkdir(parents=True)
        custom = self.agents / "worker-low.toml"
        custom.write_text("# local edit\n", encoding="utf-8")
        output = self._run(True)
        backups = list(self.hooks.parent.glob("hooks.json.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("echo keep", backups[0].read_text(encoding="utf-8"))
        self.assertIn("kept customized worker", output)
        self.assertEqual(custom.read_text(encoding="utf-8"), "# local edit\n")
        self.assertTrue((self.agents / "worker-medium.toml").exists())

    def test_invalid_json_is_not_overwritten(self):
        self.hooks.parent.mkdir(parents=True)
        self.hooks.write_text("{broken", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self._run(True)
        self.assertEqual(self.hooks.read_text(encoding="utf-8"), "{broken")


class TestStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_hooks_status_warns_then_passes_after_install(self):
        hooks = self.tmp / "hooks.json"
        self.assertEqual(codex_hooks.hooks_status(hooks)[0], "WARN")
        codex_hooks.cmd_codex_hooks(Namespace(install=True, hooks=str(hooks),
                                               agents_dir=str(self.tmp / "agents")))
        self.assertEqual(codex_hooks.hooks_status(hooks)[0], "OK")

    def test_agent_guard_registered_and_missing_guard_is_stale(self):
        merged, _ = codex_hooks.merge({})
        pre = merged["hooks"]["PreToolUse"][0]
        self.assertEqual(pre["matcher"], "^(Agent|spawn_agent)$")
        self.assertIn("agent-guard.py", pre["hooks"][0]["command"])
        hooks = self.tmp / "hooks.json"
        codex_hooks.cmd_codex_hooks(Namespace(install=True, hooks=str(hooks),
                                               agents_dir=str(self.tmp / "agents")))
        data = json.loads(hooks.read_text(encoding="utf-8"))
        del data["hooks"]["PreToolUse"]
        hooks.write_text(json.dumps(data), encoding="utf-8")
        status, message = codex_hooks.hooks_status(hooks)
        self.assertEqual(status, "WARN")
        self.assertIn("agent-guard.py", message)

    def test_hooks_status_flags_stale_matcher(self):
        hooks = self.tmp / "hooks.json"
        codex_hooks.cmd_codex_hooks(Namespace(install=True, hooks=str(hooks),
                                               agents_dir=str(self.tmp / "agents")))
        data = json.loads(hooks.read_text(encoding="utf-8"))
        data["hooks"]["PostToolUse"][0]["matcher"] = "Bash|Read"
        hooks.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(codex_hooks.hooks_status(hooks)[0], "WARN")

    def test_model_status_checks_root_model_and_effort(self):
        config = self.tmp / "config.toml"
        config.write_text('model = "gpt-6-sol"\nmodel_reasoning_effort = "high"\n', encoding="utf-8")
        self.assertEqual(codex_hooks.model_status(config)[0], "OK")
        config.write_text('model = "gpt-6-sol"\n[other]\nmodel_reasoning_effort = "high"\n',
                          encoding="utf-8")
        self.assertEqual(codex_hooks.model_status(config)[0], "WARN")

    def test_worker_status_detects_missing_and_custom_files(self):
        agents = self.tmp / "agents"
        self.assertEqual(codex_hooks.workers_status(agents)[0], "WARN")
        agents.mkdir()
        for src in codex_hooks.AGENTS_DIR.glob("worker-*.toml"):
            (agents / src.name).write_bytes(src.read_bytes())
        self.assertEqual(codex_hooks.workers_status(agents)[0], "OK")
        (agents / "worker-low.toml").write_text("custom", encoding="utf-8")
        self.assertEqual(codex_hooks.workers_status(agents)[0], "WARN")


class TestCodexHomeEnv(unittest.TestCase):
    """Install and doctor checks follow CODEX_HOME, like user_config."""

    def test_default_paths_follow_codex_home(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "ch"
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                self.assertEqual(codex_hooks.hooks_status()[0], "WARN")
                self.assertIn(str(home), codex_hooks.hooks_status()[1])
                with redirect_stdout(io.StringIO()):
                    codex_hooks.cmd_codex_hooks(Namespace(install=True, hooks=None, agents_dir=None))
                self.assertTrue((home / "hooks.json").is_file())
                self.assertTrue((home / "agents" / "worker-low.toml").is_file())
                self.assertEqual(codex_hooks.hooks_status()[0], "OK")
                self.assertEqual(codex_hooks.workers_status()[0], "OK")
                self.assertEqual(codex_hooks.model_status()[0], "WARN")
                self.assertIn(str(home), codex_hooks.model_status()[1])


if __name__ == "__main__":
    unittest.main()
