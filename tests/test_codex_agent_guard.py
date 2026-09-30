"""Codex PreToolUse agent-guard: worker-only spawns, fail-open behavior."""
from __future__ import annotations

import json
import os
import subprocess
import sys
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


if __name__ == "__main__":
    unittest.main()
