"""stats --retrieval: metrics over a synthetic usage.log with old and new events."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
import _isolation  # noqa: E402,F401
REPO_ROOT = TESTS_DIR.parent
_spec = importlib.util.spec_from_file_location("graph", REPO_ROOT / "tools" / "graph.py")
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)

EVENTS = [
    # new-style context, closed: loaded a,b; omitted c,d; core k
    {"ts": "2026-09-01T10:00:00", "event": "context", "task": "t1", "notes": ["a", "b"],
     "core": ["k"], "omitted": ["c", "d"], "retrieval": "semantic", "fallback_reason": "",
     "scores": {"a": [1, 0.9, None, 0.5], "b": [2, 0.8, None, 0.1],
                "c": [3, 0.5, None, 0.0], "d": [4, 0.4, None, 0.0]}},
    {"ts": "2026-09-01T10:05:00", "event": "reinforce", "task": "t1", "notes": ["a", "k", "d"]},
    {"ts": "2026-09-01T10:02:00", "event": "show", "task": "t1", "note": "d", "from_omitted": True},
    {"ts": "2026-09-01T10:03:00", "event": "show", "task": "t1", "note": "b", "from_omitted": False},
    # keyword fallback, deliberate, closed with empty reinforce
    {"ts": "2026-09-02T10:00:00", "event": "context", "task": "t2", "notes": ["x", "y"],
     "core": [], "omitted": [], "retrieval": "keyword", "fallback_reason": "no query text"},
    {"ts": "2026-09-02T10:05:00", "event": "reinforce", "task": "t2", "notes": []},
    # keyword fallback, failure, never closed
    {"ts": "2026-09-03T10:00:00", "event": "context", "task": "t3", "notes": ["p"],
     "retrieval": "keyword", "fallback_reason": "ollama unreachable"},
    # old event: no retrieval/core/scores, closed
    {"ts": "2026-08-01T10:00:00", "event": "context", "task": "t4", "notes": ["m", "n"]},
    {"ts": "2026-08-01T10:05:00", "event": "reinforce", "task": "t4", "notes": ["m"]},
    {"ts": "2026-08-01T10:06:00", "event": "show"},
]


class TestRetrievalStats(unittest.TestCase):
    def test_metrics(self):
        r = graph.retrieval_stats(EVENTS)
        # t1: 1/2, t2: 0 reinforced of 2 loaded -> skipped when none reinforced? no: 0/2
        self.assertEqual(r["precision"]["n"], 3)
        self.assertAlmostEqual(r["precision"]["macro"], round((0.5 + 0 + 0.5) / 3, 3))
        self.assertAlmostEqual(r["precision"]["micro"], round(2 / 6, 3))
        # coverage needs core: t1 (wanted a,d -> 1/2), t2 has no wanted notes
        self.assertEqual(r["coverage"]["n"], 1)
        self.assertEqual(r["coverage"]["macro"], 0.5)
        om = r["reinforced_from_omitted"]
        self.assertEqual((om["count"], om["n"], om["median_rank"]), (1, 2, 4))
        self.assertEqual(r["show"]["n"], 2)
        self.assertEqual(r["show"]["from_omitted"], 0.5)
        self.assertEqual(r["show"]["later_reinforced"], 0.5)
        k = r["keyword_fallback"]
        self.assertEqual((k["n"], k["count"], k["deliberate"], k["failure"]), (3, 2, 1, 1))
        self.assertEqual(r["learning"]["notes"], 2)
        self.assertEqual(r["learning"]["mean_learned_share"], 0.3)
        cl = r["closure"]
        self.assertEqual((cl["contexts"], cl["closed"], cl["empty_reinforces"]), (4, 3, 1))

    def test_date_filter_and_old_events(self):
        r = graph.retrieval_stats(EVENTS, since="2026-08-15")
        self.assertEqual(r["closure"]["contexts"], 3)
        r = graph.retrieval_stats(EVENTS, until="2026-08-31")
        self.assertEqual(r["precision"]["n"], 1)
        self.assertEqual(r["coverage"]["n"], 0)  # old event has no core field
        self.assertIsNone(r["keyword_fallback"]["rate"])
        self.assertIn("n=0", graph.format_retrieval_stats(r))

    def test_empty_log(self):
        text = graph.format_retrieval_stats(graph.retrieval_stats([]))
        self.assertIn("closure:", text)


if __name__ == "__main__":
    unittest.main()
