"""`context` total output cap: CONTEXT_OUTPUT_CAP bytes, drop order, core notes, log."""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from argparse import Namespace
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

for _var in ("VAULT_DATA", "VAULT_HOME"):
    os.environ.pop(_var, None)


def _no_registry(*_args):
    raise OSError("tests never read the real registry")


sys.modules["winreg"] = types.SimpleNamespace(HKEY_CURRENT_USER=None, OpenKey=_no_registry)

GRAPH_PATH = Path(__file__).resolve().parent.parent / "tools" / "graph.py"
_spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)

BIG_BUDGET = 1_000_000
TURKISH = "çğış öü şu türkçe.\n"


def filler(size: int, unit: str = "filler words here.\n") -> str:
    return unit * (size // len(unit))


def nbytes(text: str) -> int:
    return len(text.encode("utf-8"))


class _Ctx(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engine, self.data = self.tmp / "engine", self.tmp / "data"
        self.engine.mkdir()
        self.data.mkdir()
        self.paths = graph.Paths(self.engine, self.data)

    def note(self, nid: str, body: str, core: bool = False) -> None:
        path = self.data / f"{nid}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        front = "---\ncore: true\n---\n" if core else ""
        path.write_text(f"{front}# {nid}\n\n{body}\n", encoding="utf-8")

    def run_context(self, **overrides) -> str:
        args = Namespace(text="", seed=[], threshold=graph.DEFAULT_THRESHOLD,
                         depth=graph.DEFAULT_DEPTH, json=False, no_semantic=True,
                         project=None, no_project=True, core=False,
                         budget=BIG_BUDGET, no_log=True)
        for k, v in overrides.items():
            setattr(args, k, v)
        out = StringIO()
        with mock.patch.object(graph, "default_paths", return_value=self.paths), \
             mock.patch("pathlib.Path.home", return_value=self.tmp / "home"), \
             redirect_stdout(out), redirect_stderr(StringIO()):
            graph.cmd_query(args, content=True)
        return out.getvalue()

    def big_vault(self, unit: str = "filler words here.\n") -> None:
        """core ~5K, two entry notes ~10K each, three task notes ~8K each: ~49K."""
        self.note("core-a", filler(5000, unit), core=True)
        self.note("projects/p/p-status", filler(10000, unit))
        self.note("projects/p/p-overview", filler(10000, unit))
        for n in ("t1", "t2", "t3"):
            self.note(n, filler(8000, unit))

    def run_big(self, **kw) -> str:
        return self.run_context(seed=["t1", "t2", "t3"], project="p", no_project=False, **kw)


class TestCap(_Ctx):
    def test_constant(self):
        self.assertEqual(graph.CONTEXT_OUTPUT_CAP, 28000)

    def test_over_cap_output_fits_and_names_omitted(self):
        self.big_vault()
        with mock.patch.object(graph, "CONTEXT_OUTPUT_CAP", 10 ** 7):
            full = self.run_big()
        self.assertGreater(nbytes(full), 38000)
        out = self.run_big()
        self.assertLessEqual(nbytes(out), 28000)
        self.assertRegex(out, r"<!-- omitted over output cap \(\d+ bytes > 28000\): [^>]*t[123]")
        self.assertIn(f'python "{GRAPH_PATH}" show --body <id>', out)
        self.assertIn("<!-- note: core-a ", out)

    def test_under_cap_output_is_unchanged(self):
        self.note("core-a", "core rule", core=True)
        self.note("t1", "short task note")
        normal = self.run_context(seed=["t1"])
        with mock.patch.object(graph, "CONTEXT_OUTPUT_CAP", 280000):
            bigger = self.run_context(seed=["t1"])
        self.assertEqual(normal, bigger)
        self.assertNotIn("output cap", normal)

    def test_tenfold_cap_leaves_big_output_whole(self):
        self.big_vault()
        with mock.patch.object(graph, "CONTEXT_OUTPUT_CAP", 280000):
            a = self.run_big()
        with mock.patch.object(graph, "CONTEXT_OUTPUT_CAP", 2800000):
            b = self.run_big()
        self.assertEqual(a, b)
        self.assertNotIn("output cap", a)

    def test_counts_utf8_bytes_not_characters(self):
        self.note("core-a", "x", core=True)
        self.note("t1", TURKISH * 700)   # ~13K characters, ~20K bytes
        self.note("t2", TURKISH * 700)
        with mock.patch.object(graph, "CONTEXT_OUTPUT_CAP", 10 ** 7):
            uncapped = self.run_context(seed=["t1", "t2"])
        self.assertLess(len(uncapped), 28000)
        self.assertGreater(nbytes(uncapped), 28000)
        out = self.run_context(seed=["t1", "t2"])
        self.assertLessEqual(nbytes(out), 28000)
        self.assertIn("omitted over output cap", out)

    def test_fixed_lines_stay_when_cap_is_hit(self):
        self.big_vault()
        out = self.run_big(no_log=False)
        self.assertIn("omitted over output cap", out)
        self.assertTrue(out.startswith("<!-- retrieval:"))
        self.assertIn("reinforce --task ", out.splitlines()[-1])


class TestOrderAndCore(_Ctx):
    def block(self, nid, kind, activation, size=1000):
        return {"id": nid, "kind": kind, "activation": activation,
                "text": f"<!-- note: {nid} -->\n" + "x" * size + "\n"}

    def test_drop_order_tasks_then_entries_lowest_activation_first(self):
        blocks = [self.block("entry-hi", "entry", 0.9), self.block("task-hi", "task", 0.8),
                  self.block("entry-lo", "entry", 0.2), self.block("task-lo", "task", 0.1),
                  self.block("core", "core", 0.5)]
        kept, removed, _line, warn = graph.apply_output_cap(
            blocks, ["head"], [], "load", False, ["tail"], cap=3300)
        self.assertEqual([b["id"] for b in removed], ["task-lo", "task-hi"])
        self.assertEqual([b["id"] for b in kept], ["entry-hi", "entry-lo", "core"])
        self.assertEqual(warn, "")
        kept, removed, _line, _warn = graph.apply_output_cap(
            blocks, ["head"], [], "load", False, ["tail"], cap=2200)
        self.assertEqual([b["id"] for b in removed],
                         ["task-lo", "task-hi", "entry-lo", "entry-hi"][:len(removed)])
        self.assertGreaterEqual(len(removed), 3)
        self.assertIn("core", [b["id"] for b in kept])

    def test_removal_line_length_is_counted(self):
        blocks = [self.block(f"task-{i}", "task", i / 10) for i in range(5)]
        cap = 1000 * 5 + 200
        kept, _removed, line, _warn = graph.apply_output_cap(
            blocks, [], [], "load", False, [], cap=cap)
        total = sum(nbytes(b["text"]) + 1 for b in kept) + nbytes(line) + 1 + nbytes("load") + 1
        self.assertLessEqual(total, cap)

    def test_core_notes_are_never_dropped(self):
        self.note("core-a", filler(20000), core=True)
        self.note("core-b", filler(20000), core=True)
        self.note("projects/p/p-status", filler(3000))
        self.note("t1", filler(3000))
        out = self.run_context(seed=["t1"], project="p", no_project=False)
        self.assertIn("<!-- note: core-a ", out)
        self.assertIn("<!-- note: core-b ", out)
        self.assertNotIn("<!-- note: t1 ", out)
        self.assertNotIn("<!-- note: projects/p/p-status ", out)
        self.assertGreater(nbytes(out), 28000)
        self.assertRegex(out, r"<!-- WARN: core notes alone exceed output cap "
                              r"\(\d+ bytes > 28000\); shorten core notes -->")
        omitted_line = [x for x in out.splitlines() if "omitted over output cap" in x][0]
        self.assertIn("t1", omitted_line)
        self.assertEqual(out.count(filler(20000).rstrip()), 2)

    def test_no_warn_when_removal_is_enough(self):
        self.big_vault()
        self.assertNotIn("WARN", self.run_big())


class TestLog(_Ctx):
    def test_removed_notes_move_from_notes_to_omitted(self):
        self.big_vault()
        self.run_big(no_log=False)
        ev = [e for e in graph.read_usage(self.paths) if e.get("event") == "context"][-1]
        dropped = set(ev["omitted"])
        self.assertTrue(dropped)
        self.assertTrue(dropped <= {"t1", "t2", "t3", "projects/p/p-status",
                                    "projects/p/p-overview"})
        self.assertFalse(dropped & set(ev["notes"]))
        self.assertEqual(ev["core"], ["core-a"])
        self.assertTrue(dropped <= set(ev["scores"]))


if __name__ == "__main__":
    unittest.main()
