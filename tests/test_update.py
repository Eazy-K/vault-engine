"""Tests for tools/update.py (self-update the engine checkout via `update`).

Standard library `unittest` only. Every fixture is a real temp git repo (a
bare "origin" with three tagged commits, plus per-test clones); nothing here
touches the network or the real user's engine/environment. Post-checkout
steps (migrate/setup/doctor) are exercised through `update.run_step`, which
tests always mock -- no test ever spawns a real `graph.py` subprocess.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

for _var in ("VAULT_DATA", "VAULT_HOME"):
    os.environ.pop(_var, None)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
GRAPH_PATH = TOOLS_DIR / "graph.py"

_spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)

sys.path.insert(0, str(TOOLS_DIR))
import update  # noqa: E402  (path must be set up first)


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", check=True)


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], root)
    _run(["git", "config", "user.email", "tester@example.com"], root)
    _run(["git", "config", "user.name", "Test Runner"], root)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _commit(root: Path, message: str) -> str:
    _run(["git", "add", "-A"], root)
    _run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", message], root)
    return _run(["git", "rev-parse", "HEAD"], root).stdout.strip()


CHANGELOG_V1 = """# Changelog

## [0.1.0] - 2026-01-01
makeronepointoh
"""

CHANGELOG_V2 = """# Changelog

## [0.2.0] - 2026-02-01
makertwopointoh

### Upgrade notes
read me please

## [0.1.0] - 2026-01-01
makeronepointoh
"""

CHANGELOG_V10 = """# Changelog

## [0.10.0] - 2026-03-01
makertenpointoh

## [0.2.0] - 2026-02-01
makertwopointoh

### Upgrade notes
read me please

## [0.1.0] - 2026-01-01
makeronepointoh
"""


def _build_origin(tmp: Path) -> dict:
    """A bare origin with three tagged commits (v0.1.0, v0.2.0, v0.10.0) and one
    untagged commit on top. Returns {"bare": path, "untagged_sha": sha}."""
    seed = tmp / "seed"
    _init_repo(seed)

    _write(seed / "CHANGELOG.md", CHANGELOG_V1)
    _write(seed / "templates" / "AGENTS.md", "AGENTS v1\ncontent-a\n")
    _write(seed / "tools" / "graph.py", "# stub\n")
    _commit(seed, "v0.1.0")
    _run(["git", "tag", "v0.1.0"], seed)

    _write(seed / "CHANGELOG.md", CHANGELOG_V2)
    _write(seed / "tools" / "schema.py", "SCHEMA_VERSION = 1\n")
    _write(seed / "templates" / "AGENTS.md", "AGENTS v2\ncontent-b\n")
    _commit(seed, "v0.2.0")
    _run(["git", "tag", "v0.2.0"], seed)

    _write(seed / "CHANGELOG.md", CHANGELOG_V10)
    _write(seed / "tools" / "schema.py", "SCHEMA_VERSION = 2\n")
    _write(seed / "templates" / "AGENTS.md", "AGENTS v3\ncontent-c\n")
    _commit(seed, "v0.10.0")
    _run(["git", "tag", "v0.10.0"], seed)

    _write(seed / "NOTES.txt", "untagged bump\n")
    untagged_sha = _commit(seed, "untagged bump")

    bare = tmp / "origin.git"
    _run(["git", "init", "-q", "--bare", str(bare)], tmp)
    _run(["git", "push", "-q", str(bare), "HEAD", "--tags"], seed)
    # HEAD ref name on the bare repo may not be "main" depending on git defaults;
    # give it an explicit branch pointer so clones land on a real branch.
    branch = _run(["git", "branch", "--show-current"], seed).stdout.strip()
    _run(["git", "symbolic-ref", "HEAD", f"refs/heads/{branch}"], bare)
    return {"bare": bare, "untagged_sha": untagged_sha, "branch": branch}


def _clone(bare: Path, dest: Path) -> None:
    _run(["git", "clone", "-q", str(bare), str(dest)], dest.parent)
    _run(["git", "config", "user.email", "tester@example.com"], dest)
    _run(["git", "config", "user.name", "Test Runner"], dest)


class UpdateTestCase(unittest.TestCase):
    """A fresh origin + a stable-channel engine clone checked out at v0.1.0,
    plus a data dir, per test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.origin = _build_origin(self.tmp)
        self.engine = self.tmp / "engine"
        self.engine.parent.mkdir(parents=True, exist_ok=True)
        _clone(self.origin["bare"], self.engine)
        _run(["git", "checkout", "-q", "v0.1.0"], self.engine)

        self.data = self.tmp / "data"
        self.data.mkdir()
        self.paths = graph.Paths(self.engine, self.data)

        self._engine_patch = mock.patch.object(graph, "ENGINE", self.engine)
        self._engine_patch.start()
        self.addCleanup(self._engine_patch.stop)
        self._paths_patch = mock.patch.object(graph, "default_paths", return_value=self.paths)
        self._paths_patch.start()
        self.addCleanup(self._paths_patch.stop)
        self._version_patch = mock.patch.object(graph, "__version__", "0.1.0")
        self._version_patch.start()
        self.addCleanup(self._version_patch.stop)

    def head_tag(self) -> str:
        out = _run(["git", "describe", "--tags", "--exact-match"], self.engine)
        return out.stdout.strip()

    def set_version(self, version: str) -> None:
        self._version_patch.stop()
        self._version_patch = mock.patch.object(graph, "__version__", version)
        self._version_patch.start()

    def checkout(self, ref: str) -> None:
        _run(["git", "checkout", "-q", ref], self.engine)

    def set_config(self, updates: dict) -> None:
        cfg = json.loads(self.paths.config_file.read_text(encoding="utf-8")) \
            if self.paths.config_file.exists() else {}
        cfg["updates"] = updates
        self.paths.config_file.write_text(json.dumps(cfg), encoding="utf-8")

    def set_machine(self, updates: dict) -> None:
        path = self.data / ".graph" / "machine.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"updates": updates}), encoding="utf-8")


