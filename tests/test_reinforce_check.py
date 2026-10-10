"""Tests for tools/claude-hooks/reinforce-check.py (Stop hook that asks the agent to
run `reinforce` once a task looks finished).

Runs the script as a subprocess with JSON on stdin; the state directory and the
vault data dir are redirected into temp dirs, so nothing real is touched.
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
SCRIPT = REPO_ROOT / "tools" / "claude-hooks" / "reinforce-check.py"


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def bash(cmd, tid="t1"):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}]}}


def result(text, tid="t1"):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text}]}}


CONTEXT_CALL = bash('python tools/graph.py context "x"', "c1")
CONTEXT_OUT = result("notes...\n<!-- when done: python \"/g/graph.py\" reinforce --task abc123 -->", "c1")
REINFORCE = bash('python "/g/graph.py" reinforce --task abc123 note-a', "r1")
REINFORCE_OK = bash('python "/g/graph.py" reinforce --task abc123 note-a --outcome ok', "r2")
COMMIT = bash('git commit -m "x"', "g1")


class TestReinforceCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.session = f"test-{id(self)}"
        self.env = dict(os.environ, TMPDIR=str(self.tmp), TEMP=str(self.tmp),
                        TMP=str(self.tmp), VAULT_DATA=str(self.data))
        self.transcript = self.tmp / "t.jsonl"

    def _run(self, entries, **extra):
        self.transcript.write_text(
            "\n".join(e if isinstance(e, str) else json.dumps(e) for e in entries) + "\n",
            encoding="utf-8")
        payload = {"session_id": self.session, "transcript_path": str(self.transcript),
                   "stop_hook_active": False, "cwd": str(self.tmp), **extra}
        proc = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def _log(self):
        path = self.data / ".graph" / "usage.log"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]

    def test_no_context_allows(self):
        self.assertEqual(self._run([user("hi"), COMMIT]), "")
        self.assertEqual(self._log()[-1]["outcome"], "allowed-no-context")

    def test_reinforce_done_allows(self):
        out = self._run([user("go"), CONTEXT_CALL, CONTEXT_OUT, REINFORCE_OK, COMMIT])
        self.assertEqual(out, "")
        self.assertEqual(self._log()[-1]["outcome"], "allowed-reinforced")

    def test_reinforce_without_outcome_reminds_once_without_blocking(self):
        entries = [user("go"), CONTEXT_CALL, CONTEXT_OUT, REINFORCE, COMMIT]
        out = json.loads(self._run(entries))
        self.assertNotIn("decision", out)
        self.assertIn("--outcome ok|partial|fail", out["systemMessage"])
        self.assertEqual(self._log()[-1]["outcome"], "allowed-reinforced-no-outcome")
        self.assertEqual(self._run(entries), "")
        self.assertEqual(self._log()[-1]["outcome"], "allowed-reinforced")

    def test_no_finish_signal_allows(self):
        out = self._run([user("go"), CONTEXT_CALL, CONTEXT_OUT])
        self.assertEqual(out, "")
        self.assertEqual(self._log()[-1]["outcome"], "allowed-no-finish-signal")

    def test_finish_in_earlier_turn_does_not_count(self):
        out = self._run([user("go"), CONTEXT_CALL, CONTEXT_OUT, COMMIT, user("and now?")])
        self.assertEqual(out, "")

    def test_finish_without_reinforce_blocks(self):
        out = self._run([user("go"), CONTEXT_CALL, CONTEXT_OUT, COMMIT])
        decision = json.loads(out)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("reinforce --task abc123", decision["reason"])
        self.assertIn("graph.py", decision["reason"])
        entry = self._log()[-1]
        self.assertEqual((entry["event"], entry["outcome"], entry["task"]),
                         ("reinforce_check", "blocked", "abc123"))

    def test_push_and_pr_create_are_finish_signals(self):
        for cmd in ("git push origin x", "gh pr create --fill"):
            self.session += "x"
            out = self._run([user("go"), CONTEXT_CALL, CONTEXT_OUT, bash(cmd)])
            self.assertEqual(json.loads(out)["decision"], "block", cmd)

    def test_second_time_same_id_allows(self):
        entries = [user("go"), CONTEXT_CALL, CONTEXT_OUT, COMMIT]
        self.assertNotEqual(self._run(entries), "")
        self.assertEqual(self._run(entries), "")
        self.assertEqual(self._log()[-1]["outcome"], "allowed-already-blocked")

    def test_stop_hook_active_allows(self):
        out = self._run([user("go"), CONTEXT_CALL, CONTEXT_OUT, COMMIT], stop_hook_active=True)
        self.assertEqual(out, "")
        self.assertEqual(self._log()[-1]["outcome"], "allowed-stop-hook-active")

    def test_subagent_allows(self):
        out = self._run([user("go"), CONTEXT_CALL, CONTEXT_OUT, COMMIT], agent_id="a1")
        self.assertEqual(out, "")

    def test_malformed_transcript_allows(self):
        self.assertEqual(self._run(["{not json", "[]", "null"]), "")

    def test_missing_transcript_allows(self):
        payload = {"session_id": self.session, "transcript_path": str(self.tmp / "none")}
        proc = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                              capture_output=True, text=True, env=self.env)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertEqual(self._log()[-1]["outcome"], "allowed-error")

    def test_garbage_stdin_allows(self):
        proc = subprocess.run([sys.executable, str(SCRIPT)], input="garbage",
                              capture_output=True, text=True, env=self.env)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
