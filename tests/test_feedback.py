"""Tests for tools/feedback.py: opt-in metrics/report feedback to maintainers.

Standard library `unittest` only. Uses temp engine/data dirs and temp git
repos with a fake local identity; `send_issue` (the only thing that would
touch the network) is always mocked -- no test ever calls `gh` for real.
Personal-data samples are synthetic or well-known public test values, never
real people's data (see tests/test_guard.py for the same convention).
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
GRAPH_PATH = TOOLS_DIR / "graph.py"

# Load graph.py the same way test_core.py does: as a standalone module
# registered under the name "graph" before feedback.py does `import graph as g`.
_spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)

sys.path.insert(0, str(TOOLS_DIR))
import feedback  # noqa: E402  (path must be set up first)


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", check=True)


def _init_repo(root: Path, name: str = "Test Runner", email: str = "tester@example.com") -> None:
    root.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], root)
    _run(["git", "config", "user.email", email], root)
    _run(["git", "config", "user.name", name], root)


def _valid_tckn() -> str:
    """An 11-digit TCKN that satisfies the checksum (not a real ID)."""
    d = [1, 2, 3, 4, 5, 6, 7, 8, 9]
    d9 = (sum(d[0:9:2]) * 7 - sum(d[1:8:2])) % 10
    d10 = (sum(d) + d9) % 10
    return "".join(map(str, d + [d9, d10]))


class FeedbackTestCase(unittest.TestCase):
    """Common fixture: a temp engine repo + temp data repo, wired as g.Paths."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.engine = self.tmp / "engine"
        self.data = self.tmp / "data"
        _init_repo(self.engine)
        (self.engine / "file.txt").write_text("x\n", encoding="utf-8")
        _run(["git", "add", "file.txt"], self.engine)
        _run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"], self.engine)
        _init_repo(self.data, name="Data Owner", email="owner@example.com")
        self.paths = graph.Paths(self.engine, self.data)
        # Isolate from the real engine repo: build_metrics/_derive_repo must
        # use this temp engine, and the real user's home must never leak in.
        self._engine_patch = mock.patch.object(graph, "ENGINE", self.engine)
        self._engine_patch.start()
        self.addCleanup(self._engine_patch.stop)
        self._home_patch = mock.patch.object(graph.Path, "home",
                                             return_value=self.tmp / "fake-home")
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)
        # cmd_* handlers call g.default_paths(), which reads VAULT_DATA/VAULT_HOME
        # from the real environment -- pin it to this test's temp paths so no
        # test can ever touch a real data repo the environment happens to point at.
        self._paths_patch = mock.patch.object(graph, "default_paths", return_value=self.paths)
        self._paths_patch.start()
        self.addCleanup(self._paths_patch.stop)

    def set_config(self, feedback: dict) -> None:
        self.paths.config_file.write_text(json.dumps({"feedback": feedback}), encoding="utf-8")

    def set_machine(self, feedback: dict) -> None:
        path = self.data / ".graph" / "machine.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"feedback": feedback}), encoding="utf-8")


class TestSettings(FeedbackTestCase):
    def test_default_is_off(self):
        settings = feedback.load_settings(self.paths)
        self.assertEqual(settings["level"], "off")
        self.assertEqual(settings["mode"], "ask")

    def test_config_level_applies(self):
        self.set_config({"level": "metrics", "mode": "ask"})
        settings = feedback.load_settings(self.paths)
        self.assertEqual(settings["level"], "metrics")
        self.assertEqual(settings["mode"], "ask")

    def test_machine_override_wins(self):
        self.set_config({"level": "reports", "mode": "auto"})
        self.set_machine({"level": "off"})
        settings = feedback.load_settings(self.paths)
        self.assertEqual(settings["level"], "off")

    def test_machine_override_partial_keeps_config_keys(self):
        self.set_config({"level": "reports", "mode": "ask"})
        self.set_machine({"level": "metrics"})  # mode not overridden
        settings = feedback.load_settings(self.paths)
        self.assertEqual(settings["level"], "metrics")
        self.assertEqual(settings["mode"], "ask")

    def test_default_off_sends_nothing(self):
        with mock.patch.object(feedback, "send_issue") as mocked:
            feedback._send_all(self.paths, feedback.load_settings(self.paths), "o/r", timeout=5)
            # level "off" is handled by callers before _send_all, but even a
            # direct call with no reports/metrics due sends nothing extra here.
        # More representative: go through maybe_auto_send with level off.
        with mock.patch.object(feedback, "send_issue") as mocked2:
            feedback.maybe_auto_send(self.paths)
            mocked2.assert_not_called()


