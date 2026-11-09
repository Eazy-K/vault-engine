"""stats --retrieval: keyword_fallback breaks down by fallback reason."""
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


def ctx(reason, mode="keyword", **extra):
    e = {"ts": "2026-09-01T10:00:00", "event": "context", "retrieval": mode}
    if reason is not None:
        e["fallback_reason"] = reason
    e.update(extra)
    return e


EVENTS = [
    ctx("", mode="semantic"),
    ctx("ollama unreachable"), ctx("ollama unreachable"),
    ctx("ollama error, HTTP 400"),
    ctx("model missing"),
    ctx("embedding failed"),
    ctx("no query text"),
    ctx("disabled by --no-semantic"),
    ctx(None),  # old event: retrieval present, no fallback_reason
    ctx(""),
]


class TestReasonStats(unittest.TestCase):
    def test_by_reason(self):
        k = graph.retrieval_stats(EVENTS)["keyword_fallback"]
        self.assertEqual(k["by_reason"], {
            "ollama unreachable": 2, "disabled by --no-semantic": 1, "embedding failed": 1,
            "model missing": 1, "no query text": 1, "ollama error, HTTP 400": 1,
            "unspecified": 2})
        self.assertEqual(sum(k["by_reason"].values()), k["count"])
        self.assertEqual(k["by_reason"]["unspecified"], k["unspecified"])

    def test_empty(self):
        r = graph.retrieval_stats([])
        self.assertEqual(r["keyword_fallback"]["by_reason"], {})
        self.assertIn("by reason:        -", graph.format_retrieval_stats(r))

    def test_format_line(self):
        out = graph.format_retrieval_stats(graph.retrieval_stats(EVENTS))
        self.assertIn("by reason:        ollama unreachable 2,", out)
        self.assertIn("unspecified 2", out)


if __name__ == "__main__":
    unittest.main()
