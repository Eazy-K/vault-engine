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


def stop(role, turns, tokens, hit="none"):
    return {"event": "subagent_stop", "agent_type": role, "turns": turns,
            "max_turns": 60, "tokens": tokens, "budget_hit": hit}


EVENTS = [
    stop("worker-medium", 10, 100000), stop("worker-medium", 20, 200000, "warn"),
    stop("worker-medium", 61, 260000, "block"),
    stop("worker-low", 5, 20000),
    # older event: budget_class still counts under its agent_type
    {"event": "subagent_stop", "agent_type": "worker-low", "budget_class": "S", "turns": 7,
     "tokens": 30000, "budget_hit": "none"},
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
        self.assertIn("  worker-medium: n=3, turns p50/p90 20/52.8, tokens p50/p90 200000/248000, "
                      "warn 1, hit 1", out)
        self.assertIn("  worker-low: n=2, turns p50/p90 6/6.8, tokens p50/p90 25000/29000, "
                      "warn 0, hit 0", out)

    def test_json_key(self):
        data = json.loads(self._stats(json=True))
        budgets = data["subagent_budgets"]
        self.assertEqual(sorted(budgets), ["worker-low", "worker-medium"])
        self.assertEqual(budgets["worker-medium"]["n"], 3)
        self.assertEqual(budgets["worker-medium"]["turns_p50"], 20)
        self.assertEqual(budgets["worker-medium"]["hit"], 1)
        self.assertEqual(budgets["worker-medium"]["warn"], 1)
        self.assertEqual(budgets["worker-low"]["tokens_p90"], 29000)

    def test_no_events_no_section(self):
        with mock.patch.object(graph, "read_usage", return_value=[]), \
             mock.patch.object(graph, "default_paths", return_value=None), \
             redirect_stdout(StringIO()) as out:
            graph.cmd_stats(Namespace(no_transcripts=True))
        self.assertNotIn("subagent budgets", out.getvalue())

    def test_resumed_worker_counts_once(self):
        w = {"agent_id": "a1", "session": "s"}
        m = "worker-medium"
        events = [dict(stop(m, 50, 1, "warn"), **w), dict(stop(m, 61, 2, "block"), **w),
                  dict(stop(m, 62, 3, "block"), stop_hook_active="1", **w),
                  dict(stop(m, 10, 4, "block"), session="t"),
                  dict(stop(m, 11, 5, "block"), session="t"),
                  dict(stop(m, 50, 6, "warn"), agent_id="a2")]
        r = graph.subagent_budget_stats(events)[m]
        self.assertEqual((r["n"], r["hit"], r["warn"]), (6, 2, 1))


if __name__ == "__main__":
    unittest.main()