class TestParseVersion(unittest.TestCase):
    def test_parses_semver(self):
        self.assertEqual(update.parse_version("v0.10.0"), (0, 10, 0))
        self.assertEqual(update.parse_version("0.2.0"), (0, 2, 0))

    def test_ordering(self):
        self.assertGreater(update.parse_version("v0.10.0"), update.parse_version("v0.9.0"))
        self.assertGreater(update.parse_version("v0.2.0"), update.parse_version("v0.1.0"))

    def test_rejects_non_semver(self):
        self.assertIsNone(update.parse_version("v1.2.3-beta"))
        self.assertIsNone(update.parse_version("release"))
        self.assertIsNone(update.parse_version(""))


class TestChannel(UpdateTestCase):
    def test_stable_at_exact_tag(self):
        status, ref = update.channel(self.engine)
        self.assertEqual((status, ref), ("stable", "v0.1.0"))

    def test_dev_on_branch(self):
        self.checkout(self.origin["branch"])
        status, ref = update.channel(self.engine)
        self.assertEqual((status, ref), ("dev", self.origin["branch"]))

    def test_unknown_detached_not_on_tag(self):
        self.checkout(self.origin["untagged_sha"])
        status, ref = update.channel(self.engine)
        self.assertEqual(status, "unknown")
        self.assertIsNone(ref)

    def test_unknown_not_a_git_repo(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        status, ref = update.channel(plain)
        self.assertEqual(status, "unknown")


class TestRemoteLatest(UpdateTestCase):
    def test_finds_highest_tag(self):
        self.assertEqual(update.remote_latest(self.engine), "v0.10.0")

    def test_none_on_unreachable_remote(self):
        broken = self.tmp / "broken"
        _init_repo(broken)
        _run(["git", "remote", "add", "origin", str(self.tmp / "does-not-exist")], broken)
        self.assertIsNone(update.remote_latest(broken, timeout=5))


class TestSettings(UpdateTestCase):
    def test_default_check_true(self):
        self.assertTrue(update.load_settings(self.paths)["check"])

    def test_config_disables(self):
        self.set_config({"check": False})
        self.assertFalse(update.load_settings(self.paths)["check"])

    def test_machine_override_wins(self):
        self.set_config({"check": True})
        self.set_machine({"check": False})
        self.assertFalse(update.load_settings(self.paths)["check"])


class TestMaybeCheck(UpdateTestCase):
    def test_runs_when_due_and_saves_cache(self):
        with mock.patch("update.remote_latest", return_value="v0.10.0") as m:
            update.maybe_check(self.paths)
        m.assert_called_once()
        cache = update._load_cache(self.engine)
        self.assertEqual(cache["latest"], "v0.10.0")

    def test_throttled_within_24h(self):
        update._save_cache(self.engine, {"latest": "v0.10.0",
                                         "checked": datetime.now().isoformat(timespec="seconds")})
        with mock.patch("update.remote_latest") as m:
            update.maybe_check(self.paths)
        m.assert_not_called()

    def test_runs_again_after_24h(self):
        old = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
        update._save_cache(self.engine, {"latest": "v0.2.0", "checked": old})
        with mock.patch("update.remote_latest", return_value="v0.10.0") as m:
            update.maybe_check(self.paths)
        m.assert_called_once()

    def test_disabled_by_settings(self):
        self.set_config({"check": False})
        with mock.patch("update.remote_latest") as m:
            update.maybe_check(self.paths)
        m.assert_not_called()

    def test_noop_on_dev_channel(self):
        self.checkout(self.origin["branch"])
        with mock.patch("update.remote_latest") as m:
            update.maybe_check(self.paths)
        m.assert_not_called()

    def test_check_leaves_tracked_files_alone(self):
        # A dirty checkout would make `update` refuse to move later on.
        with mock.patch("update.remote_latest", return_value="v0.10.0"):
            update.maybe_check(self.paths)
        self.assertTrue(update._cache_file(self.engine).exists())
        self.assertIsNone(update._dirty_engine(self.engine))


class TestContextHint(UpdateTestCase):
    def test_present_when_newer_cached(self):
        update._save_cache(self.engine, {"latest": "v0.10.0",
                                         "checked": datetime.now().isoformat(timespec="seconds")})
        hint = update.context_hint(self.paths)
        self.assertIsNotNone(hint)
        self.assertIn("0.10.0", hint)
        self.assertIn("0.1.0", hint)
        self.assertIn("update", hint)

    def test_absent_when_up_to_date(self):
        update._save_cache(self.engine, {"latest": "v0.1.0",
                                         "checked": datetime.now().isoformat(timespec="seconds")})
        self.assertIsNone(update.context_hint(self.paths))

    def test_absent_without_cache(self):
        self.assertIsNone(update.context_hint(self.paths))

    def test_absent_when_disabled(self):
        update._save_cache(self.engine, {"latest": "v0.10.0",
                                         "checked": datetime.now().isoformat(timespec="seconds")})
        self.set_config({"check": False})
        self.assertIsNone(update.context_hint(self.paths))

    def test_absent_on_dev_channel(self):
        update._save_cache(self.engine, {"latest": "v0.10.0",
                                         "checked": datetime.now().isoformat(timespec="seconds")})
        self.checkout(self.origin["branch"])
        self.assertIsNone(update.context_hint(self.paths))


class Args:
    """Minimal stand-in for argparse.Namespace."""

    def __init__(self, **kw):
        self.check = False
        self.to = None
        self.yes = False
        self.apply_agents = False
        self.__dict__.update(kw)


class TestUpdateCheck(UpdateTestCase):
    def test_check_prints_and_changes_nothing(self):
        with redirect_stdout(StringIO()) as buf:
            update.cmd_update(Args(check=True))
        out = buf.getvalue()
        self.assertIn("current: v0.1.0", out)
        self.assertIn("channel: stable", out)
        self.assertIn("v0.10.0", out)
        self.assertEqual(self.head_tag(), "v0.1.0")


class TestUpdateDevAndUnknown(UpdateTestCase):
    def test_dev_channel_explains_and_exits_cleanly(self):
        self.checkout(self.origin["branch"])
        with redirect_stdout(StringIO()) as buf:
            update.cmd_update(Args(yes=True))
        self.assertIn("development checkout", buf.getvalue())

    def test_unknown_channel_exits_1(self):
        self.checkout(self.origin["untagged_sha"])
        with redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                update.cmd_update(Args(yes=True))
        self.assertTrue(ctx.exception.code)


class TestUpdateDirtyTree(UpdateTestCase):
    def test_refuses_with_dirty_tree(self):
        (self.engine / "CHANGELOG.md").write_text("dirty\n", encoding="utf-8")
        with redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                update.cmd_update(Args(yes=True))
        self.assertTrue(ctx.exception.code)
        self.assertEqual(self.head_tag(), "v0.1.0")


class TestUpdateAlreadyCurrent(UpdateTestCase):
    def test_already_up_to_date(self):
        self.checkout("v0.10.0")
        self.set_version("0.10.0")
        with redirect_stdout(StringIO()) as buf:
            update.cmd_update(Args(yes=True))
        self.assertIn("already up to date", buf.getvalue())
        self.assertEqual(self.head_tag(), "v0.10.0")


class TestUpdatePreviewAndNonTty(UpdateTestCase):
    def test_preview_has_only_the_right_sections_and_refuses_non_tty(self):
        with mock.patch("sys.stdin.isatty", return_value=False), redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit) as ctx:
                update.cmd_update(Args())
        out = buf.getvalue()
        self.assertTrue(ctx.exception.code)
        self.assertIn("makertwopointoh", out)
        self.assertIn("makertenpointoh", out)
        self.assertNotIn("makeronepointoh", out)
        self.assertIn("Upgrade notes", out)
        # no checkout happened
        self.assertEqual(self.head_tag(), "v0.1.0")


