"""Regression for #72: hooks must emit valid JSON under a legacy (cp1252) stdout.

Each hook runs as a subprocess on its warning/deny path with
PYTHONIOENCODING=cp1252 and PYTHONUTF8=0. Output must be ASCII-safe JSON, exit 0.
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

HOOKS = Path(__file__).resolve().parent.parent / "tools"


class TestLegacyStdoutEncoding(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = dict(os.environ)
        for key in ("VAULT_DATA", "VAULT_HOME", "VAULT_AGENT_GUARD", "VAULT_CONTEXT_WARN"):
            env.pop(key, None)
        env.update(PYTHONIOENCODING="cp1252", PYTHONUTF8="0",
                   TMPDIR=str(self.tmp), TEMP=str(self.tmp), TMP=str(self.tmp),
                   CODEX_CONTEXT_WARN_STATE_DIR=str(self.tmp / "s1"),
                   CODEX_DELEGATION_WARN_STATE_DIR=str(self.tmp / "s2"))
        self.env = env

    def _run(self, script, payload, expect_output=True):
        out = subprocess.run([sys.executable, str(HOOKS / script)],
                             input=json.dumps(payload).encode(),
                             capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        text = out.stdout.decode("ascii")  # raises if any non-ASCII byte leaked
        if not expect_output:
            return None
        return json.loads(text)

    def test_codex_context_warn(self):
        rollout = self.tmp / "rollout.jsonl"
        rollout.write_text(json.dumps({"payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 160000},
            "model_context_window": 200000}}}) + "\n", encoding="utf-8")
        out = self._run("codex-hooks/context-warn.py",
                        {"session_id": "s", "transcript_path": str(rollout)})
        self.assertIn("[context-warn]", out["systemMessage"])

    def test_codex_delegation_warn(self):
        payload = {"session_id": "s", "turn_id": "t", "tool_name": "Bash", "tool_input": {}}
        for _ in range(3):
            self._run("codex-hooks/delegation-warn.py", payload, expect_output=False)
        out = self._run("codex-hooks/delegation-warn.py", payload)
        self.assertIn("systemMessage", out)

    def test_codex_agent_guard(self):
        out = self._run("codex-hooks/agent-guard.py",
                        {"tool_name": "spawn_agent", "tool_input": {"agent_type": "default"}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_claude_context_warn(self):
        transcript = self.tmp / "t.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 900000, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0}}}) + "\n", encoding="utf-8")
        out = self._run("claude-hooks/context-warn.py",
                        {"session_id": "s", "transcript_path": str(transcript)})
        self.assertIn("additionalContext", out["hookSpecificOutput"])

    def test_claude_delegation_warn(self):
        payload = {"session_id": "s", "prompt_id": "p", "tool_name": "Bash", "tool_input": {}}
        for _ in range(3):
            self._run("claude-hooks/delegation-warn.py", payload, expect_output=False)
        out = self._run("claude-hooks/delegation-warn.py", payload)
        self.assertIn("additionalContext", out["hookSpecificOutput"])

    def test_claude_agent_guard(self):
        out = self._run("claude-hooks/agent-guard.py",
                        {"tool_name": "Agent", "tool_input": {"model": "opus"}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
