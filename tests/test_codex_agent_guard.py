"""Codex PreToolUse agent-guard: worker-only spawns, fail-open behavior."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "codex-hooks" / "agent-guard.py"


def invoke(payload, env=None, raw=None):
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=raw if raw is not None else json.dumps(payload),
        text=True, capture_output=True, env=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


def spawn(tool, args):
    return invoke({"tool_name": tool, "tool_input": args})


class TestCodexAgentGuard(unittest.TestCase):
    def test_workers_with_defaults_or_matching_overrides_pass(self):
        self.assertIsNone(spawn("spawn_agent", {"agent_type": "worker-low"}))
        self.assertIsNone(spawn("Agent", {"agent_type": "worker-medium",
                                           "model": "gpt-6-luna", "reasoning_effort": "high"}))

    def test_unapproved_role_model_and_effort_are_denied(self):
        for args in ({"agent_type": "default"}, {"agent_type": "explorer"}, {},
                     {"agent_type": "worker-low", "model": "gpt-6-sol"},
                     {"agent_type": "worker-low", "reasoning_effort": "low"},
                     {"agent_type": "worker-medium", "reasoning_effort": "medium"}):
            with self.subTest(args=args):
                out = spawn("spawn_agent", args)
                self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")

    def test_real_codex_spawn_shape_and_namespaced_names(self):
        # Shape of a real top-level spawn_agent call (collaboration namespace), anonymized.
        real = {"task_name": "audit", "agent_type": "worker-low", "fork_turns": "none",
                "message": "do the thing"}
        self.assertIsNone(spawn("spawn_agent", real))
        self.assertIsNone(spawn("collaboration.spawn_agent", real))
        out = spawn("collaboration.spawn_agent", dict(real, agent_type="default"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_code_mode_exec_is_never_denied(self):
        script = 'const r = await tools.exec_command({cmd:"git status"}); text(r.output);'
        self.assertIsNone(invoke({"tool_name": "exec", "tool_input": script}))
        self.assertIsNone(spawn("exec_command", {"cmd": "git status"}))
        self.assertIsNone(spawn("wait_agent", {"agent_type": "default"}))

    def test_other_tools_and_malformed_input_fail_open(self):
        self.assertIsNone(spawn("Bash", {"agent_type": "default"}))
        for raw in ("", "not json", "[]", '{"tool_name": "spawn_agent", "tool_input": 3}'):
            with self.subTest(raw=raw):
                self.assertIsNone(invoke(None, raw=raw))

    def test_env_switch_disables_guard(self):
        env = dict(os.environ, VAULT_AGENT_GUARD="off")
        self.assertIsNone(invoke({"tool_name": "spawn_agent",
                                  "tool_input": {"agent_type": "default"}}, env=env))


class TestAgentGuardLog(unittest.TestCase):
    def run_guard(self, payload, data, **extra):
        env = {**os.environ, "VAULT_DATA": str(data)}
        env.pop("VAULT_AGENT_GUARD", None)
        env.update(extra)
        return invoke(payload, env=env)

    def entries(self, data):
        log = Path(data) / ".graph" / "usage.log"
        if not log.exists():
            return []
        return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]

    def test_deny_and_allow_are_logged_without_message(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.run_guard({"tool_name": "spawn_agent", "tool_input": {
                "agent_type": "default", "message": "SECRET"}}, d)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIsNone(self.run_guard({"tool_name": "spawn_agent", "tool_input": {
                "agent_type": "worker-low", "message": "SECRET"}}, d))
            deny, allow = self.entries(d)
            self.assertEqual((deny["event"], deny["agent"], deny["decision"], deny["agent_type"]),
                             ("agent_guard", "codex", "deny", "default"))
            self.assertIn("reason", deny)
            self.assertEqual((allow["decision"], allow["agent_type"]), ("allow", "worker-low"))
            self.assertNotIn("reason", allow)
            self.assertNotIn("SECRET", (Path(d) / ".graph" / "usage.log").read_text(encoding="utf-8"))

    def test_non_spawn_and_off_switch_log_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self.run_guard({"tool_name": "exec", "tool_input": {}}, d)
            self.run_guard({"tool_name": "spawn_agent", "tool_input": {"agent_type": "x"}}, d,
                           VAULT_AGENT_GUARD="off")
            self.assertEqual(self.entries(d), [])

    def test_logging_failure_keeps_deny_output(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".graph").write_text("not a dir", encoding="utf-8")
            out = self.run_guard({"tool_name": "spawn_agent",
                                  "tool_input": {"agent_type": "default"}}, d)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
