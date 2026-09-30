"""Repeated Codex PostToolUse nudges and usage-log coverage."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "codex-hooks" / "delegation-warn.py"


class TestCodexDelegationWarn(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.session = f"session-{id(self)}"
        self.env = dict(os.environ)
        self.env["CODEX_DELEGATION_WARN_STATE_DIR"] = str(self.tmp / "state")
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.env["VAULT_DATA"] = str(self.data)

    def _run(self, turn_id="turn-1", tool_name="Bash", agent_id=None):
        payload = {"session_id": self.session, "turn_id": turn_id,
                   "tool_name": tool_name, "tool_input": {}}
        if agent_id:
            payload["agent_id"] = agent_id
        return subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                              capture_output=True, text=True, env=self.env)

    def test_warns_at_four_and_then_every_two_calls_with_stronger_language(self):
        outputs = [self._run() for _ in range(8)]
        self.assertEqual([i + 1 for i, out in enumerate(outputs) if out.stdout.strip()],
                         [4, 6, 8])
        first = json.loads(outputs[3].stdout)["systemMessage"]
        repeated = json.loads(outputs[5].stdout)["systemMessage"]
        self.assertIn("4 tool calls", first)
        self.assertIn("stop and delegate", repeated)

    def test_new_turn_resets_count_and_logs_previous_turn_as_codex(self):
        for _ in range(4):
            self._run()
        self.assertFalse((self.data / ".graph" / "usage.log").exists())
        out = self._run(turn_id="turn-2")
        self.assertEqual(out.stdout.strip(), "")
        events = [json.loads(line) for line in
                  (self.data / ".graph" / "usage.log").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "orchestrator_prompt")
        self.assertEqual(events[0]["agent"], "codex")
        self.assertEqual(events[0]["inline_calls"], 4)
        self.assertTrue(events[0]["warned"])

    def test_subagent_and_unmatched_tool_calls_are_ignored(self):
        for _ in range(5):
            self.assertEqual(self._run(agent_id="worker").stdout.strip(), "")
            self.assertEqual(self._run(tool_name="Other").stdout.strip(), "")
        self.assertFalse((self.data / ".graph" / "usage.log").exists())


if __name__ == "__main__":
    unittest.main()
