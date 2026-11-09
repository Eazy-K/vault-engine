"""Tests that tools/claude-hooks/reinforce-check.py counts only task ids printed by
this session's own `context` call (Bash tool_use linked by tool_use_id), not ids in
Read / Grep / Agent results. Helpers are copied from test_reinforce_check.py.
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


def tool(name, tid):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": name, "input": {}}]}}


def result(text, tid="t1"):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text}]}}


def hint(task):
    return f'<!-- when done: python "/g/graph.py" reinforce --task {task} -->'


CONTEXT_CALL = bash('python tools/graph.py context "x"', "c1")
CONTEXT_OUT = result("notes...\n" + hint("abc123"), "c1")
COMMIT = bash('git commit -m "x"', "g1")


class TestOwnSessionIds(unittest.TestCase):
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

    def test_foreign_id_in_other_tool_results_is_ignored(self):
        for name in ("Read", "Grep", "Agent"):
            with self.subTest(tool=name):
                entries = [user("go"), tool(name, "x1"),
                           result("seen " + hint("68f7fc"), "x1"), COMMIT]
                self.assertEqual(self._run(entries), "")
                self.assertEqual(self._log()[-1]["outcome"], "allowed-no-context")

    def test_foreign_id_in_non_context_bash_is_ignored(self):
        entries = [user("go"), bash("cat notes.txt", "b1"),
                   result(hint("68f7fc"), "b1"), COMMIT]
        self.assertEqual(self._run(entries), "")
        self.assertEqual(self._log()[-1]["outcome"], "allowed-no-context")

    def test_only_own_id_requested(self):
        entries = [user("go"), tool("Read", "x1"), result(hint("68f7fc"), "x1"),
                   CONTEXT_CALL, CONTEXT_OUT, COMMIT]
        out = json.loads(self._run(entries))
        self.assertEqual(out["decision"], "block")
        self.assertIn("abc123", out["reason"])
        self.assertNotIn("68f7fc", out["reason"])
        self.assertEqual(self._log()[-1]["task"], "abc123")

    def test_foreign_id_not_in_outcome_reminder(self):
        reinforce = bash('python "/g/graph.py" reinforce --task abc123 note-a', "r1")
        entries = [user("go"), tool("Agent", "x1"), result(hint("68f7fc"), "x1"),
                   CONTEXT_CALL, CONTEXT_OUT, reinforce, COMMIT]
        out = json.loads(self._run(entries))
        self.assertEqual(out["decision"], "block")
        self.assertIn("abc123", out["reason"])
        self.assertNotIn("68f7fc", out["reason"])
        self.assertEqual(self._log()[-1]["outcome"], "blocked-no-outcome")

    def test_foreign_id_not_in_outcome_system_message(self):
        reinforce = bash('python "/g/graph.py" reinforce --task abc123 note-a', "r1")
        entries = [user("go"), tool("Read", "x1"), result(hint("68f7fc"), "x1"),
                   CONTEXT_CALL, CONTEXT_OUT, reinforce]
        out = json.loads(self._run(entries))
        self.assertIn("abc123", out["systemMessage"])
        self.assertNotIn("68f7fc", out["systemMessage"])
        self.assertEqual(self._log()[-1]["outcome"], "allowed-reinforced-no-outcome")


if __name__ == "__main__":
    unittest.main()
