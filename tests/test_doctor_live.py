"""`doctor --live`: probe task through the real code paths, marking, stats exclusion."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
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
_spec = importlib.util.spec_from_file_location("graph", REPO_ROOT / "tools" / "graph.py")
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8")


class LiveBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        for cmd in (["init", "-q"], ["config", "user.email", "t@example.com"],
                    ["config", "user.name", "Test Runner"], ["config", "commit.gpgsign", "false"]):
            git(cmd, self.data)
        (self.data / "a.md").write_text("---\ncore: true\n---\n# a\n\nprobe alpha body\n",
                                        encoding="utf-8")
        (self.data / "b.md").write_text("# b\n\ndoctor live probe beta\n", encoding="utf-8")
        (self.data / ".gitignore").write_text(".graph/\n", encoding="utf-8")
        git(["add", "-A"], self.data)
        git(["commit", "-q", "-m", "init"], self.data)
        self.paths = graph.Paths(REPO_ROOT, self.data)
        patch = mock.patch.object(graph, "default_paths", return_value=self.paths)
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {"VAULT_MACHINE": "probe-test"})
        env.start()
        self.addCleanup(env.stop)

    def live(self):
        return graph.live_checks(self.paths)

    def by_field(self, checks):
        return {msg.split(":")[0]: status for status, msg in checks}

    def head(self):
        return git(["rev-parse", "HEAD"], self.data).stdout.strip()

    def log_stop(self, **fields):
        graph.log_usage(self.paths, {"event": "subagent_stop", **fields})

    def stripping(self, event_name, *keys):
        """Patch log_usage so `event_name` events lose `keys` (simulates a broken writer)."""
        real = graph.log_usage

        def strip(paths, event):
            if event.get("event") == event_name:
                event = {k: v for k, v in event.items() if k not in keys}
            real(paths, event)
        return mock.patch.object(graph, "log_usage", strip)


class TestLiveChecks(LiveBase):
    def test_all_probe_checks_ok(self):
        self.log_stop(agent_type="worker-low")
        checks = self.live()
        self.assertEqual({s for s, _ in checks}, {"OK"}, checks)
        fields = self.by_field(checks)
        for name in ("context core", "context scores", "context fallback_reason", "show task",
                     "show from_omitted", "reinforce outcome", "stats --retrieval",
                     "subagent_stop agent_type"):
            self.assertEqual(fields.get(name), "OK", name)

    def test_subagent_stop_missing_is_warn_with_hint(self):
        checks = self.live()
        warn = [m for s, m in checks if s == "WARN"]
        self.assertEqual(len(warn), 1)
        self.assertIn("run any subagent", warn[0])
        self.assertNotIn("FAIL", {s for s, _ in checks})

    def test_subagent_stop_empty_agent_type_fails(self):
        self.log_stop(agent_type="")
        self.assertEqual(self.by_field(self.live())["subagent_stop agent_type"], "FAIL")

    def test_subagent_stop_uses_last_record(self):
        self.log_stop(agent_type="old")
        self.log_stop()
        self.assertEqual(self.by_field(self.live())["subagent_stop agent_type"], "FAIL")
        self.log_stop(agent_type="new")
        self.assertEqual(self.by_field(self.live())["subagent_stop agent_type"], "OK")

    def test_empty_vault_fails_context_show_reinforce(self):
        with mock.patch.object(graph, "retrieve", return_value=[]):  # nothing to load
            fields = self.by_field(self.live())
        for name in ("context core", "context scores", "context fallback_reason", "show task",
                     "reinforce outcome"):
            self.assertEqual(fields[name], "FAIL", name)

    def test_missing_context_fields_fail(self):
        with self.stripping("context", "scores", "core"):
            fields = self.by_field(self.live())
        self.assertEqual(fields["context scores"], "FAIL")
        self.assertEqual(fields["context core"], "FAIL")
        self.assertEqual(fields["context fallback_reason"], "OK")

    def test_missing_fallback_reason_fails(self):
        with self.stripping("context", "fallback_reason"):
            fields = self.by_field(self.live())
        self.assertEqual(fields["context fallback_reason"], "FAIL")
        self.assertEqual(fields["context scores"], "OK")

    def test_show_without_task_fails(self):
        with self.stripping("show", "task", "from_omitted"):
            fields = self.by_field(self.live())
        self.assertEqual(fields["show task"], "FAIL")
        self.assertEqual(fields["show from_omitted"], "FAIL")

    def test_reinforce_without_outcome_fails(self):
        with self.stripping("reinforce", "outcome"):
            self.assertEqual(self.by_field(self.live())["reinforce outcome"], "FAIL")

    def test_stats_crash_fails(self):
        with mock.patch.object(graph, "cmd_stats", side_effect=SystemExit("boom")):
            self.assertEqual(self.by_field(self.live())["stats --retrieval"], "FAIL")

    def test_stats_counting_probe_events_fails(self):
        with mock.patch.object(graph, "retrieval_stats",
                               return_value={"closure": {"contexts": 99}}):
            self.assertEqual(self.by_field(self.live())["stats --retrieval"], "FAIL")


class TestProbeMarkingAndSideEffects(LiveBase):
    def stats_json(self):
        buf = StringIO()
        with redirect_stdout(buf):
            graph.cmd_stats(Namespace(tokens=False, retrieval=True, json=True,
                                      since=None, until=None))
        return json.loads(buf.getvalue())

    def test_every_probe_event_is_marked_and_flag_is_reset(self):
        self.live()
        events = graph.read_usage(self.paths, include_probe=True)
        self.assertEqual([e["event"] for e in events], ["context", "show", "reinforce"])
        self.assertTrue(all(e.get("probe") is True for e in events))
        self.assertTrue(events[0]["task"].startswith("probe-"))
        self.assertEqual({e["task"] for e in events}, {events[0]["task"]})
        self.assertFalse(graph.PROBE["on"])
        graph.log_usage(self.paths, {"event": "context", "task": "real"})
        self.assertNotIn("probe", graph.read_usage(self.paths)[-1])

    def test_probe_events_hidden_by_default_and_from_stats(self):
        graph.log_usage(self.paths, {"event": "context", "task": "t1", "notes": [], "core": []})
        graph.log_usage(self.paths, {"event": "reinforce", "task": "t1", "notes": []})
        before = self.stats_json()
        self.live()
        self.assertEqual(len(graph.read_usage(self.paths)), 2)
        self.assertEqual(len(graph.read_usage(self.paths, include_probe=True)), 5)
        self.assertEqual(self.stats_json(), before)
        buf = StringIO()
        with redirect_stdout(buf):
            graph.cmd_stats(Namespace(tokens=False, retrieval=False, json=False,
                                      since=None, until=None))
        self.assertIn("context calls:       1", buf.getvalue())

    def test_no_commit_no_weight_change_no_decay(self):
        head = self.head()
        learned = self.paths.learned_dir
        self.assertFalse(learned.exists() and any(learned.iterdir()))
        self.live()
        self.assertEqual(self.head(), head)
        self.assertEqual(git(["status", "--porcelain"], self.data).stdout.strip(), "")
        self.assertFalse(learned.exists() and any(learned.iterdir()))

    def test_probe_reinforce_skips_decay_commit_and_save(self):
        with mock.patch.object(graph, "maybe_auto_decay") as decay, \
             mock.patch.object(graph.Graph, "commit_learned") as commit, \
             mock.patch.object(graph.Graph, "save_learned") as save:
            self.live()
        decay.assert_not_called()
        commit.assert_not_called()
        save.assert_not_called()

    def test_real_reinforce_still_commits(self):
        graph.log_usage(self.paths, {"event": "context", "task": "t1"})
        with mock.patch.object(graph.Graph, "commit_learned") as commit, \
             redirect_stdout(StringIO()):
            graph.cmd_reinforce(Namespace(notes=[], task="t1", rate=0.1, outcome="ok",
                                          rework=False))
        commit.assert_called_once()


class TestDoctorCli(LiveBase):
    def run_cli(self):
        env = {k: v for k, v in os.environ.items() if k not in ("VAULT_DATA", "VAULT_HOME")}
        env["VAULT_DATA"] = str(self.data)
        return subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "graph.py"),
                               "doctor", "--live"], capture_output=True, text=True,
                              encoding="utf-8", env=env, cwd=self.data)

    def test_exit_zero_and_lines(self):
        self.log_stop(agent_type="worker-low")
        res = self.run_cli()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("OK   context core:", res.stdout)

    def test_exit_nonzero_on_fail(self):
        self.log_stop(agent_type="")
        res = self.run_cli()
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("FAIL subagent_stop agent_type:", res.stdout)


if __name__ == "__main__":
    unittest.main()
