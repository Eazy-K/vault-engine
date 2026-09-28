"""Tests for tools/claude-hooks/delegation-warn.py (PostToolUse hook that nudges
the orchestrator to delegate after several tool calls in one user message).

Runs the script as a real subprocess, feeding it JSON on stdin, with the state
directory redirected into a temp dir via TMPDIR/TEMP/TMP env overrides so the
real system temp dir is never touched. Standard library only.
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
SCRIPT = REPO_ROOT / "tools" / "claude-hooks" / "delegation-warn.py"


class TestDelegationWarn(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.session_id = f"test-{id(self)}"
        self.env = dict(os.environ)
        self.env["TMPDIR"] = str(self.tmp)
        self.env["TEMP"] = str(self.tmp)
        self.env["TMP"] = str(self.tmp)

    def _run(self, prompt_id="p1", tool_name="Bash", agent_id=None,
              env_extra=None) -> subprocess.CompletedProcess:
        payload = {"session_id": self.session_id, "prompt_id": prompt_id,
                   "tool_name": tool_name, "tool_input": {}}
        if agent_id is not None:
            payload["agent_id"] = agent_id
        env = dict(self.env)
        if env_extra:
            env.update(env_extra)
        return subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                               capture_output=True, text=True, env=env)

    def test_first_three_calls_are_silent(self):
        for _ in range(3):
            out = self._run()
            self.assertEqual(out.returncode, 0)
            self.assertEqual(out.stdout.strip(), "")

    def test_warns_once_after_three_calls(self):
        for _ in range(3):
            self._run()
        fourth = self._run()
        self.assertEqual(fourth.returncode, 0)
        payload = json.loads(fourth.stdout)
        msg = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("delegate", msg)
        self.assertIn("worker", msg)

        # further calls in the same prompt stay silent (warn once per prompt)
        fifth = self._run()
        self.assertEqual(fifth.stdout.strip(), "")
        sixth = self._run()
        self.assertEqual(sixth.stdout.strip(), "")

    def test_new_prompt_id_resets_counter(self):
        for _ in range(4):
            self._run(prompt_id="p1")
        # new prompt_id: counter resets, so the next 3 calls are silent again
        for _ in range(3):
            out = self._run(prompt_id="p2")
            self.assertEqual(out.stdout.strip(), "")
        fourth = self._run(prompt_id="p2")
        self.assertNotEqual(fourth.stdout.strip(), "")

    def test_subagent_calls_are_always_skipped(self):
        for _ in range(6):
            out = self._run(agent_id="sub-1")
            self.assertEqual(out.returncode, 0)
            self.assertEqual(out.stdout.strip(), "")

    def test_garbage_stdin_exits_0_no_traceback(self):
        out = subprocess.run([sys.executable, str(SCRIPT)], input="not json {{{",
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")
        self.assertEqual(out.stderr.strip(), "")

    def test_empty_stdin_exits_0_no_traceback(self):
        out = subprocess.run([sys.executable, str(SCRIPT)], input="",
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")
        self.assertEqual(out.stderr.strip(), "")

    def test_non_object_json_exits_0_no_traceback(self):
        out = subprocess.run([sys.executable, str(SCRIPT)], input="[1, 2, 3]",
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")
        self.assertEqual(out.stderr.strip(), "")

    def test_missing_session_or_prompt_id_is_silent(self):
        out = subprocess.run([sys.executable, str(SCRIPT)],
                              input=json.dumps({"tool_name": "Bash"}),
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")

    def test_unwritable_state_dir_exits_0_no_traceback(self):
        # Point the state dir at a path that can't be created (a file in place
        # of a directory), so os.makedirs fails inside save_state.
        blocker = self.tmp / "vault-engine-delegation"
        blocker.write_text("not a directory", encoding="utf-8")
        out = self._run()
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stderr.strip(), "")


if __name__ == "__main__":
    unittest.main()
