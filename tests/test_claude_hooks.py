"""Tests for tools/claude_hooks.py (installs the agent-guard PreToolUse hook into
a Claude Code settings.json).

Every test uses a temp settings.json; the real user's ~/.claude/settings.json
is never read or written. Standard library `unittest` only.
"""
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
GRAPH_PATH = TOOLS_DIR / "graph.py"

_spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)

sys.path.insert(0, str(TOOLS_DIR))
import claude_hooks  # noqa: E402


class TestMerge(unittest.TestCase):
    def test_merge_into_empty_settings(self):
        settings, changed = claude_hooks.merge({})
        self.assertTrue(changed)
        hooks = settings["hooks"]["PreToolUse"]
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]["matcher"], "Agent")
        self.assertIn("agent-guard.py", hooks[0]["hooks"][0]["command"])

    def test_merge_is_idempotent(self):
        settings, _ = claude_hooks.merge({})
        settings2, changed = claude_hooks.merge(settings)
        self.assertFalse(changed)
        self.assertEqual(len(settings2["hooks"]["PreToolUse"]), 1)

    def test_merge_keeps_existing_unrelated_hooks(self):
        existing = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]},
                ],
                "PostToolUse": [
                    {"matcher": "*", "hooks": [{"type": "command", "command": "echo done"}]},
                ],
            },
        }
        settings, changed = claude_hooks.merge(existing)
        self.assertTrue(changed)
        pre = settings["hooks"]["PreToolUse"]
        self.assertEqual(len(pre), 2)
        self.assertEqual(settings["hooks"]["PostToolUse"], existing["hooks"]["PostToolUse"])
        commands = {h["command"] for entry in pre for h in entry["hooks"]}
        self.assertIn("echo hi", commands)

    def test_merge_updates_stale_command_in_place(self):
        existing = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Agent",
                     "hooks": [{"type": "command", "command": "python /old/path/agent-guard.py"}]},
                ],
            },
        }
        settings, changed = claude_hooks.merge(existing)
        self.assertTrue(changed)
        pre = settings["hooks"]["PreToolUse"]
        self.assertEqual(len(pre), 1)
        self.assertIn(str(claude_hooks.GUARD_SCRIPT), pre[0]["hooks"][0]["command"])


class TestCmdClaudeHooks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.settings_path = self.tmp / "settings.json"

    def _run(self, install: bool) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            claude_hooks.cmd_claude_hooks(
                Namespace(install=install, settings=str(self.settings_path)))
        return buf.getvalue()

    def test_dry_run_does_not_write(self):
        out = self._run(install=False)
        self.assertFalse(self.settings_path.exists())
        self.assertIn("would update", out)

    def test_install_writes_settings(self):
        out = self._run(install=True)
        self.assertTrue(self.settings_path.exists())
        self.assertIn("installed", out)
        data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(data["hooks"]["PreToolUse"][0]["matcher"], "Agent")

    def test_install_is_idempotent_no_duplicate_backup_on_rerun(self):
        self._run(install=True)
        before = self.settings_path.read_text(encoding="utf-8")
        out = self._run(install=True)
        after = self.settings_path.read_text(encoding="utf-8")
        self.assertEqual(before, after)
        self.assertIn("up to date", out)
        backups = list(self.tmp.glob("settings.json.bak-*"))
        self.assertEqual(backups, [])

    def test_install_backs_up_existing_file_before_changing_it(self):
        self.settings_path.write_text(
            json.dumps({"hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}]}}),
            encoding="utf-8")
        self._run(install=True)
        backups = list(self.tmp.glob("settings.json.bak-*"))
        self.assertEqual(len(backups), 1)
        backup_data = json.loads(backups[0].read_text(encoding="utf-8"))
        self.assertEqual(backup_data["hooks"]["PreToolUse"][0]["matcher"], "Bash")
        new_data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        matchers = {e["matcher"] for e in new_data["hooks"]["PreToolUse"]}
        self.assertEqual(matchers, {"Bash", "Agent"})

    def test_install_creates_missing_parent_dirs(self):
        nested = self.tmp / "nested" / "dir" / "settings.json"
        buf = io.StringIO()
        with redirect_stdout(buf):
            claude_hooks.cmd_claude_hooks(Namespace(install=True, settings=str(nested)))
        self.assertTrue(nested.exists())

    def test_default_settings_path_used_when_none_given(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            claude_hooks.cmd_claude_hooks(Namespace(install=False, settings=None))
        out = buf.getvalue()
        self.assertIn(str(claude_hooks.DEFAULT_SETTINGS), out)


class TestStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.settings_path = self.tmp / "settings.json"

    def test_status_warn_when_missing(self):
        level, msg = claude_hooks.status(self.settings_path)
        self.assertEqual(level, "WARN")
        self.assertIn("not installed", msg)

    def test_status_ok_after_install(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            claude_hooks.cmd_claude_hooks(
                Namespace(install=True, settings=str(self.settings_path)))
        level, msg = claude_hooks.status(self.settings_path)
        self.assertEqual(level, "OK")


if __name__ == "__main__":
    unittest.main()