class TestRepoDerivation(FeedbackTestCase):
    def test_explicit_repo_wins(self):
        settings = {"repo": "explicit/repo"}
        self.assertEqual(feedback.target_repo(settings), "explicit/repo")

    def test_derive_from_https_remote(self):
        _run(["git", "remote", "add", "origin", "https://github.com/acme/vault-engine.git"],
             self.engine)
        self.assertEqual(feedback._derive_repo(), "acme/vault-engine")

    def test_derive_from_ssh_remote(self):
        _run(["git", "remote", "add", "origin", "git@github.com:acme/vault-engine.git"],  # guard:ignore
             self.engine)
        self.assertEqual(feedback._derive_repo(), "acme/vault-engine")

    def test_no_remote_returns_none(self):
        self.assertIsNone(feedback._derive_repo())


class TestMetrics(FeedbackTestCase):
    def test_metrics_only_numbers_and_enums(self):
        metrics = feedback.build_metrics(self.paths)
        self.assertIn(metrics["note_count_bucket"], ("<25", "25-100", "100-500", "500+"))
        self.assertIsInstance(metrics["context_calls"], int)
        self.assertIsInstance(metrics["reinforce_without_task"], int)
        self.assertIsInstance(metrics["no_useful_notes"], int)
        self.assertEqual(set(metrics["agents"]), set(feedback.AGENTS))
        self.assertEqual(metrics["python"], f"{sys.version_info.major}.{sys.version_info.minor}")
        self.assertEqual(metrics["os"], __import__("platform").system())
        # engine_version comes from git in the temp engine repo, a short hash
        self.assertRegex(metrics["engine_version"], r"^[0-9a-f]{4,}$")

    def test_note_count_bucket_thresholds(self):
        self.assertEqual(feedback._note_bucket(0), "<25")
        self.assertEqual(feedback._note_bucket(24), "<25")
        self.assertEqual(feedback._note_bucket(25), "25-100")
        self.assertEqual(feedback._note_bucket(99), "25-100")
        self.assertEqual(feedback._note_bucket(100), "100-500")
        self.assertEqual(feedback._note_bucket(499), "100-500")
        self.assertEqual(feedback._note_bucket(500), "500+")

    def test_no_engine_version_when_detached_from_git(self):
        no_git = self.tmp / "no-git-engine"
        no_git.mkdir()
        with mock.patch.object(graph, "ENGINE", no_git):
            self.assertEqual(feedback._engine_version(), "unknown")


class TestFilter(FeedbackTestCase):
    def test_guard_pattern_caught(self):
        hit = feedback._filter_text(f"tckn: {_valid_tckn()}", self.paths)
        self.assertEqual(hit, "TCKN")

    def test_clean_text_passes(self):
        self.assertIsNone(feedback._filter_text("the query command felt slow today", self.paths))

    def test_home_path_caught(self):
        home = str(graph.Path.home())
        hit = feedback._filter_text(f"broke under {home}/notes", self.paths)
        self.assertEqual(hit, "home path")

    def test_git_identity_caught(self):
        hit = feedback._filter_text("written by Data Owner earlier", self.paths)
        self.assertEqual(hit, "git identity")

    def test_denylist_term_caught(self):
        git_dir = graph._git_dir(self.data)
        (git_dir / "info").mkdir(parents=True, exist_ok=True)
        (git_dir / "info" / "vault-denylist").write_text("projectcodename\n", encoding="utf-8")
        hit = feedback._filter_text("this mentions projectcodename here", self.paths)
        self.assertEqual(hit, "denylist term")

    def test_username_env_caught(self):
        with mock.patch.dict("os.environ", {"USERNAME": "someuser1"}, clear=False):
            hit = feedback._filter_text("ran as someuser1 on the box", self.paths)
            self.assertEqual(hit, "machine username")

    def test_short_username_not_caught(self):
        with mock.patch.dict("os.environ", {"USERNAME": "abc"}, clear=False):
            self.assertIsNone(feedback._filter_text("abc did the thing", self.paths))

    def test_project_name_caught(self):
        (self.data / "projects" / "megaproj").mkdir(parents=True)
        hit = feedback._filter_text("issue happened inside megaproj today", self.paths)
        self.assertEqual(hit, "project name")

    def test_project_name_short_names_ignored(self):
        (self.data / "projects" / "abc").mkdir(parents=True)  # 3 chars, below floor
        self.assertIsNone(feedback._filter_text("abc broke", self.paths))

    def test_project_root_subfolder_caught(self):
        root = self.tmp / "workspace"
        (root / "bigclientwork").mkdir(parents=True)
        self.set_config({"level": "reports"})
        (self.paths.config_file).write_text(
            json.dumps({"feedback": {"level": "reports"}, "project_roots": [str(root)]}),
            encoding="utf-8")
        hit = feedback._filter_text("working on bigclientwork today", self.paths)
        self.assertEqual(hit, "project name")

    def test_value_never_echoed(self):
        # scan_line's own contract: matched value must never appear in the label.
        hit = feedback._filter_text(f"tckn: {_valid_tckn()}", self.paths)
        self.assertNotIn(_valid_tckn(), hit)

    def test_guard_ignore_suppresses_pattern_match(self):
        line = f"tckn: {_valid_tckn()} guard:ignore"
        self.assertIsNone(feedback._filter_text(line, self.paths))


