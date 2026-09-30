"""Codex UserPromptSubmit orchestration reminder and context warning tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import sys as _sys
from pathlib import Path as _P
_sys.path.insert(0, str(_P(__file__).resolve().parent))
import _isolation  # noqa: E402,F401  (scrubs VAULT_DATA/VAULT_HOME)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "codex-hooks" / "context-warn.py"


class TestCodexContextWarn(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.rollout = self.tmp / "rollout.jsonl"
        self.env = dict(os.environ)
        self.env["CODEX_CONTEXT_WARN_STATE_DIR"] = str(self.tmp / "state")

    def _run(self, payload=None):
        payload = payload or {"session_id": "session-test",
                               "transcript_path": str(self.rollout)}
        return subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                              capture_output=True, text=True, env=self.env)

    def _write_context(self, tokens):
        event = {"payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": tokens}, "model_context_window": 200000}}}
        self.rollout.write_text(json.dumps(event) + "\n", encoding="utf-8")

    def test_reminder_is_present_without_rollout(self):
        out = self._run({"session_id": "session-test"})
        self.assertEqual(out.returncode, 0)
        context = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("present a short plan", context)
        self.assertIn("wait for explicit approval", context)
        self.assertIn("worker-low/worker-medium", context)

    def test_existing_context_warning_is_preserved_with_reminder(self):
        self._write_context(160000)
        self.env["PYTHONIOENCODING"] = "ascii"
        out = self._run()
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Orchestration:", context)
        self.assertIn("[context-warn]", context)
        self.assertIn("[context-warn]", payload["systemMessage"])

    def _meta(self, payload):
        self._write_context(160000)
        body = self.rollout.read_text(encoding="utf-8")
        self.rollout.write_text(json.dumps({"type": "session_meta", "payload": payload})
                                + "\n" + body, encoding="utf-8")

    def test_subagent_rollout_is_silent(self):
        for meta in ({"source": {"subagent": {"thread_spawn": {"parent_thread_id": "p"}}}},
                     {"parent_thread_id": "p"}):
            with self.subTest(meta=meta):
                self._meta(meta)
                out = self._run()
                self.assertEqual(out.returncode, 0)
                self.assertEqual(out.stdout.strip(), "")

    def test_main_rollout_with_session_meta_still_warns(self):
        self._meta({"source": "cli"})
        out = self._run()
        self.assertIn("[context-warn]", json.loads(out.stdout)["systemMessage"])

    def test_subagent_does_not_receive_orchestration_reminder(self):
        out = self._run({"session_id": "session-test", "agent_id": "worker-1"})
        self.assertEqual(out.stdout.strip(), "")

    def test_malformed_input_is_silent(self):
        out = subprocess.run([sys.executable, str(SCRIPT)], input="not json",
                             capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