class TestUpdateApply(UpdateTestCase):
    def test_yes_checks_out_and_runs_steps(self):
        fake_ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr="")
        with mock.patch("update.run_step", return_value=fake_ok) as m, \
             redirect_stdout(StringIO()) as buf:
            update.cmd_update(Args(to="v0.2.0", yes=True))
        self.assertEqual(self.head_tag(), "v0.2.0")
        calls = [c.args[2] for c in m.call_args_list]
        self.assertIn(["migrate", "--yes"], calls)
        self.assertTrue(any(c[:2] == ["setup", "--yes"] for c in calls))
        self.assertIn(["doctor"], calls)
        self.assertIn("checked out v0.2.0", buf.getvalue())

    def test_step_failure_reports_rollback_hint(self):
        fake_fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
        with mock.patch("update.run_step", return_value=fake_fail), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit):
                update.cmd_update(Args(to="v0.2.0", yes=True))
        self.assertIn("update --to v0.1.0", buf.getvalue())
        self.assertIn("graph.py", buf.getvalue())


class TestUpdateDowngradeGuard(UpdateTestCase):
    def test_refused_when_target_schema_below_vault_schema(self):
        self.checkout("v0.10.0")
        self.set_version("0.10.0")
        self.paths.config_file.write_text(json.dumps({"schema": 2}), encoding="utf-8")
        with redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit) as ctx:
                update.cmd_update(Args(to="v0.1.0", yes=True))
        self.assertIn("refusing", str(ctx.exception.code))
        self.assertEqual(self.head_tag(), "v0.10.0")


