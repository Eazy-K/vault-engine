"""Tests for the one-command `update`: the dev channel (fast-forward main), the
post-update routine shared by both channels, --yes versus --all consent and the
engine-developer marker. Same fixtures as test_update.py (real temp git repos,
`update.run_step` always mocked)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_update import Args, UpdateTestCase, _commit, _run, _write, update  # noqa: E402

OK = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
POST_STEPS = ("_agents_step", "_claude_hooks_step", "_models_step", "_codex_hooks_step")


class DevChannelCase(UpdateTestCase):
    """The engine clone sits on a branch named main, one commit behind origin/main."""

    def setUp(self):
        super().setUp()
        branch = self.origin["branch"]
        if branch != "main":
            _run(["git", "branch", "-m", branch, "main"], self.origin["bare"])
            _run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], self.origin["bare"])
        _run(["git", "fetch", "-q", "origin"], self.engine)
        _run(["git", "checkout", "-q", "-B", "main", "v0.10.0"], self.engine)
        self.set_version("0.10.0")
        self.origin_head = _run(["git", "rev-parse", "origin/main"], self.engine).stdout.strip()

    def head(self) -> str:
        return _run(["git", "rev-parse", "HEAD"], self.engine).stdout.strip()

    def run_update(self, **kw):
        with mock.patch("update.run_step", return_value=OK) as steps, \
                redirect_stdout(StringIO()) as buf:
            try:
                update.cmd_update(Args(**kw))
            except SystemExit as exc:
                return steps, buf.getvalue(), exc
        return steps, buf.getvalue(), None


class TestUpdateDevChannel(DevChannelCase):
    def test_fast_forwards_without_asking_and_runs_the_post_steps(self):
        with mock.patch("builtins.input", side_effect=AssertionError("must not ask")):
            steps, out, exc = self.run_update()
        self.assertIsNone(exc)
        self.assertEqual(self.head(), self.origin_head)
        calls = [c.args[2] for c in steps.call_args_list]
        self.assertEqual(calls[0], ["migrate", "--yes"])
        self.assertTrue(calls[1][:2] == ["setup", "--yes"] and "--no-env" in calls[1])
        self.assertEqual(calls[-1], ["doctor"])
        self.assertIn("pulled main", out)

    def test_nothing_to_pull_still_runs_doctor(self):
        _run(["git", "merge", "-q", "--ff-only", "origin/main"], self.engine)
        steps, out, exc = self.run_update()
        self.assertIsNone(exc)
        self.assertIn("already up to date", out)
        self.assertEqual([c.args[2] for c in steps.call_args_list], [["doctor"]])

    def test_ci_pin_is_left_alone(self):
        workflow = self.data / ".github" / "workflows" / "vault.yml"
        _write(workflow, "steps:\n  - uses: example/vault-engine@main\n")
        self.run_update()
        self.assertIn("@main", workflow.read_text(encoding="utf-8"))

    def test_other_branch_stops_and_names_the_command(self):
        _run(["git", "checkout", "-q", "-b", "feature/x"], self.engine)
        head = self.head()
        steps, _out, exc = self.run_update()
        self.assertIn("switch main", str(exc))
        self.assertIn(str(self.engine), str(exc))
        self.assertEqual(self.head(), head)
        steps.assert_not_called()
        self.assertEqual(_run(["git", "branch", "--show-current"], self.engine).stdout.strip(),
                         "feature/x")

    def test_dirty_tree_stops_and_names_the_command(self):
        (self.engine / "CHANGELOG.md").write_text("dirty\n", encoding="utf-8")
        head = self.head()
        steps, _out, exc = self.run_update()
        self.assertIn("local changes", str(exc))
        self.assertIn("stash", str(exc))
        self.assertEqual(self.head(), head)
        steps.assert_not_called()

    def test_diverged_main_stops_without_merging_or_branching(self):
        branches_before = _run(["git", "branch", "--format=%(refname:short)"],
                               self.engine).stdout.split()
        _write(self.engine / "LOCAL.txt", "mine\n")
        local = _commit(self.engine, "local work")
        steps, _out, exc = self.run_update()
        self.assertIn("rebase origin/main", str(exc))
        self.assertEqual(self.head(), local)
        steps.assert_not_called()
        branches = _run(["git", "branch", "--format=%(refname:short)"], self.engine).stdout.split()
        self.assertEqual(branches, branches_before)

    def test_fetch_failure_stops(self):
        _run(["git", "remote", "set-url", "origin", str(self.tmp / "gone.git")], self.engine)
        steps, _out, exc = self.run_update()
        self.assertIn("fetch failed", str(exc))
        steps.assert_not_called()

    def test_failed_step_exits_1(self):
        bad = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
        with mock.patch("update.run_step", return_value=bad), redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit) as ctx:
                update.cmd_update(Args(yes=True))
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("update: migrate failed", buf.getvalue())

    def test_to_tag_switches_a_dev_checkout_to_stable(self):
        _steps, _out, exc = self.run_update(to="v0.2.0", yes=True)
        self.assertIsNone(exc)
        self.assertEqual(self.head_tag(), "v0.2.0")


class TestSharedPostUpdateSteps(DevChannelCase):
    def _patched(self):
        mocks = {}
        for name in POST_STEPS + ("_handle_agents_md",):
            patcher = mock.patch(f"update.{name}")
            mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        return mocks

    def _assert_all_once(self, mocks):
        for name, m in mocks.items():
            with self.subTest(step=name):
                m.assert_called_once()

    def test_dev_channel_runs_every_step(self):
        mocks = self._patched()
        _steps, _out, exc = self.run_update()
        self.assertIsNone(exc)
        self._assert_all_once(mocks)

    def test_stable_upgrade_runs_the_same_steps(self):
        mocks = self._patched()
        self.checkout("v0.1.0")
        self.set_version("0.1.0")
        _steps, _out, exc = self.run_update(to="v0.2.0", yes=True)
        self.assertIsNone(exc)
        self._assert_all_once(mocks)

    def test_stable_already_current_runs_the_same_steps(self):
        mocks = self._patched()
        self.checkout("v0.10.0")
        _steps, _out, exc = self.run_update(yes=True)
        self.assertIsNone(exc)
        self._assert_all_once(mocks)

    def test_order_is_fixed_and_doctor_is_last(self):
        order = []
        for name in POST_STEPS + ("_handle_agents_md",):
            patcher = mock.patch(f"update.{name}",
                                 side_effect=lambda *a, _n=name, **k: order.append(_n))
            patcher.start()
            self.addCleanup(patcher.stop)
        with mock.patch("update.run_step",
                        side_effect=lambda e, d, cmd: order.append(cmd[0]) or OK), \
                redirect_stdout(StringIO()):
            update.cmd_update(Args())
        self.assertEqual(order, ["migrate", "setup", "_handle_agents_md", "_agents_step",
                                 "_claude_hooks_step", "_models_step", "_codex_hooks_step",
                                 "doctor"])


class TestConsent(unittest.TestCase):
    """--yes never writes ~/.claude or ~/.codex by itself; --all does, without prompts."""

    CASES = (("_claude_hooks_step", "would update: x\n", ["claude-hooks", "--install"]),
             ("_models_step", "OUT OF DATE\n", ["models", "--apply"]),
             ("_codex_hooks_step", "would update: x\n", ["codex-hooks", "--install"]))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engine = self.tmp / "engine"
        for name in ("claude_hooks", "models", "codex_hooks"):
            _write(self.engine / "tools" / f"{name}.py", "")
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".codex").mkdir(parents=True)
        for patcher in (mock.patch.object(update.Path, "home", return_value=self.home),
                        mock.patch.dict(sys.modules, {"codex_hooks": types.SimpleNamespace(
                            codex_home=lambda: self.home / ".codex")})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, step, dry_stdout, **kw):
        dry = subprocess.CompletedProcess(args=[], returncode=0, stderr="", stdout=dry_stdout)
        with mock.patch("update.run_step", side_effect=[dry, OK]) as m, \
                mock.patch.object(update.g, "stdin_is_interactive", return_value=False), \
                redirect_stdout(StringIO()) as buf:
            getattr(update, step)(self.engine, self.tmp / "data", Args(**kw))
        return [c.args[2] for c in m.call_args_list], buf.getvalue()

    def test_yes_alone_writes_nothing_and_names_update_all(self):
        for step, dry, install in self.CASES:
            with self.subTest(step=step):
                calls, out = self._run(step, dry, yes=True)
                self.assertNotIn(install, calls)
                self.assertIn("update --all", out)

    def test_all_installs_without_asking(self):
        for step, dry, install in self.CASES:
            with self.subTest(step=step):
                calls, _out = self._run(step, dry, all=True, yes=True)
                self.assertEqual(calls[-1], install)

    def test_codex_hooks_flag_installs(self):
        calls, _out = self._run("_codex_hooks_step", "would update: x\n", codex_hooks=True)
        self.assertEqual(calls[-1], ["codex-hooks", "--install"])

    def test_codex_hooks_quiet_when_current_or_no_codex_home(self):
        calls, _out = self._run("_codex_hooks_step", "hooks up to date: x\n", all=True)
        self.assertEqual(calls, [["codex-hooks"]])
        shutil.rmtree(self.home / ".codex")
        with mock.patch("update.run_step") as m:
            update._codex_hooks_step(self.engine, self.tmp / "data", Args(all=True))
        m.assert_not_called()

    def test_all_implies_yes(self):
        args = Args(all=True)
        with mock.patch.object(update, "channel", return_value=("unknown", None)):
            with self.assertRaises(SystemExit):
                update.cmd_update(args)
        self.assertTrue(args.yes)


class TestEngineDeveloperMarker(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_marker_roundtrip_through_machine_command(self):
        import onboarding
        _write(self.tmp / "AGENTS.md", "x\n")
        self.assertFalse(update.is_engine_developer(self.tmp))
        base = dict(data=str(self.tmp), project_root=None, name=None)
        with redirect_stdout(StringIO()):
            onboarding.cmd_machine(types.SimpleNamespace(**base, engine_developer="on"))
        self.assertTrue(update.is_engine_developer(self.tmp))
        saved = json.loads((self.tmp / ".graph" / "machine.json").read_text(encoding="utf-8"))
        self.assertIs(saved["engine_developer"], True)
        with redirect_stdout(StringIO()):
            onboarding.cmd_machine(types.SimpleNamespace(**base, engine_developer="off"))
        self.assertFalse(update.is_engine_developer(self.tmp))

    def test_only_a_real_true_counts(self):
        _write(self.tmp / ".graph" / "machine.json", json.dumps({"engine_developer": "yes"}))
        self.assertFalse(update.is_engine_developer(self.tmp))


if __name__ == "__main__":
    unittest.main()
