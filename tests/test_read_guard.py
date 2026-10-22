"""Tests for tools/claude-hooks/read-guard.py (PreToolUse hook that gives the
orchestrator a per-prompt read budget). Runs the script as a subprocess with JSON on
stdin, like Claude Code does. Standard library only.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _isolation  # noqa: E402,F401  (scrubs VAULT_DATA/VAULT_HOME)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "claude-hooks" / "read-guard.py"


class TestReadGuard(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.data, ignore_errors=True)
        self.env = dict(os.environ)
        for k in ("VAULT_READ_GUARD", "VAULT_READ_BUDGET", "VAULT_HOME"):
            self.env.pop(k, None)
        self.env["VAULT_DATA"] = str(self.data)

    def run_hook(self, payload, env_extra=None):
        env = dict(self.env)
        env.update(env_extra or {})
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([sys.executable, str(SCRIPT)], input=stdin,
                              capture_output=True, text=True, env=env)

    @staticmethod
    def call(tool="Read", prompt="p1", command=None, **extra):
        payload = {"session_id": "s1", "prompt_id": prompt, "tool_name": tool,
                   "tool_input": {"command": command} if command else {"file_path": "x"}}
        payload.update(extra)
        return payload

    def assertPass(self, out):
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")

    def assertDenied(self, out):
        self.assertEqual(out.returncode, 0)
        hso = json.loads(out.stdout)["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("worker-low", hso["permissionDecisionReason"])

    def test_reads_under_budget_pass_then_denied(self):
        for tool in ("Read", "Grep", "Glob"):
            self.assertPass(self.run_hook(self.call(tool)))
        self.assertDenied(self.run_hook(self.call("Read")))
        self.assertDenied(self.run_hook(self.call("Grep")))

    def test_subagent_call_passes_and_is_not_counted(self):
        for _ in range(6):
            self.assertPass(self.run_hook(self.call(agent_id="a1", agent_type="Explore")))
        self.assertPass(self.run_hook(self.call()))

    def test_bash_verification_not_counted(self):
        for cmd in ("git status", "gh pr view 1", "pytest -q", "python -m unittest",
                    'python "$VAULT_ENGINE/tools/graph.py" context'):
            for _ in range(2):
                self.assertPass(self.run_hook(self.call("Bash", command=cmd)))
        for _ in range(3):
            self.assertPass(self.run_hook(self.call("Read")))
        self.assertDenied(self.run_hook(self.call("Read")))

    def test_bash_cat_and_sed_count(self):
        self.assertPass(self.run_hook(self.call("Bash", command="cat a.txt")))
        self.assertPass(self.run_hook(self.call("Bash", command="sed -n 1,20p a.txt")))
        self.assertPass(self.run_hook(self.call("Bash", command="rg foo src | head")))
        self.assertDenied(self.run_hook(self.call("Bash", command="head -5 a.txt")))

    def test_other_tools_and_writes_ignored(self):
        for tool in ("Edit", "Write", "Agent", "WebFetch"):
            for _ in range(5):
                self.assertPass(self.run_hook(self.call(tool)))
        for _ in range(5):
            self.assertPass(self.run_hook(self.call("Bash", command="rm -rf build")))

    def test_off_switch(self):
        for _ in range(6):
            self.assertPass(self.run_hook(self.call(), {"VAULT_READ_GUARD": "off"}))

    def test_budget_override(self):
        extra = {"VAULT_READ_BUDGET": "1"}
        self.assertPass(self.run_hook(self.call(), extra))
        self.assertDenied(self.run_hook(self.call(), extra))
        self.assertPass(self.run_hook(self.call(prompt="q"), {"VAULT_READ_BUDGET": "5"}))

    def test_invalid_budget_falls_back_to_default(self):
        extra = {"VAULT_READ_BUDGET": "many"}
        for _ in range(3):
            self.assertPass(self.run_hook(self.call(), extra))
        self.assertDenied(self.run_hook(self.call(), extra))

    def test_counter_resets_on_new_prompt(self):
        for _ in range(3):
            self.assertPass(self.run_hook(self.call(prompt="p1")))
        self.assertDenied(self.run_hook(self.call(prompt="p1")))
        for _ in range(3):
            self.assertPass(self.run_hook(self.call(prompt="p2")))
        self.assertDenied(self.run_hook(self.call(prompt="p2")))

    def test_state_lives_under_graph_dir(self):
        self.run_hook(self.call())
        self.assertTrue(list((self.data / ".graph" / "read-guard").glob("*.json")))

    def test_malformed_or_incomplete_input_is_silent(self):
        for stdin in ("", "not json", "[1, 2]", "null", '{"tool_name": "Read"}',
                      json.dumps({"tool_name": "Read", "prompt_id": "p"}),
                      json.dumps({"tool_name": "Bash", "session_id": "s", "prompt_id": "p",
                                  "tool_input": "oops"})):
            self.assertPass(self.run_hook(stdin))

    def test_unwritable_state_fails_open(self):
        blocker = self.data / "file"
        blocker.write_text("x", encoding="utf-8")
        env = {"VAULT_DATA": str(blocker)}  # not a directory: falls back to temp dir
        self.assertPass(self.run_hook(self.call(prompt="fresh-" + self.id()), env))


if __name__ == "__main__":
    unittest.main()
