"""Tests for $CLAUDE_CONFIG_DIR support (issue #47) and the registry of Claude
Code config dirs (tools/claude_dirs.py). Only tempfile dirs. Named so it sorts
after the test modules that import the tool modules first (those bind `graph`
once at import)."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

for _var in ("VAULT_DATA", "VAULT_HOME", "CLAUDE_CONFIG_DIR"):
    os.environ.pop(_var, None)


def _no_registry(*_args):
    raise OSError("tests never read the real registry")


sys.modules["winreg"] = types.SimpleNamespace(HKEY_CURRENT_USER=None, OpenKey=_no_registry)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"

if "graph" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("graph", TOOLS_DIR / "graph.py")
    _graph = importlib.util.module_from_spec(_spec)
    sys.modules["graph"] = _graph
    _spec.loader.exec_module(_graph)

sys.path.insert(0, str(TOOLS_DIR))
import claude_dirs  # noqa: E402
import claude_hooks  # noqa: E402
import models  # noqa: E402
import onboarding  # noqa: E402
import update  # noqa: E402

graph = claude_dirs.g  # the graph module the tool modules are bound to

_sl_spec = importlib.util.spec_from_file_location(
    "statusline_cfgdir", TOOLS_DIR / "claude-hooks" / "statusline.py")
statusline = importlib.util.module_from_spec(_sl_spec)
_sl_spec.loader.exec_module(statusline)


class ConfigDirCase(unittest.TestCase):
    """CLAUDE_CONFIG_DIR points at a temp dir; the fake home has no ~/.claude
    unless a test creates it; VAULT_DATA is a temp dir (it holds the registry)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.cfg = self.tmp / "custom-claude"
        self.cfg.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "data"
        (self.data / ".graph").mkdir(parents=True)
        (self.data / "vault.config.json").write_text(json.dumps({"schema": 1}), encoding="utf-8")
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg),
                                           "VAULT_DATA": str(self.data)})
        env.start()
        self.addCleanup(env.stop)
        home = mock.patch("pathlib.Path.home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)

    def _env_without(self, *names):
        return {k: v for k, v in os.environ.items() if k not in names}

    def _home_claude(self) -> Path:
        (self.home / ".claude").mkdir(exist_ok=True)
        return self.home / ".claude"

    def _machine(self) -> dict:
        return json.loads((self.data / ".graph" / "machine.json").read_text(encoding="utf-8"))

    def _settings(self, d: Path) -> dict:
        return json.loads((d / "settings.json").read_text(encoding="utf-8"))


class TestHelper(ConfigDirCase):
    def test_env_wins(self):
        self.assertEqual(graph.claude_config_dir(), self.cfg)

    def test_falls_back_to_home_when_unset_or_blank(self):
        for value in (None, "", "  "):
            env = self._env_without("CLAUDE_CONFIG_DIR")
            if value is not None:
                env["CLAUDE_CONFIG_DIR"] = value
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(graph.claude_config_dir(), self.home / ".claude")


class TestHooksAndModels(ConfigDirCase):
    def _install(self):
        with redirect_stdout(io.StringIO()):
            claude_hooks.cmd_claude_hooks(Namespace(install=True, settings=None))

    def _models_ns(self, **kw):
        base = dict(orchestrator="sonnet", effort=None, worker_low="haiku",
                    worker_low_effort=None, worker_medium="haiku", worker_medium_effort=None,
                    this_computer=False, apply=True, data=str(self.data), settings=None,
                    agents_dir=None)
        base.update(kw)
        return Namespace(**base)

    def test_claude_hooks_install_writes_to_config_dir(self):
        self._install()
        self.assertTrue((self.cfg / "settings.json").exists())
        self.assertFalse((self.home / ".claude" / "settings.json").exists())

    def test_claude_hooks_status_reads_config_dir(self):
        self._install()
        level, message = claude_hooks.status()
        self.assertEqual(level, "OK")
        self.assertIn(str(self.cfg), message)

    def test_models_status_reads_config_dir(self):
        (self._home_claude() / "settings.json").write_text("{}", encoding="utf-8")
        self.assertIsNone(models.status(self.data))  # only the fallback dir has settings
        (self.cfg / "settings.json").write_text("{}", encoding="utf-8")
        self.assertIsNotNone(models.status(self.data))

    def test_models_apply_defaults_to_config_dir(self):
        with redirect_stdout(io.StringIO()):
            models.cmd_models(self._models_ns())
        self.assertTrue((self.cfg / "settings.json").exists())
        self.assertFalse((self.home / ".claude" / "settings.json").exists())

    def test_install_and_models_apply_to_every_registered_dir(self):
        other = self.tmp / "corp"
        other.mkdir()
        claude_dirs.add(other)
        home_claude = self._home_claude()
        self._install()
        for d in (self.cfg, other, home_claude):
            self.assertIn("hooks", self._settings(d))
        with redirect_stdout(io.StringIO()):
            models.cmd_models(self._models_ns())
        for d in (self.cfg, other, home_claude):
            self.assertIn("model", self._settings(d))

    def test_explicit_settings_targets_only_that_file(self):
        self._home_claude()
        only = self.tmp / "only.json"
        with redirect_stdout(io.StringIO()):
            claude_hooks.cmd_claude_hooks(Namespace(install=True, settings=str(only)))
        self.assertTrue(only.exists())
        self.assertFalse((self.cfg / "settings.json").exists())
        self.assertFalse((self.home / ".claude" / "settings.json").exists())


class TestAgentsAndUserLevel(ConfigDirCase):
    def test_setup_agents_writes_to_config_dir(self):
        with redirect_stdout(io.StringIO()):
            onboarding._setup_agents()
        self.assertTrue(any((self.cfg / "agents").glob("*.md")))
        self.assertFalse((self.home / ".claude" / "agents").exists())
        self.assertEqual(onboarding.stale_claude_agents(), [])

    def test_setup_agents_writes_to_every_registered_dir(self):
        home_claude = self._home_claude()
        with redirect_stdout(io.StringIO()):
            onboarding._setup_agents()
        self.assertTrue(any((home_claude / "agents").glob("*.md")))
        self.assertTrue(any((self.cfg / "agents").glob("*.md")))

    def test_user_level_claude_md_in_config_dir(self):
        with redirect_stdout(io.StringIO()):
            onboarding._setup_user_level(self.data)
        self.assertTrue((self.cfg / "CLAUDE.md").exists())
        self.assertFalse((self.home / ".claude" / "CLAUDE.md").exists())

    def test_update_agents_step_checks_registered_dirs(self):
        engine = self.tmp / "engine"
        (engine / "tools" / "claude-agents").mkdir(parents=True)
        args = types.SimpleNamespace(yes=True, agents=True)
        done = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        seen = []
        fake = types.SimpleNamespace(
            stale_claude_agents=lambda dest_dir: seen.append(dest_dir) or ["worker-low.md"])
        home_claude = self._home_claude()
        with mock.patch.dict(sys.modules, {"onboarding": fake}), \
             mock.patch("update.run_step", return_value=done) as m, \
             redirect_stdout(io.StringIO()):
            update._agents_step(engine, self.data, args)
            self.assertEqual(m.call_count, 1)
            self.assertEqual({p.parent for p in seen}, {self.cfg, home_claude})
            m.reset_mock()
            shutil.rmtree(self.cfg)
            shutil.rmtree(home_claude)  # no registered dir exists any more
            update._agents_step(engine, self.data, args)
            m.assert_not_called()


class TestRegistry(ConfigDirCase):
    def test_add_remove_and_dedupe(self):
        other = self.tmp / "corp"
        other.mkdir()
        self.assertTrue(claude_dirs.add(other))
        self.assertFalse(claude_dirs.add(other))
        self.assertIn(str(other), self._machine()["claude_dirs"])
        self.assertIn(other, claude_dirs.registered())
        self.assertTrue(claude_dirs.remove(other))
        self.assertFalse(claude_dirs.remove(other))
        self.assertNotIn(other, claude_dirs.registered())

    def test_registry_keeps_other_machine_json_keys(self):
        (self.data / ".graph" / "machine.json").write_text(json.dumps({"machine": "x"}),
                                                            encoding="utf-8")
        claude_dirs.add(self.tmp)
        self.assertEqual(self._machine()["machine"], "x")

    def test_default_dir_is_implicit_and_not_removable(self):
        home_claude = self._home_claude()
        self.assertIn(home_claude, claude_dirs.registered())
        self.assertFalse(claude_dirs.add(home_claude))
        with self.assertRaises(SystemExit):
            claude_dirs.cmd_claude_dirs(Namespace(add=None, remove=str(home_claude), ask=False))

    def test_env_dir_is_recorded_by_engine_commands(self):
        self.assertTrue(claude_dirs.record_env())
        self.assertEqual(self._machine()["claude_dirs"], [str(self.cfg)])
        with mock.patch.dict(os.environ, self._env_without("CLAUDE_CONFIG_DIR"), clear=True):
            self.assertIn(self.cfg, claude_dirs.registered())  # remembered without the env var

    def test_record_env_without_data_dir_is_quiet(self):
        env = self._env_without("VAULT_DATA", "VAULT_HOME")
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(graph, "saved_data_dir", return_value=None):
            self.assertFalse(claude_dirs.record_env())

    def test_graph_main_records_env(self):
        with mock.patch.object(sys, "argv", ["graph.py", "claude-dirs"]), \
             redirect_stdout(io.StringIO()):
            graph.main()
        self.assertEqual(self._machine()["claude_dirs"], [str(self.cfg)])


class TestCandidates(ConfigDirCase):
    def _make(self, *parts, settings=True):
        d = self.home.joinpath(*parts)
        d.mkdir(parents=True)
        if settings:
            (d / "settings.json").write_text("{}", encoding="utf-8")
        return d

    def test_scan_finds_unconfirmed_dirs_one_and_two_levels_deep(self):
        one = self._make(".claude-work")
        two = self._make(".claude-corp", "claude-config")
        projects_only = self._make(".claude-x", settings=False)
        (projects_only / "projects").mkdir()
        self._make(".claude-empty", settings=False)
        self._make(".config", "claude")  # not a .claude* name
        self._make(".claude", "sub")  # inside the default dir: not a candidate
        self.assertEqual(set(claude_dirs.candidates()), {one, two, projects_only})

    def test_registered_dirs_are_not_candidates_and_nothing_is_auto_added(self):
        one = self._make(".claude-work")
        two = self._make(".claude-corp", "claude-config")
        claude_dirs.add(one)
        self.assertEqual(claude_dirs.candidates(), [two])
        self.assertNotIn(str(two), self._machine()["claude_dirs"])

    def test_ask_prints_question_and_registers_nothing(self):
        two = self._make(".claude-corp", "claude-config")
        with redirect_stdout(io.StringIO()) as buf:
            claude_dirs.cmd_claude_dirs(Namespace(add=None, remove=None, ask=True))
        self.assertIn(str(two), buf.getvalue())
        self.assertIn("claude-dirs --add", buf.getvalue())
        self.assertFalse((self.data / ".graph" / "machine.json").exists())

    def test_list_shows_sources(self):
        two = self._make(".claude-corp", "claude-config")
        with redirect_stdout(io.StringIO()) as buf:
            claude_dirs.cmd_claude_dirs(Namespace(add=None, remove=None, ask=False))
        out = buf.getvalue()
        self.assertIn(f"{self.cfg}  (environment)", out)
        self.assertIn(str(two), out)

    def test_non_interactive_install_prints_notice_and_skips_candidates(self):
        two = self._make(".claude-corp", "claude-config")
        with mock.patch.object(graph, "stdin_is_interactive", return_value=False), \
             redirect_stdout(io.StringIO()) as buf:
            claude_hooks.cmd_claude_hooks(Namespace(install=True, settings=None))
        self.assertIn("not registered", buf.getvalue())
        self.assertIn("claude-dirs --add", buf.getvalue())
        self.assertIn("hooks", self._settings(self.cfg))
        self.assertNotIn("hooks", self._settings(two))

    def test_interactive_prompt_registers_the_choice(self):
        one = self._make(".claude-a")
        two = self._make(".claude-b")
        with mock.patch.object(graph, "stdin_is_interactive", return_value=True), \
             mock.patch("builtins.input", return_value="3"), redirect_stdout(io.StringIO()):
            claude_hooks.cmd_claude_hooks(Namespace(install=True, settings=None))
        self.assertEqual(set(self._machine()["claude_dirs"]), {str(one), str(two)})
        for d in (one, two):
            self.assertIn("hooks", self._settings(d))


class TestDoctor(ConfigDirCase):
    def _doctor(self) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                onboarding.cmd_doctor(Namespace(data=str(self.data)))
            except SystemExit:
                pass
        return buf.getvalue()

    def test_reports_each_registered_dir(self):
        home_claude = self._home_claude()
        out = self._doctor()
        self.assertIn(f"Claude config dir: {self.cfg} (environment)", out)
        self.assertIn(f"Claude config dir: {home_claude} (default)", out)
        self.assertIn(f"claude-agents out of date in {self.cfg / 'agents'}", out)
        self.assertIn(f"claude-agents out of date in {home_claude / 'agents'}", out)

    def test_warns_about_unconfirmed_candidates(self):
        d = self.home / ".claude-corp" / "claude-config"
        d.mkdir(parents=True)
        (d / "settings.json").write_text("{}", encoding="utf-8")
        out = self._doctor()
        self.assertRegex(out, r"WARN.*unconfirmed Claude config dirs: .*claude-config")
        self.assertIn("claude-dirs --add", out)


class TestStatusline(ConfigDirCase):
    def test_settings_path_honors_config_dir(self):
        with mock.patch.dict(os.environ, self._env_without(statusline.SETTINGS_ENV_VAR), clear=True):
            self.assertEqual(Path(statusline._settings_path()), self.cfg / "settings.json")
        env = self._env_without(statusline.SETTINGS_ENV_VAR, "CLAUDE_CONFIG_DIR")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(Path(statusline._settings_path()),
                             Path(os.path.expanduser("~")) / ".claude" / "settings.json")


class TestStatsDefault(ConfigDirCase):
    def test_default_projects_dir_follows_config_dir(self):
        import token_stats
        args = Namespace(tokens=True, projects_dir=None, codex_dir=str(self.tmp / "none"),
                         since=None, top=5, json=False)
        with mock.patch.object(token_stats, "run", return_value="x") as run, \
             redirect_stdout(io.StringIO()):
            graph.cmd_stats(args)
        self.assertEqual(run.call_args.args[0], self.cfg / "projects")


if __name__ == "__main__":
    unittest.main()
