"""stats: "subagent budgets" section from subagent_stop events with budget fields."""
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


def stop(cls, turns, tokens, hit="none"):
    return {"event": "subagent_stop", "budget_class": cls, "turns": turns,
            "tokens": tokens, "budget_hit": hit}


EVENTS = [
    stop("M", 10, 100000), stop("M", 20, 200000, "warn"), stop("M", 61, 260000, "block"),
    stop("S", 5, 20000),
    {"event": "subagent_stop", "agent_type": "Explore"},  # no budget fields: ignored
    {"event": "context", "task": "t1", "notes": []},
]


class TestStatsBudgets(unittest.TestCase):
    def _stats(self, **kw) -> str:
        out = StringIO()
        with mock.patch.object(graph, "read_usage", return_value=EVENTS), \
             mock.patch.object(graph, "default_paths", return_value=None), \
             redirect_stdout(out):
            graph.cmd_stats(Namespace(no_transcripts=True, **kw))
        return out.getvalue()

    def test_text_section(self):
        out = self._stats()
        self.assertIn("subagent budgets:", out)
        self.assertIn("  M: n=3, turns p50/p90 20/52.8, tokens p50/p90 200000/248000, "
                      "warn 1, hit 1", out)
        self.assertIn("  S: n=1, turns p50/p90 5/5, tokens p50/p90 20000/20000, "
                      "warn 0, hit 0", out)

    def test_json_key(self):
        data = json.loads(self._stats(json=True))
        budgets = data["subagent_budgets"]
        self.assertEqual(sorted(budgets), ["M", "S"])
        self.assertEqual(budgets["M"]["n"], 3)
        self.assertEqual(budgets["M"]["turns_p50"], 20)
        self.assertEqual(budgets["M"]["hit"], 1)
        self.assertEqual(budgets["M"]["warn"], 1)
        self.assertEqual(budgets["S"]["tokens_p90"], 20000)

    def test_no_events_no_section(self):
        with mock.patch.object(graph, "read_usage", return_value=[]), \
             mock.patch.object(graph, "default_paths", return_value=None), \
             redirect_stdout(StringIO()) as out:
            graph.cmd_stats(Namespace(no_transcripts=True))
        self.assertNotIn("subagent budgets", out.getvalue())


if __name__ == "__main__":
    unittest.main()
