"""Codex adapter tests. Every installer path is under a temporary directory."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

for _var in ("VAULT_DATA", "VAULT_HOME"):
    os.environ.pop(_var, None)


def _no_registry(*_args):
    raise OSError("tests never read the real registry")


sys.modules["winreg"] = types.SimpleNamespace(HKEY_CURRENT_USER=None, OpenKey=_no_registry)
REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
spec = importlib.util.spec_from_file_location("graph", TOOLS_DIR / "graph.py")
graph = importlib.util.module_from_spec(spec)
sys.modules["graph"] = graph
spec.loader.exec_module(graph)
sys.path.insert(0, str(TOOLS_DIR))
import codex_hooks  # noqa: E402
import onboarding  # noqa: E402


class TestMerge(unittest.TestCase):
    def test_merge_adds_both_hooks_and_preserves_unrelated_entries(self):
        original = {"description": "user config", "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "echo keep"}]}]}}
        merged, changed = codex_hooks.merge(original)
        self.assertTrue(changed)
        self.assertEqual(merged["description"], "user config")
        self.assertEqual(merged["hooks"]["PreToolUse"], original["hooks"]["PreToolUse"])
        self.assertIn("context-warn.py", merged["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"])
        post = merged["hooks"]["PostToolUse"][0]
        self.assertEqual(post["matcher"], codex_hooks.INLINE_MATCHER)
        self.assertIn("delegation-warn.py", post["hooks"][0]["command"])

    def test_merge_is_idempotent(self):
        merged, _ = codex_hooks.merge({})
        merged_again, changed = codex_hooks.merge(merged)
        self.assertFalse(changed)
        self.assertEqual(merged_again, merged)

    def test_merge_refreshes_stale_paths_in_place(self):
        settings = {"hooks": {
            "UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": "python /claude/context-warn.py"}]}],
            "PostToolUse": [{"matcher": "Bash|Read|Edit|Write", "hooks": [
                {"type": "command", "command": "python /claude/delegation-warn.py"}]}],
        }}
        merged, changed = codex_hooks.merge(settings)
        self.assertTrue(changed)
        context_entries = merged["hooks"]["UserPromptSubmit"]
        delegation_entries = merged["hooks"]["PostToolUse"]
        self.assertEqual(len(context_entries), 1)
        self.assertEqual(context_entries[0]["hooks"][0]["command"],
                         codex_hooks._command(codex_hooks.HOOKS_DIR / "context-warn.py"))
        self.assertEqual(delegation_entries[0]["matcher"],
                         codex_hooks.INLINE_MATCHER)
        self.assertEqual(delegation_entries[0]["hooks"][0]["command"],
                         codex_hooks._command(codex_hooks.HOOKS_DIR / "delegation-warn.py"))

    def test_merge_adds_stop_and_subagent_stop_hooks(self):
        merged, _ = codex_hooks.merge({})
        stop = merged["hooks"]["Stop"]
        self.assertIn("delegation-warn.py", stop[0]["hooks"][0]["command"])
        sub = merged["hooks"]["SubagentStop"]
        self.assertIn("subagent-log.py", sub[0]["hooks"][0]["command"])
        self.assertEqual(codex_hooks.INLINE_MATCHER, "*")

    def test_merge_refuses_malformed_hooks_member(self):
        with self.assertRaises(ValueError):
            codex_hooks.merge({"hooks": "broken"})


class TestWindowsCommand(unittest.TestCase):
    """#73: Windows command must not start with a quoted Program Files path."""
    SCRIPT = Path("C:/tools/codex-hooks/context-warn.py")

    def _build(self, exe, short):
        from unittest import mock
        with (mock.patch.object(codex_hooks.sys, "platform", "win32"),
              mock.patch.object(codex_hooks.sys, "executable", exe),
              mock.patch.object(codex_hooks, "_short_path", short),
              mock.patch.object(codex_hooks.Path, "resolve", lambda self: self)):
            return codex_hooks._command(self.SCRIPT)

    def test_path_without_spaces_is_not_quoted(self):
        cmd = self._build("C:/Python313/python.exe", lambda p: p)
        self.assertFalse(cmd.startswith('"'))
        self.assertNotIn('"', cmd)

    def test_path_with_spaces_uses_short_path(self):
        cmd = self._build("C:/Program Files/Python313/python.exe",
                          lambda p: "C:/PROGRA~1/Python313/python.exe")
        self.assertTrue(cmd.startswith("C:/PROGRA~1/Python313/python.exe "))
        self.assertNotIn('"', cmd)

    def test_short_path_unavailable_falls_back_to_quoting(self):
        cmd = self._build("C:/Program Files/Python313/python.exe", lambda p: p)
        self.assertTrue(cmd.startswith('"'))

    def test_short_path_helper_without_ctypes_windll_returns_input(self):
        # On non-Windows hosts ctypes has no windll; on Windows a missing path is returned as-is.
        self.assertEqual(codex_hooks._short_path("Z:/definitely/missing path/x.exe"),
                         "Z:/definitely/missing path/x.exe")

    def test_stale_quoted_entries_are_rewritten_and_flagged(self):
        from unittest import mock
        expected = "C:/PROGRA~1/Python313/python.exe C:/x/context-warn.py"
        old = '"C:/Program Files/Python313/python.exe" C:/x/context-warn.py'
        with mock.patch.object(codex_hooks, "_command", lambda script: expected):
            settings = {"hooks": {"UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": old, "commandWindows": old}]}]}}
            merged, changed = codex_hooks.merge(settings)
            handler = merged["hooks"]["UserPromptSubmit"][0]["hooks"][0]
            self.assertTrue(changed)
            self.assertEqual(handler["command"], expected)
            self.assertEqual(handler["commandWindows"], expected)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "hooks.json"
                stale = {"hooks": {ev: [{"hooks": [{"type": "command", "command": expected,
                         "commandWindows": old}]}] for ev in codex_hooks.HOOKS}}
                path.write_text(json.dumps(stale), encoding="utf-8")
                self.assertEqual(codex_hooks.hooks_status(path)[0], "WARN")


class TestInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.hooks = self.tmp / ".codex" / "hooks.json"
        self.agents = self.tmp / ".codex" / "agents"

    def _run(self, install: bool):
        out = io.StringIO()
        with redirect_stdout(out):
            codex_hooks.cmd_codex_hooks(Namespace(install=install, hooks=str(self.hooks),
                                                  agents_dir=str(self.agents)))
        return out.getvalue()

    def test_install_creates_hooks_and_worker_files(self):
        output = self._run(True)
        self.assertIn("installed hooks", output)
        data = json.loads(self.hooks.read_text(encoding="utf-8"))
        self.assertIn("UserPromptSubmit", data["hooks"])
        self.assertIn("PostToolUse", data["hooks"])
        for role in ("worker-low", "worker-medium"):
            self.assertTrue((self.agents / f"{role}.toml").is_file())
        self.assertFalse(list(self.tmp.rglob("*.bak-*")))
        before = self.hooks.read_bytes()
        second = self._run(True)
        self.assertIn("hooks up to date", second)
        self.assertEqual(self.hooks.read_bytes(), before)
        self.assertFalse(list(self.tmp.rglob("*.bak-*")))

    def test_dry_run_does_not_write(self):
        output = self._run(False)
        self.assertIn("would update", output)
        self.assertFalse(self.hooks.exists())
        self.assertFalse(self.agents.exists())

    def test_install_backs_up_changed_hooks_and_keeps_custom_worker(self):
        self.hooks.parent.mkdir(parents=True)
        self.hooks.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash",
            "hooks": [{"type": "command", "command": "echo keep"}]}]}}), encoding="utf-8")
        self.agents.mkdir(parents=True)
        custom = self.agents / "worker-low.toml"
        custom.write_text("# local edit\n", encoding="utf-8")
        output = self._run(True)
        backups = list(self.hooks.parent.glob("hooks.json.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("echo keep", backups[0].read_text(encoding="utf-8"))
        self.assertIn("kept customized worker", output)
        self.assertEqual(custom.read_text(encoding="utf-8"), "# local edit\n")
        self.assertTrue((self.agents / "worker-medium.toml").exists())

    def test_install_updates_unedited_earlier_version(self):
        self.agents.mkdir(parents=True)
        old = self.agents / "worker-low.toml"
        old.write_text("# earlier shipped version\n", encoding="utf-8")
        with mock.patch("onboarding._shipped_blob_ids",
                        return_value={onboarding._blob_id(b"# earlier shipped version\n")}):
            output = self._run(True)
        shipped = (codex_hooks.AGENTS_DIR / "worker-low.toml").read_bytes()
        self.assertEqual(old.read_bytes(), shipped)
        self.assertIn("updated worker", output)
        self.assertNotIn("kept customized", output)

    def test_install_leaves_identical_worker_unchanged(self):
        self._run(True)
        output = self._run(True)
        self.assertNotIn("updated worker", output)
        self.assertNotIn("installed worker", output)
        self.assertNotIn("kept customized", output)

    def test_dry_run_writes_nothing_for_earlier_version(self):
        self.agents.mkdir(parents=True)
        old = self.agents / "worker-low.toml"
        old.write_text("# earlier shipped version\n", encoding="utf-8")
        with mock.patch("onboarding._shipped_blob_ids",
                        return_value={onboarding._blob_id(b"# earlier shipped version\n")}):
            output = self._run(False)
        self.assertIn("update unedited workers: worker-low.toml", output)
        self.assertEqual(old.read_text(encoding="utf-8"), "# earlier shipped version\n")
        self.assertFalse(self.hooks.exists())

    def test_refreshable_workers(self):
        self.assertEqual(codex_hooks.refreshable_workers(self.agents), [])  # Codex not set up
        self.agents.mkdir(parents=True)
        self.assertEqual(codex_hooks.refreshable_workers(self.agents), [])
        shipped = sorted(codex_hooks.AGENTS_DIR.glob("worker-*.toml"))
        (self.agents / shipped[0].name).write_bytes(shipped[0].read_bytes())
        (self.agents / shipped[1].name).write_text("# my own edit\n", encoding="utf-8")
        self.assertEqual(codex_hooks.refreshable_workers(self.agents), [])  # custom kept
        (self.agents / shipped[1].name).unlink()
        self.assertEqual(codex_hooks.refreshable_workers(self.agents), [shipped[1].name])

    def test_agents_only_leaves_hooks_alone(self):
        out = io.StringIO()
        with redirect_stdout(out):
            codex_hooks.cmd_codex_hooks(Namespace(install=True, hooks=str(self.hooks),
                                                  agents_dir=str(self.agents), agents_only=True))
        self.assertFalse(self.hooks.exists())
        self.assertTrue((self.agents / "worker-low.toml").is_file())

    def test_invalid_json_is_not_overwritten(self):
        self.hooks.parent.mkdir(parents=True)
        self.hooks.write_text("{broken", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self._run(True)
        self.assertEqual(self.hooks.read_text(encoding="utf-8"), "{broken")


class TestStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_hooks_status_warns_then_passes_after_install(self):
        hooks = self.tmp / "hooks.json"
        self.assertEqual(codex_hooks.hooks_status(hooks)[0], "WARN")
        codex_hooks.cmd_codex_hooks(Namespace(install=True, hooks=str(hooks),
                                               agents_dir=str(self.tmp / "agents")))
        self.assertEqual(codex_hooks.hooks_status(hooks)[0], "OK")

    def test_agent_guard_registered_and_missing_guard_is_stale(self):
        merged, _ = codex_hooks.merge({})
        pre = merged["hooks"]["PreToolUse"][0]
        self.assertEqual(pre["matcher"], codex_hooks.SPAWN_MATCHER)
        self.assertIn("agent-guard.py", pre["hooks"][0]["command"])
        hooks = self.tmp / "hooks.json"
        codex_hooks.cmd_codex_hooks(Namespace(install=True, hooks=str(hooks),
                                               agents_dir=str(self.tmp / "agents")))
        data = json.loads(hooks.read_text(encoding="utf-8"))
        del data["hooks"]["PreToolUse"]
        hooks.write_text(json.dumps(data), encoding="utf-8")
        status, message = codex_hooks.hooks_status(hooks)
        self.assertEqual(status, "WARN")
        self.assertIn("agent-guard.py", message)

    def test_hooks_status_flags_stale_matcher(self):
        hooks = self.tmp / "hooks.json"
        codex_hooks.cmd_codex_hooks(Namespace(install=True, hooks=str(hooks),
                                               agents_dir=str(self.tmp / "agents")))
        data = json.loads(hooks.read_text(encoding="utf-8"))
        data["hooks"]["PostToolUse"][0]["matcher"] = "Bash|Read"
        hooks.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(codex_hooks.hooks_status(hooks)[0], "WARN")

    def test_model_status_checks_root_model_and_effort(self):
        config = self.tmp / "config.toml"
        config.write_text('model = "gpt-6-sol"\nmodel_reasoning_effort = "high"\n', encoding="utf-8")
        self.assertEqual(codex_hooks.model_status(config)[0], "OK")
        config.write_text('model = "gpt-6-sol"\n[other]\nmodel_reasoning_effort = "high"\n',
                          encoding="utf-8")
        self.assertEqual(codex_hooks.model_status(config)[0], "WARN")

    def test_worker_status_detects_missing_and_custom_files(self):
        agents = self.tmp / "agents"
        self.assertEqual(codex_hooks.workers_status(agents)[0], "WARN")
        agents.mkdir()
        for src in codex_hooks.AGENTS_DIR.glob("worker-*.toml"):
            (agents / src.name).write_bytes(src.read_bytes())
        self.assertEqual(codex_hooks.workers_status(agents)[0], "OK")
        (agents / "worker-low.toml").write_text("custom", encoding="utf-8")
        self.assertEqual(codex_hooks.workers_status(agents)[0], "WARN")


class TestCodexHomeEnv(unittest.TestCase):
    """Install and doctor checks follow CODEX_HOME, like user_config."""

    def test_default_paths_follow_codex_home(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "ch"
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                self.assertEqual(codex_hooks.hooks_status()[0], "WARN")
                self.assertIn(str(home), codex_hooks.hooks_status()[1])
                with redirect_stdout(io.StringIO()):
                    codex_hooks.cmd_codex_hooks(Namespace(install=True, hooks=None, agents_dir=None))
                self.assertTrue((home / "hooks.json").is_file())
                self.assertTrue((home / "agents" / "worker-low.toml").is_file())
                self.assertEqual(codex_hooks.hooks_status()[0], "OK")
                self.assertEqual(codex_hooks.workers_status()[0], "OK")
                self.assertEqual(codex_hooks.model_status()[0], "WARN")
                self.assertIn(str(home), codex_hooks.model_status()[1])


class TestDenyProbe(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        self.mock = mock
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        (self.data / ".graph").mkdir(parents=True)

    def _fake_run(self, version="codex-cli 1.2.3", fire=True, write=False, timeout=False):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[1:] == ["--version"]:
                return types.SimpleNamespace(returncode=0, stdout=version + "\n", stderr="")
            if cmd[1] == "exec":
                if timeout:
                    raise subprocess.TimeoutExpired(cmd, 1)
                cwd = Path(kw["cwd"])
                self.assertTrue((cwd / ".codex" / "hooks.json").is_file())
                if fire:
                    (cwd / "fired.log").write_text("fired")
                if write:
                    (cwd / "probe.txt").write_text("x")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return run, calls

    def _probe(self, **kw):
        run, calls = self._fake_run(**kw)
        buf = io.StringIO()
        with self.mock.patch("subprocess.run", side_effect=run), redirect_stdout(buf):
            result = codex_hooks.probe_deny("codex", self.data)
        return result, buf.getvalue(), calls

    def _machine(self):
        return json.loads((self.data / ".graph" / "machine.json").read_text(encoding="utf-8"))

    def test_enforced_records_version(self):
        result, out, calls = self._probe()
        self.assertEqual(result, "enforced")
        self.assertIn("blocked", out)
        self.assertEqual(self._machine()["codex_deny_tested_version"], "codex-cli 1.2.3")
        self.assertEqual(sum(1 for c in calls if c[1:2] == ["exec"]), 1)

    def test_not_enforced_when_file_written(self):
        result, out, _ = self._probe(write=True)
        self.assertEqual(result, "not-enforced")
        self.assertIn("NOT enforced", out)

    def test_hook_not_fired_is_inconclusive_and_recorded(self):
        result, out, _ = self._probe(fire=False)
        self.assertEqual(result, "inconclusive")
        self.assertIn("trust", out)
        self.assertEqual(self._machine()["codex_deny_tested_version"], "codex-cli 1.2.3")
        self.assertEqual(self._machine()["codex_deny_tested_result"], "inconclusive")
        with self.mock.patch("subprocess.run", side_effect=self._fake_run()[0]):
            self.assertIsNone(codex_hooks.deny_retest_status("codex", self.data))

    def test_ack_records_acknowledged(self):
        run, _ = self._fake_run()
        buf = io.StringIO()
        with self.mock.patch("subprocess.run", side_effect=run), redirect_stdout(buf):
            self.assertEqual(codex_hooks.ack_deny("codex", self.data), "acknowledged")
            self.assertIsNone(codex_hooks.deny_retest_status("codex", self.data))
        self.assertIn("acknowledged", buf.getvalue())
        self.assertEqual(self._machine()["codex_deny_tested_version"], "codex-cli 1.2.3")
        self.assertEqual(self._machine()["codex_deny_tested_result"], "acknowledged")

    def test_ack_without_codex_records_nothing(self):
        with self.mock.patch("shutil.which", return_value=None), \
             self.mock.patch("subprocess.run", side_effect=OSError("nope")), \
             redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(codex_hooks.ack_deny(None, self.data), "skipped")
            self.assertEqual(codex_hooks.ack_deny("codex", self.data), "skipped")
        self.assertIn("nothing recorded", buf.getvalue())
        self.assertFalse((self.data / ".graph" / "machine.json").exists())

    def test_retest_status_mentions_last_result(self):
        run, _ = self._fake_run()
        with self.mock.patch("subprocess.run", side_effect=run):
            self.assertIn("no deny check recorded", codex_hooks.deny_retest_status("codex", self.data)[1])
            codex_hooks.record_tested_version("codex-cli 1.0.0", self.data, "inconclusive")
            level, text = codex_hooks.deny_retest_status("codex", self.data)
            self.assertEqual(level, "INFO")
            self.assertNotIn("\n", text)
            for part in ("codex-cli 1.0.0 = inconclusive", "codex-cli 1.2.3", "--probe-deny", "--ack-deny"):
                self.assertIn(part, text)

    def test_retest_status_old_machine_json_without_result(self):
        (self.data / ".graph" / "machine.json").write_text(
            json.dumps({"codex_deny_tested_version": "codex-cli 1.0.0"}), encoding="utf-8")
        run, _ = self._fake_run()
        with self.mock.patch("subprocess.run", side_effect=run):
            self.assertIn("codex-cli 1.0.0 = unknown", codex_hooks.deny_retest_status("codex", self.data)[1])
            codex_hooks.record_tested_version("codex-cli 1.2.3", self.data)
            self.assertIsNone(codex_hooks.deny_retest_status("codex", self.data))

    def test_not_enforced_records_version(self):
        self._probe(write=True)
        self.assertEqual(self._machine()["codex_deny_tested_version"], "codex-cli 1.2.3")

    def test_matchers_cover_real_codex_tool_names(self):
        import re
        spawn = re.compile(codex_hooks.SPAWN_MATCHER)
        for name in ("Agent", "spawn_agent", "collaboration.spawn_agent", "collaboration__spawn_agent"):
            self.assertTrue(spawn.search(name), name)
        for name in ("exec", "exec_command", "wait_agent", "followup_task", "spawn_agent_x"):
            self.assertFalse(spawn.search(name), name)
        # every tool is matched; delegation-warn.py itself skips exec/spawn tools
        self.assertEqual(codex_hooks.INLINE_MATCHER, "*")
        self.assertIn("|exec)", codex_hooks.PROBE_MATCHER)

    def test_run_error_records_nothing(self):
        with self.mock.patch("subprocess.run", side_effect=OSError("boom")), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(codex_hooks.probe_deny("codex", self.data), "inconclusive")
        self.assertFalse((self.data / ".graph" / "machine.json").exists())

    def test_timeout_records_nothing(self):
        result, _out, _ = self._probe(timeout=True)
        self.assertEqual(result, "inconclusive")
        self.assertFalse((self.data / ".graph" / "machine.json").exists())

    def test_skipped_without_codex(self):
        with self.mock.patch("shutil.which", return_value=None), \
             self.mock.patch("subprocess.run") as run, redirect_stdout(io.StringIO()):
            self.assertEqual(codex_hooks.probe_deny(None, self.data), "skipped")
        run.assert_not_called()

    def test_retest_status(self):
        run, _ = self._fake_run()
        with self.mock.patch("subprocess.run", side_effect=run):
            status = codex_hooks.deny_retest_status("codex", self.data)
            self.assertEqual(status[0], "INFO")
            self.assertIn("codex-hooks --probe-deny", status[1])
            codex_hooks.record_tested_version("codex-cli 1.2.3", self.data)
            self.assertIsNone(codex_hooks.deny_retest_status("codex", self.data))
            codex_hooks.record_tested_version("codex-cli 1.0.0", self.data)
            self.assertEqual(codex_hooks.deny_retest_status("codex", self.data)[0], "INFO")

    def test_retest_status_silent_when_codex_cannot_run(self):
        with self.mock.patch("subprocess.run", side_effect=OSError("nope")):
            self.assertIsNone(codex_hooks.deny_retest_status("codex", self.data))


if __name__ == "__main__":
    unittest.main()