class TestAddCommand(FeedbackTestCase):
    def _args(self, **kw):
        from argparse import Namespace
        base = {"kind": "friction", "command": "context", "summary": "slow today", "details": None}
        base.update(kw)
        return Namespace(**base)

    def test_add_requires_reports_level(self):
        self.set_config({"level": "metrics"})
        with self.assertRaises(SystemExit):
            feedback.cmd_add(self._args())

    def test_add_queues_clean_report(self):
        self.set_config({"level": "reports"})
        feedback.cmd_add(self._args())
        queued = list(feedback._queue_dir(self.paths).glob("*.json"))
        self.assertEqual(len(queued), 1)
        report = json.loads(queued[0].read_text(encoding="utf-8"))
        self.assertEqual(report["kind"], "friction")
        self.assertEqual(report["summary"], "slow today")

    def test_add_rejects_and_quarantines_leak_without_echoing(self):
        self.set_config({"level": "reports"})
        tckn = _valid_tckn()
        with self.assertRaises(SystemExit) as ctx:
            feedback.cmd_add(self._args(summary=f"broke with tckn {tckn}"))
        self.assertNotIn(tckn, str(ctx.exception))
        self.assertIn("TCKN", str(ctx.exception))
        self.assertEqual(list(feedback._queue_dir(self.paths).glob("*.json")), [])
        quarantined = list(feedback._quarantine_dir(self.paths).glob("*.json"))
        self.assertEqual(len(quarantined), 1)
        record = json.loads(quarantined[0].read_text(encoding="utf-8"))
        self.assertEqual(record["reason"], "TCKN")
        self.assertNotIn(tckn, json.dumps(record))

    def test_summary_and_details_truncated(self):
        self.set_config({"level": "reports"})
        feedback.cmd_add(self._args(summary="x" * 500, details="y" * 5000))
        report = json.loads(next(feedback._queue_dir(self.paths).glob("*.json")).read_text(encoding="utf-8"))
        self.assertLessEqual(len(report["summary"]), 200)
        self.assertLessEqual(len(report["details"]), 1000)


