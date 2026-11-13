"""Tests for tools/claude-hooks/budget-guard.py (PreToolUse mid-run budget gate)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _isolation  # noqa: E402,F401  (scrubs VAULT_DATA/VAULT_HOME)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "claude-hooks" / "budget-guard.py"


def rec(n, tokens=None):
    """One assistant record with message id `m<n>`."""
    msg = {"id": f"m{n}", "role": "assistant", "content": [{"type": "text", "text": "x"}]}
    if tokens is not None:
        msg["usage"] = {"input_tokens": tokens, "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0}
    return {"type": "assistant", "message": msg}


class TestBudgetGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.sub = self.tmp / "proj" / "sess" / "subagents"
        self.sub.mkdir(parents=True)
        self.transcript = self.sub / "agent-a1.jsonl"

    def _write(self, turns, tokens=None, path=None):
        lines = [json.dumps(rec(i, tokens if i == turns - 1 else None)) for i in range(turns)]
        (path or self.transcript).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _payload(self, **kw):
        p = {"agent_id": "a1", "agent_type": "worker-low", "tool_name": "Bash",
             "session_id": "s1", "transcript_path": str(self.transcript)}
        p.update(kw)
        return p

    def _run(self, payload, data=True, raw=None):
        env = dict(os.environ)
        env.pop("VAULT_HOME", None)
        env.pop("VAULT_DATA", None)
        if data:
            env["VAULT_DATA"] = str(self.data)
        stdin = raw if raw is not None else json.dumps(payload)
        r = subprocess.run([sys.executable, str(SCRIPT)], input=stdin,
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def _events(self):
        log = self.data / ".graph" / "usage.log"
        return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] \
            if log.exists() else []

    def test_main_session_silent(self):
        self._write(25)
        self.assertIsNone(self._run(self._payload(agent_id="")))
        p = self._payload()
        del p["agent_id"]
        self.assertIsNone(self._run(p))

    def test_handback_never_denied(self):
        self._write(25)
        self.assertIsNone(self._run(self._payload(tool_name="SubagentHandback")))
        self.assertEqual(self._events(), [])

    def test_unknown_agent_type_silent(self):
        self._write(25)
        self.assertIsNone(self._run(self._payload(agent_type="Explore")))
        self.assertIsNone(self._run(self._payload(agent_type="")))

    def test_under_80_percent_silent(self):
        self._write(15)  # 15/20 = 75%
        self.assertIsNone(self._run(self._payload()))

    def test_warns_once_at_80_percent(self):
        self._write(16)  # 16/20
        out = self._run(self._payload())
        h = out["hookSpecificOutput"]
        self.assertEqual(h["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", h)
        self.assertIn("turn cap 20", h["additionalContext"])
        self.assertIn("turns 16", h["additionalContext"])
        self.assertTrue((self.data / ".graph" / "budget-warn" / "a1").exists())
        self.assertIsNone(self._run(self._payload()))

    def test_warn_without_data_dir_repeats(self):
        self._write(17)
        self.assertIsNotNone(self._run(self._payload(), data=False))
        self.assertIsNotNone(self._run(self._payload(), data=False))

    def test_marker_name_sanitized(self):
        t = self.sub / "agent-x.y.jsonl"
        self._write(16, path=t)
        self._run(self._payload(agent_id="x.y", transcript_path=str(t)))
        names = [p.name for p in (self.data / ".graph" / "budget-warn").iterdir()]
        self.assertEqual(names, ["x_y"])

    def test_denies_at_100_percent_and_logs_once(self):
        self._write(20)
        for _ in range(2):
            out = self._run(self._payload())
            h = out["hookSpecificOutput"]
            self.assertEqual(h["permissionDecision"], "deny")
            self.assertIn("SubagentHandback", h["permissionDecisionReason"])
            self.assertIn("BLOCKED", h["permissionDecisionReason"])
            self.assertIn("worker-low turn cap 20 exceeded (turns 20)",
                          h["permissionDecisionReason"])
        events = self._events()
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["event"], "budget_block")
        self.assertEqual((e["agent"], e["agent_type"], e["agent_id"], e["max_turns"],
                          e["turns"], e["tool_name"], e["session"]),
                         ("claude", "worker-low", "a1", 20, 20, "Bash", "s1"))

    def test_token_overrun_alone_is_silent(self):
        self._write(3, tokens=5_000_000)
        self.assertIsNone(self._run(self._payload()))
        self.assertEqual(self._events(), [])

    def test_tokens_still_logged_on_block(self):
        self._write(20, tokens=80000)
        self._run(self._payload())
        self.assertEqual(self._events()[0]["tokens"], 80000)

    def test_multi_record_message_counts_one_turn(self):
        lines = [json.dumps(rec(0))] * 30  # same message.id: one turn
        self.transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertIsNone(self._run(self._payload()))

    def test_transcript_derived_from_main_transcript_path(self):
        main = self.tmp / "proj" / "sess.jsonl"
        main.write_text("", encoding="utf-8")
        self._write(20)  # proj/sess/subagents/agent-a1.jsonl
        out = self._run(self._payload(transcript_path=str(main)))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_agent_transcript_path_accepted(self):
        self._write(20)
        out = self._run(self._payload(transcript_path="", agent_transcript_path=str(self.transcript)))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_missing_transcript_silent(self):
        self.assertIsNone(self._run(self._payload()))

    def test_garbage_stdin_silent(self):
        self.assertIsNone(self._run(None, raw="not json {"))
        self.assertIsNone(self._run(None, raw="[1,2]"))
        self.assertIsNone(self._run(None, raw=""))

    def test_config_override_roles(self):
        cfg = {"budgets": {"roles": {"worker-low": 5}}}
        (self.data / "vault.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self._write(5)
        out = self._run(self._payload())
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("turn cap 5 exceeded (turns 5)",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_design_roles_have_their_caps(self):
        for role, cap in (("worker-design", 60), ("worker-design-verify", 25)):
            with self.subTest(role=role):
                marker = self.data / ".graph" / "budget-warn" / "a1"
                if marker.exists():
                    marker.unlink()
                self._write(cap - 1)
                self.assertIsNotNone(self._run(self._payload(agent_type=role)))  # >=80%: warn
                self._write(cap)
                out = self._run(self._payload(agent_type=role))
                h = out["hookSpecificOutput"]
                self.assertEqual(h["permissionDecision"], "deny")
                self.assertIn(f"{role} turn cap {cap} exceeded (turns {cap})",
                              h["permissionDecisionReason"])

    def test_config_old_shape_still_read(self):
        cfg = {"budgets": {"classes": {"S": {"max_turns": 5, "max_tokens": 1000}},
                           "roles": {"worker-low": "S"}}}
        (self.data / "vault.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self._write(5)
        out = self._run(self._payload())
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


class TestFallbackTableParity(unittest.TestCase):
    """The hook's _FallbackBudgets table must match budgets.FALLBACK_ROLES."""

    def test_fallback_roles_match_budgets_module(self):
        import importlib.util
        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import budgets
        spec = importlib.util.spec_from_file_location("hook_under_test_budget_guard", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod._FallbackBudgets.ROLES, budgets.FALLBACK_ROLES)
        self.assertEqual(mod._FallbackBudgets.ROLES["worker-design"], 60)
        self.assertEqual(mod._FallbackBudgets.ROLES["worker-design-verify"], 25)


if __name__ == "__main__":
    unittest.main()
