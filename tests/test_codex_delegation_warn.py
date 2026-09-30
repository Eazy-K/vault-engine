"""Repeated Codex PostToolUse nudges and usage-log coverage."""
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
        if getattr(self, "transcript", None):
            payload["transcript_path"] = str(self.transcript)
        return subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                              capture_output=True, text=True, env=self.env)

    def test_code_mode_inner_tool_names_count_but_outer_exec_does_not(self):
        outs = [self._run(turn_id="t2", tool_name="exec") for _ in range(6)]
        self.assertTrue(all(not o.stdout.strip() for o in outs))
        outs = [self._run(turn_id="t3", tool_name="exec_command") for _ in range(4)]
        self.assertTrue(outs[3].stdout.strip())

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

    def _rollout(self, payload):
        self.transcript = self.tmp / "rollout.jsonl"
        self.transcript.write_text(json.dumps({"type": "session_meta", "payload": payload})
                                   + "\n", encoding="utf-8")

    def test_subagent_rollout_is_ignored_and_not_logged(self):
        for meta in ({"source": {"subagent": {"thread_spawn": {"parent_thread_id": "p"}}}},
                     {"parent_thread_id": "p"}):
            with self.subTest(meta=meta):
                self._rollout(meta)
                for turn in ("a", "b"):
                    for _ in range(5):
                        self.assertEqual(self._run(turn_id=turn).stdout.strip(), "")
                self.assertFalse((self.data / ".graph" / "usage.log").exists())

    def test_main_rollout_still_warns_and_missing_transcript_fails_open(self):
        self._rollout({"source": "cli"})
        self.assertTrue(any(self._run().stdout.strip() for _ in range(4)))
        self.transcript = self.tmp / "missing.jsonl"
        self.session += "-2"
        self.assertTrue(any(self._run().stdout.strip() for _ in range(4)))

    def test_subagent_and_unmatched_tool_calls_are_ignored(self):
        for _ in range(5):
            self.assertEqual(self._run(agent_id="worker").stdout.strip(), "")
            self.assertEqual(self._run(tool_name="Other").stdout.strip(), "")
        self.assertFalse((self.data / ".graph" / "usage.log").exists())


if __name__ == "__main__":
    unittest.main()