class TestSend(FeedbackTestCase):
    def test_off_sends_nothing(self):
        self.set_config({"level": "off", "repo": "o/r"})
        with mock.patch.object(feedback, "send_issue") as mocked:
            from argparse import Namespace
            feedback.cmd_send(Namespace(yes=True))
            mocked.assert_not_called()

    def test_ask_mode_without_yes_previews_only(self):
        self.set_config({"level": "metrics", "mode": "ask", "repo": "o/r"})
        with mock.patch.object(feedback, "send_issue") as mocked:
            from argparse import Namespace
            feedback.cmd_send(Namespace(yes=False))
            mocked.assert_not_called()

    def test_ask_mode_with_yes_sends(self):
        self.set_config({"level": "metrics", "mode": "ask", "repo": "o/r"})
        with mock.patch.object(feedback, "send_issue", return_value=True) as mocked:
            from argparse import Namespace
            feedback.cmd_send(Namespace(yes=True))
            mocked.assert_called_once()
            title = mocked.call_args[0][1]
            self.assertTrue(title.startswith("[metrics]"))

    def test_auto_mode_sends_without_yes(self):
        self.set_config({"level": "metrics", "mode": "auto", "repo": "o/r"})
        with mock.patch.object(feedback, "send_issue", return_value=True) as mocked:
            from argparse import Namespace
            feedback.cmd_send(Namespace(yes=False))
            mocked.assert_called_once()

    def test_metrics_throttled_to_seven_days(self):
        self.set_config({"level": "metrics", "mode": "auto", "repo": "o/r"})
        with mock.patch.object(feedback, "send_issue", return_value=True) as mocked:
            from argparse import Namespace
            feedback.cmd_send(Namespace(yes=False))
            self.assertEqual(mocked.call_count, 1)
            feedback.cmd_send(Namespace(yes=False))  # immediately again
            self.assertEqual(mocked.call_count, 1)  # throttled, no second call

    def test_metrics_send_after_seven_days_allowed(self):
        self.set_config({"level": "metrics", "mode": "auto", "repo": "o/r"})
        state = {"last_metrics_sent": (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds")}
        feedback._save_state(self.paths, state)
        with mock.patch.object(feedback, "send_issue", return_value=True) as mocked:
            from argparse import Namespace
            feedback.cmd_send(Namespace(yes=False))
            mocked.assert_called_once()

    def test_reports_sent_and_moved_to_sent_dir(self):
        self.set_config({"level": "reports", "mode": "auto", "repo": "o/r"})
        feedback.cmd_add(self._args_for_add())
        with mock.patch.object(feedback, "send_issue", return_value=True):
            from argparse import Namespace
            feedback.cmd_send(Namespace(yes=False))
        self.assertEqual(list(feedback._queue_dir(self.paths).glob("*.json")), [])
        sent = list(feedback._sent_dir(self.paths).glob("*.json"))
        # one for metrics, one for the report
        self.assertGreaterEqual(len(sent), 1)

    def test_send_failure_leaves_report_queued(self):
        self.set_config({"level": "reports", "mode": "auto", "repo": "o/r"})
        feedback.cmd_add(self._args_for_add())
        with mock.patch.object(feedback, "send_issue", return_value=False):
            from argparse import Namespace
            feedback.cmd_send(Namespace(yes=False))
        self.assertEqual(len(list(feedback._queue_dir(self.paths).glob("*.json"))), 1)

    def test_leaky_report_quarantined_at_send_time_not_sent(self):
        self.set_config({"level": "reports", "mode": "auto", "repo": "o/r"})
        # Bypass cmd_add's own filter by writing the queue file directly, to
        # simulate a leak that only became detectable later (e.g. a project
        # created after the report was queued).
        feedback._queue_dir(self.paths).mkdir(parents=True, exist_ok=True)
        tckn = _valid_tckn()
        report = {"id": "abcd1234", "kind": "bug", "command": "query",
                  "summary": f"broke with {tckn}", "details": "", "created": "2024-01-01T00:00:00"}
        (feedback._queue_dir(self.paths) / "abcd1234.json").write_text(
            json.dumps(report), encoding="utf-8")
        with mock.patch.object(feedback, "send_issue", return_value=True) as mocked:
            from argparse import Namespace
            feedback.cmd_send(Namespace(yes=False))
        self.assertEqual(list(feedback._queue_dir(self.paths).glob("*.json")), [])
        quarantined = list(feedback._quarantine_dir(self.paths).glob("*.json"))
        self.assertEqual(len(quarantined), 1)
        self.assertNotIn(tckn, json.dumps([str(p) for p in quarantined]))
        # send_issue must not have been called with the leaking report's body
        for call in mocked.call_args_list:
            self.assertNotIn(tckn, call[0][2])

    def _args_for_add(self, **kw):
        from argparse import Namespace
        base = {"kind": "friction", "command": "context", "summary": "slow today", "details": None}
        base.update(kw)
        return Namespace(**base)

    def test_no_repo_reports_unavailable_without_crashing(self):
        self.set_config({"level": "metrics", "mode": "auto"})  # no repo, no origin remote
        with mock.patch.object(feedback, "send_issue") as mocked:
            from argparse import Namespace
            feedback.cmd_send(Namespace(yes=False))
            mocked.assert_not_called()


class TestAutoSend(FeedbackTestCase):
    def test_off_level_never_sends(self):
        self.set_config({"level": "off", "repo": "o/r"})
        with mock.patch.object(feedback, "send_issue") as mocked:
            feedback.maybe_auto_send(self.paths)
            mocked.assert_not_called()

    def test_ask_mode_never_auto_sends(self):
        self.set_config({"level": "metrics", "mode": "ask", "repo": "o/r"})
        with mock.patch.object(feedback, "send_issue") as mocked:
            feedback.maybe_auto_send(self.paths)
            mocked.assert_not_called()

    def test_auto_mode_sends_once(self):
        self.set_config({"level": "metrics", "mode": "auto", "repo": "o/r"})
        with mock.patch.object(feedback, "send_issue", return_value=True) as mocked:
            feedback.maybe_auto_send(self.paths)
            self.assertEqual(mocked.call_count, 1)

    def test_auto_mode_throttled_to_24h(self):
        self.set_config({"level": "metrics", "mode": "auto", "repo": "o/r"})
        with mock.patch.object(feedback, "send_issue", return_value=True) as mocked:
            feedback.maybe_auto_send(self.paths)
            feedback.maybe_auto_send(self.paths)
            self.assertEqual(mocked.call_count, 1)

    def test_auto_mode_after_24h_allowed(self):
        self.set_config({"level": "metrics", "mode": "auto", "repo": "o/r"})
        state = {"last_auto_send": (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds"),
                 "last_metrics_sent": (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds")}
        feedback._save_state(self.paths, state)
        with mock.patch.object(feedback, "send_issue", return_value=True) as mocked:
            feedback.maybe_auto_send(self.paths)
            self.assertEqual(mocked.call_count, 1)

    def test_never_raises_even_if_send_issue_blows_up(self):
        self.set_config({"level": "metrics", "mode": "auto", "repo": "o/r"})
        with mock.patch.object(feedback, "send_issue", side_effect=RuntimeError("boom")):
            feedback.maybe_auto_send(self.paths)  # must not raise


class TestSetCommand(FeedbackTestCase):
    def test_set_writes_config(self):
        from argparse import Namespace
        feedback.cmd_set(Namespace(level="metrics", mode="ask", repo=None, machine=False))
        data = json.loads(self.paths.config_file.read_text(encoding="utf-8"))
        self.assertEqual(data["feedback"]["level"], "metrics")
        self.assertEqual(data["feedback"]["mode"], "ask")

    def test_set_machine_writes_override(self):
        from argparse import Namespace
        feedback.cmd_set(Namespace(level="off", mode=None, repo=None, machine=True))
        data = json.loads((self.data / ".graph" / "machine.json").read_text(encoding="utf-8"))
        self.assertEqual(data["feedback"]["level"], "off")
        self.assertFalse(self.paths.config_file.exists())

    def test_set_preserves_other_config_keys(self):
        self.paths.config_file.write_text(json.dumps({"project_roots": ["/x"]}), encoding="utf-8")
        from argparse import Namespace
        feedback.cmd_set(Namespace(level="metrics", mode=None, repo=None, machine=False))
        data = json.loads(self.paths.config_file.read_text(encoding="utf-8"))
        self.assertEqual(data["project_roots"], ["/x"])
        self.assertEqual(data["feedback"]["level"], "metrics")


class TestStatusAndList(FeedbackTestCase):
    def test_status_runs_clean(self):
        from argparse import Namespace
        feedback.cmd_status(Namespace())  # must not raise

    def test_list_reports_reports_level_includes_queue(self):
        self.set_config({"level": "reports"})
        from argparse import Namespace
        feedback.cmd_add(Namespace(kind="idea", command="query", summary="nice idea", details=None))
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            feedback.cmd_list(Namespace())
        preview = json.loads(buf.getvalue())
        self.assertEqual(len(preview["reports"]), 1)
        self.assertIn("metrics", preview)


if __name__ == "__main__":
    unittest.main()
