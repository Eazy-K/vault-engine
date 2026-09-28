"""Tests for tools/claude-hooks/context-warn.py (UserPromptSubmit hook that warns
the model when context usage crosses a threshold).

Runs the script as a real subprocess, feeding it JSON on stdin and a temp
transcript file (JSONL) it reads usage from. Standard library only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "claude-hooks" / "context-warn.py"


def _assistant_line(usage: dict) -> str:
    return json.dumps({"type": "assistant", "message": {"usage": usage}})


class TestContextWarn(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.transcript = self.tmp / "transcript.jsonl"
        # unique state file per test via a fresh session_id and tmp state dir
        self.session_id = f"test-{id(self)}"
        self.env = dict(os.environ)
        self.env.pop("VAULT_CONTEXT_WARN", None)
        self.env["TMPDIR"] = str(self.tmp)
        self.env["TEMP"] = str(self.tmp)
        self.env["TMP"] = str(self.tmp)

    def _write_transcript(self, usages):
        with open(self.transcript, "w", encoding="utf-8") as f:
            for usage in usages:
                f.write(_assistant_line(usage) + "\n")

    def _run(self, env_extra=None) -> subprocess.CompletedProcess:
        payload = {"session_id": self.session_id, "transcript_path": str(self.transcript)}
        env = dict(self.env)
        if env_extra:
            env.update(env_extra)
        return subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                               capture_output=True, text=True, env=env)

    def test_under_threshold_only_has_reminder(self):
        self._write_transcript([{"input_tokens": 1000, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}])
        out = self._run()
        self.assertEqual(out.returncode, 0)
        payload = json.loads(out.stdout)
        msg = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Reminder", msg)
        self.assertIn("worker-low", msg)
        self.assertNotIn("context-warn", msg)
        self.assertNotIn("systemMessage", payload["hookSpecificOutput"])

    def test_over_threshold_warns(self):
        self._write_transcript([{"input_tokens": 160000, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}])
        out = self._run()
        self.assertEqual(out.returncode, 0)
        payload = json.loads(out.stdout)
        msg = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("context-warn", msg)
        self.assertIn("vault", msg)
        self.assertIn("compact", msg)
        self.assertIn("Reminder", msg)

        system_msg = payload["hookSpecificOutput"]["systemMessage"]
        self.assertIn("context-warn", system_msg)
        self.assertIn("vault", system_msg)
        self.assertIn("compact", system_msg)
        self.assertNotIn("Reminder", system_msg)

    def test_custom_threshold_env(self):
        self._write_transcript([{"input_tokens": 5000, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}])
        out = self._run(env_extra={"VAULT_CONTEXT_WARN": "1000"})
        self.assertEqual(out.returncode, 0)
        self.assertNotEqual(out.stdout.strip(), "")

    def test_no_rewarn_until_growth_step(self):
        self._write_transcript([{"input_tokens": 160000, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}])
        first = self._run()
        self.assertNotEqual(first.stdout.strip(), "")

        # small growth, still under GROWTH_STEP (25000) since last warning
        self._write_transcript([{"input_tokens": 165000, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}])
        second = self._run()
        second_payload = json.loads(second.stdout)["hookSpecificOutput"]
        second_msg = second_payload["additionalContext"]
        self.assertIn("Reminder", second_msg)
        self.assertNotIn("context-warn", second_msg)
        self.assertNotIn("systemMessage", second_payload)

        # enough growth: re-warn
        self._write_transcript([{"input_tokens": 190000, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}])
        third = self._run()
        self.assertNotEqual(third.stdout.strip(), "")

    def test_no_prior_assistant_turn_only_has_reminder(self):
        self.transcript.write_text("", encoding="utf-8")
        out = self._run()
        self.assertEqual(out.returncode, 0)
        msg = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Reminder", msg)
        self.assertNotIn("context-warn", msg)

    def test_missing_transcript_only_has_reminder(self):
        payload = {"session_id": self.session_id,
                   "transcript_path": str(self.tmp / "does-not-exist.jsonl")}
        out = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 0)
        msg = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Reminder", msg)
        self.assertNotIn("context-warn", msg)

    def test_garbage_transcript_lines_are_skipped(self):
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write("not json {{{\n")
            f.write(_assistant_line({"input_tokens": 160000, "cache_creation_input_tokens": 0,
                                      "cache_read_input_tokens": 0}) + "\n")
        out = self._run()
        self.assertEqual(out.returncode, 0)
        self.assertNotEqual(out.stdout.strip(), "")

    def test_garbage_stdin_is_silent(self):
        out = subprocess.run([sys.executable, str(SCRIPT)], input="not json {{{",
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")

    def test_empty_stdin_is_silent(self):
        out = subprocess.run([sys.executable, str(SCRIPT)], input="",
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
