"""Tests for tools/claude-hooks/subagent-log.py (SubagentStop measurement hook)."""
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
SCRIPT = REPO_ROOT / "tools" / "claude-hooks" / "subagent-log.py"


class TestSubagentLog(unittest.TestCase):
    AGENT = "claude"
    PRINTS_JSON = False

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.data = self.tmp / "data"
        self.data.mkdir()

    def _run(self, stdin, data=True):
        env = dict(os.environ)
        env.pop("VAULT_HOME", None)
        env.pop("VAULT_DATA", None)
        if data:
            env["VAULT_DATA"] = str(self.data)
        return subprocess.run([sys.executable, str(SCRIPT)], input=stdin,
                              capture_output=True, text=True, env=env)

    def _events(self):
        log = self.data / ".graph" / "usage.log"
        return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] \
            if log.exists() else []

    def test_logs_documented_fields_only(self):
        payload = {"session_id": "s1", "hook_event_name": "SubagentStop",
                   "stop_hook_active": False, "agent_id": "def456", "agent_type": "Explore",
                   "agent_transcript_path": "/x/agent.jsonl",
                   "last_assistant_message": "Analysis complete."}
        out = self._run(json.dumps(payload))
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "{}" if self.PRINTS_JSON else "")
        (event,) = self._events()
        self.assertEqual(event["event"], "subagent_stop")
        self.assertEqual(event["agent"], self.AGENT)
        self.assertEqual(event["session"], "s1")
        self.assertEqual(event["agent_type"], "Explore")
        self.assertEqual(event["agent_id"], "def456")
        self.assertIs(event["stop_hook_active"], False)
        self.assertEqual(event["last_message_chars"], len("Analysis complete."))
        self.assertNotIn("Analysis", json.dumps(event))  # message text is never logged
        self.assertNotIn("agent_transcript_path", event)

    def test_missing_optional_fields_are_omitted(self):
        self._run(json.dumps({"session_id": "s1"}))
        (event,) = self._events()
        for key in ("agent_type", "agent_id", "stop_hook_active", "last_message_chars"):
            self.assertNotIn(key, event)

    def test_no_data_dir_writes_nothing(self):
        out = self._run(json.dumps({"session_id": "s1"}), data=False)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(self._events(), [])

    def test_garbage_input_fails_open(self):
        for stdin in ("", "not json {{", "[1, 2]"):
            out = self._run(stdin)
            self.assertEqual(out.returncode, 0)
            self.assertEqual(out.stderr.strip(), "")
        self.assertEqual(self._events(), [])


if __name__ == "__main__":
    unittest.main()
