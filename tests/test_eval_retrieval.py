"""Tests for `graph.py eval` (tools/eval_retrieval.py) on a synthetic mini vault.

Ollama is never contacted: every run here is keyword-only except the
unavailable-semantic case, where the embedding call is patched to fail.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_core import GRAPH_PATH, graph, note, write  # noqa: E402

sys.path.insert(0, str(GRAPH_PATH.parent))
import eval_retrieval  # noqa: E402

eval_retrieval.g = graph  # other test modules replace sys.modules["graph"]; bind to ours

QUERIES = [
    {"task": "t1", "ts": "2026-01-01", "query": "login auth", "relevant": ["auth"], "split": "dev"},
    {"task": "t2", "ts": "2026-01-02", "query": "invoice", "relevant": ["billing", "mail"],
     "split": "dev"},
    {"task": "t3", "ts": "2026-01-03", "query": "smtp", "relevant": ["auth"], "split": "holdout"},
    {"task": "t4", "ts": "2026-01-04", "query": "login", "relevant": ["profile"], "split": "dev"},
]


class TestEval(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = self.tmp / "data"
        engine = self.tmp / "engine"
        engine.mkdir()
        kw = lambda *k: f"keywords: [{', '.join(k)}]\n"  # noqa: E731
        write(self.data / "auth.md", note("auth", "Sign-in.", extra=kw("login", "auth")))
        write(self.data / "billing.md", note("billing", "Money.", extra=kw("invoice")))
        write(self.data / "mail.md", note("mail", "Mail.", extra=kw("smtp")))
        write(self.data / "profile.md", note("profile", "Always loaded.", core=True,
                                             extra=kw("login")))
        self.paths = graph.Paths(engine, self.data)
        self.set_path = self.tmp / "set.jsonl"
        self.set_path.write_text("\n".join(json.dumps(q) for q in QUERIES) + "\n", encoding="utf-8")

    def run_eval(self, **over) -> str:
        args = Namespace(set=str(self.set_path), no_semantic=True, k="1,3", split="all",
                         json=True, **over)
        out = StringIO()
        with mock.patch.object(graph, "default_paths", return_value=self.paths), \
                redirect_stdout(out):
            eval_retrieval.cmd_eval(args)
        return out.getvalue()

    def test_metrics_on_known_rankings(self):
        result = json.loads(self.run_eval())
        kw = result["modes"]["keyword"]
        self.assertTrue(kw["available"])
        self.assertEqual(kw["skipped"], 1)  # t4: its only relevant note is core
        s = kw["splits"]["all"]
        self.assertEqual(s["n"], 3)
        # t1: recall 1, rr 1; t2: billing found, mail not: 0.5, rr 1; t3: miss
        self.assertAlmostEqual(s["recall"]["1"], (1 + 0.5 + 0) / 3, places=3)
        self.assertAlmostEqual(s["recall"]["3"], (1 + 0.5 + 0) / 3, places=3)
        self.assertAlmostEqual(s["mrr"], 2 / 3, places=3)

    def test_core_notes_are_not_in_the_ranking(self):
        ranking = eval_retrieval.rank(graph.Graph(self.paths), "login auth", False)
        self.assertEqual(ranking, ["auth"])

    def test_split_filtering(self):
        dev = json.loads(self.run_eval())
        self.assertEqual(list(dev["modes"]["keyword"]["splits"]), ["all"])
        args = Namespace(set=str(self.set_path), no_semantic=True, k="3", split="holdout", json=True)
        out = StringIO()
        with mock.patch.object(graph, "default_paths", return_value=self.paths), \
                redirect_stdout(out):
            eval_retrieval.cmd_eval(args)
        res = json.loads(out.getvalue())["modes"]["keyword"]["splits"]
        self.assertEqual(list(res), ["holdout"])
        self.assertEqual(res["holdout"]["n"], 1)
        self.assertEqual(res["holdout"]["mrr"], 0.0)

    def test_json_shape(self):
        result = json.loads(self.run_eval())
        self.assertEqual(sorted(result), ["k", "modes", "set", "split"])
        self.assertEqual(result["k"], [1, 3])
        self.assertEqual(list(result["modes"]), ["keyword"])  # --no-semantic
        self.assertEqual(sorted(result["modes"]["keyword"]["splits"]["all"]), ["mrr", "n", "recall"])

    def test_no_state_written(self):
        before = sorted(p.relative_to(self.tmp).as_posix() for p in self.tmp.rglob("*"))
        self.run_eval()
        after = sorted(p.relative_to(self.tmp).as_posix() for p in self.tmp.rglob("*"))
        self.assertEqual(before, after)
        self.assertFalse((self.data / ".graph" / "usage.log").exists())

    def test_semantic_unavailable_keeps_keyword_results(self):
        args = Namespace(set=str(self.set_path), no_semantic=False, k="3", split="all", json=True)
        out = StringIO()
        with mock.patch.object(graph, "default_paths", return_value=self.paths), \
                mock.patch.object(graph, "_embed", side_effect=urllib.error.URLError("down")), \
                redirect_stdout(out), redirect_stderr(StringIO()):
            eval_retrieval.cmd_eval(args)
        modes = json.loads(out.getvalue())["modes"]
        self.assertTrue(modes["keyword"]["available"])
        self.assertFalse(modes["semantic"]["available"])
        self.assertTrue(modes["semantic"]["reason"])

    def test_text_output_has_both_columns(self):
        args = Namespace(set=str(self.set_path), no_semantic=False, k="3", split="all", json=False)
        out = StringIO()
        with mock.patch.object(graph, "default_paths", return_value=self.paths), \
                mock.patch.object(graph, "_embed", side_effect=urllib.error.URLError("down")), \
                redirect_stdout(out), redirect_stderr(StringIO()):
            eval_retrieval.cmd_eval(args)
        text = out.getvalue()
        self.assertIn("keyword", text)
        self.assertIn("semantic", text)
        self.assertIn("recall@3", text)
        self.assertIn("unavailable", text)

    def test_bad_set_line_exits(self):
        self.set_path.write_text('{"query": "x"}\n', encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.run_eval()


if __name__ == "__main__":
    unittest.main()
