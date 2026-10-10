"""`context`: retrieval-mode header and log, section-aware truncation, load hints."""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
import urllib.error
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


def section(name: str, size: int = 300) -> str:
    return f"## {name}\n" + ("filler words here.\n" * (size // 19)) + "\n"


class _Ctx(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engine, self.data = self.tmp / "engine", self.tmp / "data"
        self.engine.mkdir()
        self.data.mkdir()
        self.paths = graph.Paths(self.engine, self.data)

    def note(self, nid: str, body: str) -> None:
        path = self.data / f"{nid}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {nid}\n\n{body}\n", encoding="utf-8")

    def run_context(self, **overrides) -> str:
        args = Namespace(text="", seed=[], threshold=graph.DEFAULT_THRESHOLD,
                         depth=graph.DEFAULT_DEPTH, json=False, no_semantic=True,
                         project=None, no_project=True, core=False,
                         budget=graph.DEFAULT_BUDGET, no_log=True)
        for k, v in overrides.items():
            setattr(args, k, v)
        out = StringIO()
        with mock.patch.object(graph, "default_paths", return_value=self.paths), \
             mock.patch("pathlib.Path.home", return_value=self.tmp / "home"), \
             redirect_stdout(out), redirect_stderr(StringIO()):
            graph.cmd_query(args, content=True)
        return out.getvalue()

    def stats(self) -> str:
        out = StringIO()
        with mock.patch.object(graph, "default_paths", return_value=self.paths), \
             redirect_stdout(out):
            graph.cmd_stats(Namespace())
        return out.getvalue()


class TestRetrievalHeader(_Ctx):
    def setUp(self):
        super().setUp()
        self.note("a", "Alpha note.")

    def test_no_semantic_flag_is_reported(self):
        out = self.run_context(text="alpha")
        self.assertEqual(out.splitlines()[0],
                         "<!-- retrieval: keyword-only (disabled by --no-semantic) -->")

    def test_unreachable_ollama(self):
        with mock.patch.object(graph, "refresh_embeddings",
                               side_effect=urllib.error.URLError("refused")):
            out = self.run_context(text="alpha", no_semantic=False)
        self.assertEqual(out.splitlines()[0],
                         "<!-- retrieval: keyword-only (ollama unreachable) -->")

    def test_missing_model(self):
        with mock.patch.object(graph, "refresh_embeddings",
                               side_effect=ValueError(f"Ollama has no {graph.EMBED_MODEL} model")):
            out = self.run_context(text="alpha", no_semantic=False)
        self.assertIn("keyword-only (model missing)", out.splitlines()[0])

    def test_semantic_mode(self):
        with mock.patch.object(graph, "semantic_scores", return_value={"a": 1.0}):
            out = self.run_context(text="alpha", no_semantic=False)
        self.assertEqual(out.splitlines()[0],
                         f"<!-- retrieval: semantic ({graph.EMBED_MODEL}) + keyword -->")

    def test_mode_is_logged_and_shown_by_stats(self):
        self.run_context(seed=["a"], no_log=False)
        self.assertEqual(graph.read_usage(self.paths)[-1]["retrieval"], "keyword")
        graph.log_usage(self.paths, {"event": "context", "task": "old", "notes": []})
        self.assertIn("keyword-only:        1/1 (100%)", self.stats())

    def test_stats_ignores_logs_without_the_field(self):
        graph.log_usage(self.paths, {"event": "context", "task": "old", "notes": []})
        self.assertNotIn("keyword-only", self.stats())

    def test_orchestrator_prompt_summary(self):
        for n, warned in ((1, False), (2, False), (5, True), (9, True)):
            graph.log_usage(self.paths, {"event": "orchestrator_prompt", "inline_calls": n,
                                         "warned": warned, "threshold": 3})
        self.assertIn("orchestrator prompts: 4 measured, 2 over 3 inline calls (50%), "
                      "median inline calls 3.5", self.stats())

    def test_orchestrator_prompts_are_split_by_agent(self):
        graph.log_usage(self.paths, {"event": "orchestrator_prompt", "agent": "claude",
                                     "inline_calls": 2, "warned": False, "threshold": 3})
        graph.log_usage(self.paths, {"event": "orchestrator_prompt", "agent": "codex",
                                     "inline_calls": 6, "warned": True, "threshold": 4})
        out = self.stats()
        self.assertIn("  claude: 1 measured, 0 over 3 inline calls", out)
        self.assertIn("  codex: 1 measured, 1 over 4 inline calls", out)

    def test_legacy_orchestrator_prompt_without_agent_is_unknown(self):
        self.paths.usage_log.parent.mkdir(parents=True, exist_ok=True)
        self.paths.usage_log.write_text(
            '{"event":"orchestrator_prompt","inline_calls":2,'
            '"warned":false,"threshold":4}\n',
            encoding="utf-8",
        )
        self.assertIn("  unknown: 1 measured, 0 over 4 inline calls (0%), "
                      "median inline calls 2", self.stats())

    def test_no_orchestrator_section_without_events(self):
        graph.log_usage(self.paths, {"event": "context", "task": "old", "notes": []})
        self.assertNotIn("orchestrator prompts", self.stats())


class TestTruncation(unittest.TestCase):
    def test_cuts_at_section_boundary_and_lists_cut_sections(self):
        body = "intro\n\n" + section("One") + section("Two") + section("Three")
        out = graph.truncate_body(body, 700)
        self.assertIn("## One", out)
        self.assertNotIn("## Three", out)
        self.assertRegex(out, r"<!-- truncated; cut sections: (Two, )?Three -->$")
        self.assertLessEqual(len(out), 700)

    def test_falls_back_to_line_cut_when_first_section_does_not_fit(self):
        body = "".join(f"line {i}\n" for i in range(200)) + "## Later\ntext\n"
        out = graph.truncate_body(body, 400)
        self.assertLessEqual(len(out), 400)
        self.assertTrue(out.endswith("<!-- truncated; cut sections: Later -->"))

    def test_plain_marker_without_sections(self):
        out = graph.truncate_body("word\n" * 500, 400)
        self.assertTrue(out.endswith("<!-- truncated -->"))
        self.assertLessEqual(len(out), 400)


class TestContextBudget(_Ctx):
    def test_truncated_and_omitted_notes_get_hint(self):
        self.note("big", "intro\n\n" + section("Rules") + section("History") + section("Extra"))
        self.note("other", section("Big", 600))
        out = self.run_context(seed=["big", "other"], budget=280)
        self.assertIn("## Rules", out)
        self.assertRegex(out, r"<!-- truncated; cut sections: .*Extra -->")
        self.assertIn("omitted over budget: other", out)
        self.assertIn(f'python "{GRAPH_PATH}" show --body <id>', out)

    def test_no_hint_when_everything_fits(self):
        self.note("a", "short")
        out = self.run_context(seed=["a"])
        self.assertNotIn("load a note", out)


class TestShowBody(_Ctx):
    def test_body_flag_prints_full_text(self):
        self.note("a", "Full text here.\n\n## Sec\nmore")
        out = StringIO()
        with mock.patch.object(graph, "default_paths", return_value=self.paths), \
             redirect_stdout(out):
            graph.cmd_show(Namespace(note="a", body=True))
        self.assertIn("Full text here.", out.getvalue())
        self.assertIn("## Sec", out.getvalue())

    def _show_events(self):
        return [e for e in graph.read_usage(self.paths) if e.get("event") == "show"]

    def _show(self, nid, env):
        with mock.patch.object(graph, "default_paths", return_value=self.paths),              mock.patch.dict(os.environ, env), redirect_stdout(StringIO()):
            graph.cmd_show(Namespace(note=nid, body=True))

    def test_show_body_logs_task_and_from_omitted(self):
        self.note("a", "x")
        self.note("b", "y")
        env = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s1"}
        for k in ("CODEX_THREAD_ID", "CODEX_SESSION_ID"):
            os.environ.pop(k, None)
        with mock.patch.dict(os.environ, env):
            graph.log_usage(self.paths, {"event": "context", "task": "t1",
                                         "notes": ["a"], "core": [], "omitted": ["b"]})
        self._show("b", env)
        self._show("a", env)
        ev = self._show_events()
        self.assertEqual([(e["note"], e["task"], e["from_omitted"]) for e in ev],
                         [("b", "t1", True), ("a", "t1", False)])

    def test_show_body_without_session_has_empty_task(self):
        self.note("a", "x")
        env = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": ""}
        self._show("a", env)
        ev = self._show_events()
        self.assertEqual((ev[0]["task"], ev[0]["from_omitted"]), ("", False))

    def test_context_logs_core_ids_separately(self):
        self.note("c", "core body")
        self.note("a", "x")
        path = self.data / "c.md"
        path.write_text("---\ncore: true\n---\n# c\n\ncore body\n", encoding="utf-8")
        with mock.patch.object(graph, "default_paths", return_value=self.paths):
            self.run_context(seed=["a"], core=True, no_log=False)
        ev = [e for e in graph.read_usage(self.paths) if e.get("event") == "context"]
        self.assertEqual(ev[-1]["core"], ["c"])
        self.assertNotIn("c", ev[-1]["notes"])

    def linked_note(self, nid, target):
        text = "---" + chr(10) + "links: [\"[[" + target + "]]\"]" + chr(10) + "---" + chr(10) + "# " + nid + chr(10) + "alpha" + chr(10)
        (self.data / f"{nid}.md").write_text(text, encoding="utf-8")

    def _ctx_event(self, **kw):
        self.run_context(no_log=False, **kw)
        return [e for e in graph.read_usage(self.paths) if e.get("event") == "context"][-1]

    def test_context_logs_scores_and_fallback_reason(self):
        self.linked_note("a", "b")
        self.note("b", "beta")
        ev = self._ctx_event(seed=["a"])
        self.assertEqual(ev["scores"]["a"][:3], [1, 1.0, None])
        self.assertEqual(ev["scores"]["b"][0], 2)
        self.assertEqual(ev["scores"]["b"][2], "a")
        self.assertEqual(ev["scores"]["b"][3], 0.0)
        self.assertEqual(ev["fallback_reason"], "disabled by --no-semantic")

    def test_learned_share_reflects_learned_edge(self):
        self.linked_note("a", "b")
        self.note("b", "beta")
        g = graph.Graph(self.paths)
        key = graph.pair("a", "b")
        g.add_learned(key, g.base[key])
        g.save_learned()
        ev = self._ctx_event(seed=["a"])
        self.assertAlmostEqual(ev["scores"]["b"][3], 0.5, places=1)

    def test_scores_include_omitted_notes(self):
        self.linked_note("a", "b")
        self.note("b", "beta " + "word " * 500)
        ev = self._ctx_event(seed=["a"], budget=10)
        self.assertIn("b", ev["scores"])
        self.assertTrue(set(ev["omitted"]) <= set(ev["scores"]))

    def test_reinforce_warns_on_unknown_task(self):
        self.note("a", "x")
        graph.log_usage(self.paths, {"event": "context", "task": "known"})
        def run(task):
            err = StringIO()
            args = Namespace(notes=["a"], task=task, rate=0.1)
            with mock.patch.object(graph, "default_paths", return_value=self.paths),                  mock.patch.object(graph, "maybe_auto_decay"),                  redirect_stdout(StringIO()), redirect_stderr(err):
                graph.cmd_reinforce(args)
            return err.getvalue()
        self.assertIn("WARN", run("nope"))
        self.assertNotIn("WARN", run("known"))


if __name__ == "__main__":
    unittest.main()
