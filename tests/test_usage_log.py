"""usage.log trimming, and proof that hook tests never touch a real data dir."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
import _isolation  # noqa: E402,F401
REPO_ROOT = TESTS_DIR.parent
_spec = importlib.util.spec_from_file_location("graph", REPO_ROOT / "tools" / "graph.py")
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)

HOOK_TEST_MODULES = ["test_agent_guard", "test_codex_agent_guard", "test_codex_context_warn",
                     "test_codex_delegation_warn", "test_context_warn", "test_delegation_warn",
                     "test_statusline"]


class TestTrim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = graph.Paths(REPO_ROOT, Path(self.tmp.name))
        self.paths.usage_log.parent.mkdir()
        self.now = datetime(2026, 9, 30, 12, 0, 0)

    def _line(self, days_ago):
        ts = (self.now - timedelta(days=days_ago)).isoformat(timespec="seconds")
        return json.dumps({"ts": ts, "event": "context"}) + "\n"

    def test_old_lines_dropped_unparsable_and_recent_kept(self):
        old, recent = self._line(91), self._line(89)
        self.paths.usage_log.write_text(old + "garbage\n" + recent + '{"no": "ts"}\n', encoding="utf-8")
        self.assertEqual(graph.trim_usage_log(self.paths, self.now), 1)
        self.assertEqual(self.paths.usage_log.read_text(encoding="utf-8"),
                         "garbage\n" + recent + '{"no": "ts"}\n')
        self.assertFalse(self.paths.usage_log.with_name("usage.log.tmp").exists())
        self.assertEqual(len(graph.read_usage(self.paths)), 2)  # stats reader still works

    def test_recent_log_is_not_rewritten(self):
        self.paths.usage_log.write_text(self._line(10), encoding="utf-8")
        before = self.paths.usage_log.stat().st_mtime_ns
        self.assertEqual(graph.trim_usage_log(self.paths, self.now), 0)
        self.assertEqual(self.paths.usage_log.stat().st_mtime_ns, before)

    def test_log_usage_triggers_trim(self):
        self.paths.usage_log.write_text(self._line(400), encoding="utf-8")
        graph.log_usage(self.paths, {"event": "context"})
        self.assertEqual(len(graph.read_usage(self.paths)), 1)

    def test_missing_log_is_fine(self):
        self.paths.usage_log.unlink(missing_ok=True)
        self.assertEqual(graph.trim_usage_log(self.paths, self.now), 0)


class TestHookTestsDoNotLeak(unittest.TestCase):
    def test_hook_test_modules_leave_sentinel_data_dir_untouched(self):
        with tempfile.TemporaryDirectory() as sentinel:
            env = dict(os.environ, VAULT_DATA=sentinel, VAULT_HOME=sentinel)
            result = subprocess.run(
                [sys.executable, "-m", "unittest", *HOOK_TEST_MODULES],
                cwd=TESTS_DIR, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertEqual(list(Path(sentinel).iterdir()), [], "a test wrote into the real data dir")


if __name__ == "__main__":
    unittest.main()
