"""Tests for tools/claude-hooks/context-warn.py (UserPromptSubmit hook that warns
the model when context has grown past the session's first call by a delta).

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
import sys as _sys
from pathlib import Path as _P
_sys.path.insert(0, str(_P(__file__).resolve().parent))
import _isolation  # noqa: E402,F401  (scrubs VAULT_DATA/VAULT_HOME)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "claude-hooks" / "context-warn.py"


def _usage(n: int) -> dict:
    return {"input_tokens": n, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}


def _assistant_line(usage: dict) -> str:
    return json.dumps({"type": "assistant", "message": {"usage": usage}})


class TestContextWarn(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.transcript = self.tmp / "transcript.jsonl"
        self.session_id = f"test-{id(self)}"
        self.env = dict(os.environ)
        for k in ("VAULT_CONTEXT_WARN", "VAULT_DATA", "VAULT_HOME"):
            self.env.pop(k, None)
        self.env["TMPDIR"] = str(self.tmp)
        self.env["TEMP"] = str(self.tmp)
        self.env["TMP"] = str(self.tmp)

    def _write_transcript(self, *token_counts):
        with open(self.transcript, "w", encoding="utf-8", newline="\n") as f:
            for n in token_counts:
                f.write(_assistant_line(_usage(n)) + "\n")

    def _run(self, env_extra=None) -> subprocess.CompletedProcess:
        payload = {"session_id": self.session_id, "transcript_path": str(self.transcript)}
        env = dict(self.env)
        if env_extra:
            env.update(env_extra)
        return subprocess.run([sys.executable, "-I", str(SCRIPT)], input=json.dumps(payload),
                              capture_output=True, text=True, env=env)

    def _msg(self, out) -> str:
        self.assertEqual(out.returncode, 0)
        if not out.stdout.strip():
            return ""
        return json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]

    def _state(self) -> dict:
        p = self.tmp / f"vault-context-warn-{self.session_id}.json"
        return json.loads(p.read_text(encoding="utf-8"))

    def test_under_threshold_silent_and_baseline_recorded(self):
        self._write_transcript(40000, 60000)
        out = self._run()
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")
        self.assertEqual(self._state()["baseline"], 40000)

    def test_warn_at_baseline_plus_50k(self):
        self._write_transcript(60000, 109000)
        self.assertEqual(self._msg(self._run()), "")
        self._write_transcript(60000, 110000)
        msg = self._msg(self._run())
        self.assertIn("context-warn", msg)
        self.assertIn("baseline 60000", msg)
        self.assertIn("subagents", msg)

    def test_warn_with_small_baseline(self):
        self._write_transcript(37000, 86000)
        self.assertEqual(self._msg(self._run()), "")
        self._write_transcript(37000, 87000)
        self.assertIn("baseline 37000", self._msg(self._run()))

    def test_baseline_not_overwritten(self):
        self._write_transcript(30000, 35000)
        self._run()
        self._write_transcript(30000, 35000, 36000)
        self._run()
        self.assertEqual(self._state()["baseline"], 30000)

    def test_handoff_right_after_warn(self):
        self._write_transcript(60000, 110000)
        self.assertIn("baseline 60000", self._msg(self._run()))
        self._write_transcript(60000, 110000, 140000)
        msg = self._msg(self._run())
        self.assertIn("handoff note", msg)
        self.assertIn("new session", msg)
        # handoff already emitted: small growth is silent
        self._write_transcript(60000, 110000, 140000, 145000)
        self.assertEqual(self._msg(self._run()), "")

    def test_no_rewarn_until_growth_step(self):
        self._write_transcript(60000, 112000)
        self.assertNotEqual(self._msg(self._run()), "")
        self._write_transcript(60000, 117000)
        self.assertEqual(self._msg(self._run()), "")
        self._write_transcript(60000, 138000)
        self.assertNotEqual(self._msg(self._run()), "")

    def test_absolute_env_override(self):
        self._write_transcript(5000, 100000)
        msg = self._msg(self._run(env_extra={"VAULT_CONTEXT_WARN": "100000"}))
        self.assertIn("context-warn", msg)
        # baseline+delta alone would not have warned at 100000 for baseline 60000
        self.session_id += "-b"
        self._write_transcript(60000, 100000)
        self.assertIn("context-warn", self._msg(self._run(env_extra={"VAULT_CONTEXT_WARN": "100000"})))
        self.assertEqual(self._msg(self._run(env_extra={"VAULT_CONTEXT_WARN": "bogus"})), "")

    def test_config_warn_delta_and_usage_log(self):
        data = self.tmp / "data"
        (data / ".graph").mkdir(parents=True)
        (data / "vault.config.json").write_text(
            json.dumps({"budgets": {"context": {"warn_delta": 20000, "handoff_delta": 40000}}}),
            encoding="utf-8", newline="\n")
        env = {"VAULT_DATA": str(data)}
        self._write_transcript(10000, 29000)
        self.assertEqual(self._msg(self._run(env)), "")
        self._write_transcript(10000, 30000)
        self.assertIn("context-warn", self._msg(self._run(env)))
        lines = (data / ".graph" / "usage.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        ev = json.loads(lines[0])
        self.assertEqual(ev["event"], "context_warn")
        self.assertEqual((ev["level"], ev["tokens"], ev["baseline"], ev["threshold"]),
                         ("warn", 30000, 10000, 30000))
        self.assertEqual(ev["agent"], "claude")

    def test_invalid_config_uses_defaults(self):
        data = self.tmp / "data"
        data.mkdir()
        (data / "vault.config.json").write_text(
            json.dumps({"budgets": {"context": {"warn_delta": "x"}}}), encoding="utf-8", newline="\n")
        self._write_transcript(10000, 59000)
        self.assertEqual(self._msg(self._run({"VAULT_DATA": str(data)})), "")
        self._write_transcript(10000, 60000)
        self.assertNotEqual(self._msg(self._run({"VAULT_DATA": str(data)})), "")

    def test_no_prior_assistant_turn_silent(self):
        self.transcript.write_text("", encoding="utf-8", newline="\n")
        out = self._run()
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")

    def test_missing_transcript_silent(self):
        payload = {"session_id": self.session_id,
                   "transcript_path": str(self.tmp / "does-not-exist.jsonl")}
        out = subprocess.run([sys.executable, "-I", str(SCRIPT)], input=json.dumps(payload),
                             capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")

    def test_garbage_transcript_lines_are_skipped(self):
        with open(self.transcript, "w", encoding="utf-8", newline="\n") as f:
            f.write("not json {{{\n")
            f.write(_assistant_line(_usage(10000)) + "\n")
            f.write("{{{ garbage\n")
            f.write(_assistant_line(_usage(70000)) + "\n")
        out = self._run()
        self.assertIn("baseline 10000", self._msg(out))

    def test_garbage_stdin_is_silent(self):
        out = subprocess.run([sys.executable, "-I", str(SCRIPT)], input="not json {{{",
                             capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")

    def test_empty_stdin_is_silent(self):
        out = subprocess.run([sys.executable, "-I", str(SCRIPT)], input="",
                             capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
