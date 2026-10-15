"""Tests for tools/claude-hooks/agent-guard.py (PreToolUse hook that stops the
orchestrator from starting anything but worker-low/worker-medium).

Runs the script as a real subprocess (like Claude Code itself would), feeding
it JSON on stdin and checking exit code + stdout. Standard library only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
import sys as _sys
from pathlib import Path as _P
_sys.path.insert(0, str(_P(__file__).resolve().parent))
import _isolation  # noqa: E402,F401  (scrubs VAULT_DATA/VAULT_HOME)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "claude-hooks" / "agent-guard.py"


def _run(payload, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("VAULT_AGENT_GUARD", None)
    # Isolate from this machine's real vault: without this, a host that has set
    # VAULT_DATA/VAULT_HOME to a vault.config.json with a non-default orchestrator
    # model would change ALLOWED_MODELS and make these tests flaky.
    env.pop("VAULT_DATA", None)
    env.pop("VAULT_HOME", None)
    if env_extra:
        env.update(env_extra)
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(SCRIPT)], input=stdin,
                           capture_output=True, text=True, env=env)


def _assert_denied(case, out) -> None:
    case.assertEqual(out.returncode, 0)
    payload = json.loads(out.stdout)
    case.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "deny")


def _agent_call(subagent_type=None, model=None) -> dict:
    tool_input = {}
    if subagent_type is not None:
        tool_input["subagent_type"] = subagent_type
    if model is not None:
        tool_input["model"] = model
    return {"tool_name": "Agent", "tool_input": tool_input}


class TestAgentGuard(unittest.TestCase):
    assertDenied = _assert_denied

    def test_allows_worker_low_with_sonnet(self):
        out = _run(_agent_call("worker-low", "sonnet"))
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")

    def test_denies_worker_medium_with_non_configured_model(self):
        out = _run(_agent_call("worker-medium", "haiku"))
        self.assertDenied(out)

    def test_denies_unknown_worker_role(self):
        self.assertDenied(_run(_agent_call("worker-foo")))
        self.assertDenied(_run(_agent_call("worker-foo", "sonnet")))

    def test_allows_worker_without_model_override(self):
        out = _run(_agent_call("worker-low"))
        self.assertEqual(out.returncode, 0)

    def test_denies_general_purpose(self):
        out = _run(_agent_call("general-purpose"))
        self.assertDenied(out)
        payload = json.loads(out.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("worker-", payload["hookSpecificOutput"]["permissionDecisionReason"])

    def test_denies_fork(self):
        out = _run(_agent_call("fork"))
        self.assertDenied(out)

    def test_denies_explore(self):
        out = _run(_agent_call("Explore"))
        self.assertDenied(out)

    def test_denies_plan(self):
        out = _run(_agent_call("Plan"))
        self.assertDenied(out)

    def test_denies_missing_subagent_type(self):
        out = _run(_agent_call(None))
        self.assertDenied(out)

    def test_denies_expensive_model_override(self):
        out = _run(_agent_call("worker-low", "opus"))
        self.assertDenied(out)
        payload = json.loads(out.stdout)
        self.assertIn("model", payload["hookSpecificOutput"]["permissionDecisionReason"])

    def test_ignores_other_tools(self):
        out = _run({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")

    def test_escape_hatch_allows_everything(self):
        out = _run(_agent_call("general-purpose", "opus"), env_extra={"VAULT_AGENT_GUARD": "off"})
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")

    def test_garbage_input_allows(self):
        out = _run("not json at all {{{")
        self.assertEqual(out.returncode, 0)

    def test_empty_input_allows(self):
        out = _run("")
        self.assertEqual(out.returncode, 0)

    def test_non_object_json_allows(self):
        out = _run("[1, 2, 3]")
        self.assertEqual(out.returncode, 0)

    def test_non_string_subagent_type_denies(self):
        out = _run({"tool_name": "Agent", "tool_input": {"subagent_type": 123}})
        self.assertDenied(out)


class TestAgentGuardModelConfig(unittest.TestCase):
    """The allowed override is the role's configured model from
    <data>/vault.config.json (+ machine.json) "models" -> <role> -> "model"
    (see tools/model_config.py), read via VAULT_DATA. Everything here uses a
    throwaway temp dir; the real vault is never read."""

    assertDenied = _assert_denied

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.env = {"VAULT_DATA": str(self.tmp)}

    def _write_config(self, models: dict) -> None:
        (self.tmp / "vault.config.json").write_text(
            json.dumps({"models": models}), encoding="utf-8")

    def test_no_config_falls_back_to_default(self):
        self.assertDenied(_run(_agent_call("worker-low", "opus"), env_extra=self.env))
        out = _run(_agent_call("worker-low", "sonnet"), env_extra=self.env)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")

    def test_override_equal_to_configured_model_allowed(self):
        self._write_config({"worker-low": {"model": "haiku"}})
        out = _run(_agent_call("worker-low", "haiku"), env_extra=self.env)
        self.assertEqual(out.stdout.strip(), "")

    def test_other_model_denied(self):
        self._write_config({"worker-low": {"model": "sonnet"}})
        for model in ("haiku", "opus"):
            self.assertDenied(_run(_agent_call("worker-low", model), env_extra=self.env))

    def test_same_model_as_orchestrator_allowed_when_configured(self):
        self._write_config({"orchestrator": {"model": "sonnet"},
                            "worker-medium": {"model": "sonnet"}})
        out = _run(_agent_call("worker-medium", "sonnet"), env_extra=self.env)
        self.assertEqual(out.stdout.strip(), "")

    def test_each_role_uses_its_own_model(self):
        self._write_config({"worker-low": {"model": "haiku"}})
        self.assertDenied(_run(_agent_call("worker-medium", "haiku"), env_extra=self.env))

    def test_deny_message_mentions_configured_model(self):
        self._write_config({"worker-low": {"model": "haiku"}})
        out = _run(_agent_call("worker-low", "opus"), env_extra=self.env)
        self.assertDenied(out)
        reason = json.loads(out.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("haiku", reason)
        self.assertNotIn("cheap", reason)

    def test_invalid_config_falls_back_to_default(self):
        (self.tmp / "vault.config.json").write_text("not json", encoding="utf-8")
        out = _run(_agent_call("worker-low", "sonnet"), env_extra=self.env)
        self.assertEqual(out.returncode, 0)
        self.assertDenied(_run(_agent_call("worker-low", "opus"), env_extra=self.env))


if __name__ == "__main__":
    unittest.main()