class TestUpdateAgentsMd(UpdateTestCase):
    def test_diff_shown_and_not_written_with_yes_alone(self):
        (self.data / "AGENTS.md").write_text("AGENTS v1\ncontent-a\n", encoding="utf-8")
        fake_ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch("update.run_step", return_value=fake_ok), \
             redirect_stdout(StringIO()) as buf:
            update.cmd_update(Args(to="v0.2.0", yes=True))
        out = buf.getvalue()
        self.assertIn("differs from the new template", out)
        self.assertIn("rerun with --apply-agents", out)
        self.assertEqual((self.data / "AGENTS.md").read_text(encoding="utf-8"), "AGENTS v1\ncontent-a\n")

    def test_written_with_apply_agents(self):
        (self.data / "AGENTS.md").write_text("AGENTS v1\ncontent-a\n", encoding="utf-8")
        fake_ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch("update.run_step", return_value=fake_ok), \
             redirect_stdout(StringIO()):
            update.cmd_update(Args(to="v0.2.0", yes=True, apply_agents=True))
        self.assertEqual((self.data / "AGENTS.md").read_text(encoding="utf-8"), "AGENTS v2\ncontent-b\n")

    def test_no_diff_when_template_did_not_change_between_releases(self):
        self.assertFalse(update._template_changed(self.engine, "v0.2.0", "v0.2.0"))
        self.assertTrue(update._template_changed(self.engine, "v0.1.0", "v0.2.0"))


if __name__ == "__main__":
    unittest.main()
