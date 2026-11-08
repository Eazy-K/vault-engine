"""stats: two-channel (Claude / Codex) view from usage.log events."""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
import _isolation  # noqa: E402,F401
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
_spec = importlib.util.spec_from_file_location("graph", REPO_ROOT / "tools" / "graph.py")
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)


def cx(i, c, o, turns, profile="worker-low", r=5):
    return {"event": "codex_exec", "input_tokens": i, "cached_input_tokens": c,
            "output_tokens": o, "reasoning_output_tokens": r, "turns": turns,
            "exit_code": 0, "profile": profile, "agent_type": "codex-worker"}


CLAUDE = [{"event": "subagent_stop", "agent": "claude", "turns": 10, "tokens": 1000},
          {"event": "subagent_stop", "agent": "claude", "turns": 5, "tokens": 500},
          {"event": "subagent_stop", "agent": "claude", "internal": True, "turns": 9, "tokens": 9},
          {"event": "context", "task": "t", "notes": []}]
CODEX = [cx(100, 60, 10, 3), cx(200, 100, 20, 4, "worker-medium"),
         {"event": "codex_exec", "events_missing": True, "input_tokens": None,
          "cached_input_tokens": None, "output_tokens": None, "reasoning_output_tokens": None,
          "turns": None, "exit_code": 1, "profile": "worker-low"}]


class TestStatsChannels(unittest.TestCase):
    def test_sums_and_missing(self):
        ch = graph.channel_stats(CLAUDE + CODEX)
        x = ch["codex"]
        self.assertEqual((x["runs"], x["runs_missing_events"]), (3, 1))
        self.assertEqual((x["input"], x["cached"], x["output"], x["turns"]), (300, 160, 30, 7))
        self.assertEqual(x["by_profile"]["worker-low"]["runs"], 2)
        self.assertEqual(x["by_profile"]["worker-low"]["runs_missing_events"], 1)
        self.assertEqual(x["by_profile"]["worker-medium"]["input"], 200)
        c = ch["claude"]
        self.assertEqual((c["runs"], c["turns"], c["tokens"]), (2, 15, 1500))

    def test_no_codex_events(self):
        ch = graph.channel_stats(CLAUDE)
        self.assertEqual(ch["codex"]["runs"], 0)
        self.assertEqual(ch["codex"]["by_profile"], {})
        self.assertTrue(graph.format_channel_stats(ch))

    def _stats(self, events, **kw):
        out = StringIO()
        with mock.patch.object(graph, "read_usage", return_value=events), \
             mock.patch.object(graph, "default_paths", return_value=None), \
             redirect_stdout(out):
            graph.cmd_stats(Namespace(no_transcripts=True, **kw))
        return out.getvalue()

    def test_text_and_json(self):
        out = self._stats(CLAUDE + CODEX)
        self.assertIn("channels:", out)
        self.assertIn("codex runs with missing events: 1", out)
        data = json.loads(self._stats(CLAUDE + CODEX, json=True))
        self.assertEqual(set(data["channels"]), {"claude", "codex"})
        self.assertEqual(data["channels"]["codex"]["input"], 300)
        data = json.loads(self._stats(CLAUDE, json=True))
        self.assertEqual(data["channels"]["codex"]["runs"], 0)


if __name__ == "__main__":
    unittest.main()
