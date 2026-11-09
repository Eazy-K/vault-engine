"""Tests for task 0029: `update --all` brings everything up to date in one command
(engine, user config, vault AGENTS.md) and leaves nothing to run by hand.

Standard library `unittest` only. Everything lives in temp dirs: HOME, USERPROFILE,
CODEX_HOME and CLAUDE_CONFIG_DIR point there, VAULT_DATA is cleared, and
`update.run_step` is always mocked, so no real ~/.claude, ~/.codex or vault is read
or written and no `graph.py` subprocess runs.
"""
from __future__ import annotations

import argparse
import atexit
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest import mock

for _var in ("VAULT_DATA", "VAULT_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
    os.environ.pop(_var, None)


def _no_registry(*_args):
    raise OSError("tests never read the real registry")


sys.modules.setdefault("winreg", types.SimpleNamespace(HKEY_CURRENT_USER=None, OpenKey=_no_registry))

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
GRAPH_PATH = TOOLS_DIR / "graph.py"

if "graph" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
    _graph = importlib.util.module_from_spec(_spec)
    sys.modules["graph"] = _graph
    _spec.loader.exec_module(_graph)

sys.path.insert(0, str(TOOLS_DIR))
import update  # noqa: E402
import user_config as uc  # noqa: E402

# Other test modules re-execute graph.py; patch whichever graph objects the
# modules under test actually hold.
graph = update.g
GRAPHS = {id(m): m for m in (update.g, uc.g)}.values()

OK = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", check=True)


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], root)
    _run(["git", "config", "user.email", "tester@example.com"], root)
    _run(["git", "config", "user.name", "Test Runner"], root)
    _run(["git", "config", "commit.gpgsign", "false"], root)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _commit(root: Path, message: str) -> str:
    _run(["git", "add", "-A"], root)
    _run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", message], root)
    return _run(["git", "rev-parse", "HEAD"], root).stdout.strip()


def _build_origin(tmp: Path) -> dict:
    """A bare origin with one tagged commit (v0.1.0) whose template is
    'AGENTS v1' (no revision line, so a differing vault file counts as stale)."""
    seed = tmp / "seed"
    _init_repo(seed)
    _write(seed / "CHANGELOG.md", "# Changelog\n\n## [0.1.0] - 2026-01-01\nfirst\n")
    _write(seed / "templates" / "AGENTS.md", "AGENTS v1\ncontent-a\n")
    _write(seed / "tools" / "graph.py", "# stub\n")
    _commit(seed, "v0.1.0")
    _run(["git", "tag", "v0.1.0"], seed)
    bare = tmp / "origin.git"
    _run(["git", "init", "-q", "--bare", str(bare)], tmp)
    _run(["git", "push", "-q", str(bare), "HEAD", "--tags"], seed)
    branch = _run(["git", "branch", "--show-current"], seed).stdout.strip()
    _run(["git", "symbolic-ref", "HEAD", f"refs/heads/{branch}"], bare)
    return {"bare": bare, "branch": branch}


_TEMPLATE: dict = {}


def _template() -> dict:
    if not _TEMPLATE:
        root = Path(tempfile.mkdtemp())
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        origin = _build_origin(root)
        engine = root / "engine"
        _run(["git", "clone", "-q", str(origin["bare"]), str(engine)], root)
        _run(["git", "config", "user.email", "tester@example.com"], engine)
        _run(["git", "config", "user.name", "Test Runner"], engine)
        _run(["git", "checkout", "-q", "v0.1.0"], engine)
        _TEMPLATE.update(root=root, branch=origin["branch"])
    return _TEMPLATE


class Args:
    def __init__(self, **kw):
        self.check = False
        self.to = "v0.1.0"  # == the checkout: the "already up to date" path
        self.yes = False
        self.apply_agents = False
        self.all = False
        self.__dict__.update(kw)


class UpdateAllCase(unittest.TestCase):
    """A stable engine clone at v0.1.0, a git data repo, and a private HOME with
    CODEX_HOME / CLAUDE_CONFIG_DIR (created or not, per test)."""

    make_codex = True
    make_claude = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        template = _template()
        shutil.copytree(template["root"], self.tmp, dirs_exist_ok=True)
        self.engine = self.tmp / "engine"
        _run(["git", "remote", "set-url", "origin", str(self.tmp / "origin.git")], self.engine)

        self.data = self.tmp / "data"
        _init_repo(self.data)
        self.agents = self.data / "AGENTS.md"
        _write(self.agents, "AGENTS v1\ncontent-a\n")
        _commit(self.data, "initial")
        self.paths = graph.Paths(self.engine, self.data)

        self.home = self.tmp / "home"
        self.codex = self.home / ".codex"
        self.claude = self.home / ".claude"
        self.home.mkdir()
        if self.make_codex:
            self.codex.mkdir()
        if self.make_claude:
            self.claude.mkdir()
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home),
               "CODEX_HOME": str(self.codex), "CLAUDE_CONFIG_DIR": str(self.claude)}
        patchers = [mock.patch.dict(os.environ, env),
                    mock.patch.dict(sys.modules, {"user_config": uc})]
        for g in GRAPHS:
            patchers += [mock.patch.object(g, "ENGINE", self.engine),
                         mock.patch.object(g, "default_paths", return_value=self.paths),
                         mock.patch.object(g, "__version__", "0.1.0"),
                         mock.patch.object(g, "stdin_is_interactive", return_value=False)]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("VAULT_DATA", None)
        self.codex_hooks = mock.patch.dict(sys.modules, {"codex_hooks": None})
        self.codex_hooks.start()
        self.addCleanup(self.codex_hooks.stop)

    # -- helpers --------------------------------------------------------------

    def put_sources(self):
        """Vault templates that make Claude settings and Codex rules drift, with
        user files already present so a backup is expected."""
        _write(self.data / uc.SETTINGS_SRC, '{"a": 1}')
        _write(self.data / uc.RULES_SRC, 'prefix_rule(pattern=["ls"], decision="allow")\n')
        if self.claude.is_dir():
            _write(self.claude / "settings.json", '{"b": 2}')
        if self.codex.is_dir():
            _write(self.codex / "rules" / "default.rules", "# mine\n")

    def run_update(self, **kw):
        """(stdout, SystemExit code or None, run_step command list)."""
        code = None
        with mock.patch("update.run_step", return_value=OK) as steps, \
                redirect_stdout(StringIO()) as buf:
            try:
                update.cmd_update(Args(**kw))
            except SystemExit as exc:
                code = exc.code
        return buf.getvalue(), code, [c.args[2][0] for c in steps.call_args_list]

    def backups(self, directory: Path) -> list[Path]:
        return sorted(directory.rglob("*.bak-*"))

    def log(self) -> list[str]:
        return _run(["git", "log", "--format=%s"], self.data).stdout.splitlines()

    def all_command(self) -> str:
        return update.update_all_command(self.engine)


class TestUserConfigStep(UpdateAllCase):
    def test_all_writes_what_user_config_install_writes_with_backups(self):
        self.put_sources()
        expected = {item.target: item.new_bytes
                    for item in uc.evaluate(self.paths, only_existing_homes=True)
                    if item.status == "drift"}
        self.assertEqual(len(expected), 2)
        out, code, _ = self.run_update(all=True, yes=True)
        self.assertIsNone(code, out)
        for target, new_bytes in expected.items():
            self.assertEqual(target.read_bytes(), new_bytes)
        self.assertEqual(len(self.backups(self.claude)), 1)
        self.assertEqual(len(self.backups(self.codex)), 1)
        self.assertEqual(self.backups(self.claude)[0].read_text(encoding="utf-8"), '{"b": 2}')
        self.assertEqual(self.backups(self.codex)[0].read_text(encoding="utf-8"), "# mine\n")
        self.assertIn("(backup:", out)

    def test_all_matches_the_standalone_command_on_the_same_inputs(self):
        self.put_sources()
        items = uc.evaluate(self.paths, only_existing_homes=True)
        via_cmd = {i.target: i.new_bytes for i in items if i.status == "drift"}
        self.run_update(all=True, yes=True)
        self.assertEqual({t: t.read_bytes() for t in via_cmd}, via_cmd)
        again = uc.evaluate(self.paths, only_existing_homes=True)
        self.assertTrue(all(i.status == "ok" for i in again))

    def test_missing_home_dirs_are_not_created_and_do_not_fail(self):
        for d in (self.codex, self.claude):
            shutil.rmtree(d)
        self.put_sources()
        out, code, _ = self.run_update(all=True, yes=True)
        self.assertIsNone(code, out)
        self.assertFalse(self.codex.exists())
        self.assertFalse(self.claude.exists())
        self.assertNotIn("manual:", out)

    def test_only_the_existing_home_is_written(self):
        shutil.rmtree(self.codex)
        self.put_sources()
        self.run_update(all=True, yes=True)
        self.assertFalse(self.codex.exists())
        self.assertEqual((self.claude / "settings.json").read_text(encoding="utf-8").count("a"), 1)

    def test_yes_alone_writes_nothing_and_prints_the_all_command(self):
        self.put_sources()
        out, code, _ = self.run_update(yes=True)
        self.assertIsNone(code)
        self.assertEqual((self.claude / "settings.json").read_text(encoding="utf-8"), '{"b": 2}')
        self.assertEqual(self.backups(self.claude), [])
        self.assertIn(self.all_command(), out)

    def test_non_interactive_plain_update_prints_the_all_command(self):
        self.put_sources()
        out, code, _ = self.run_update()
        self.assertIsNone(code)
        self.assertEqual((self.codex / "rules" / "default.rules").read_text(encoding="utf-8"), "# mine\n")
        self.assertIn(self.all_command(), out)

    def test_interactive_plain_update_asks_and_writes_on_yes(self):
        self.put_sources()
        for g in GRAPHS:
            patcher = mock.patch.object(g, "stdin_is_interactive", return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        with mock.patch("builtins.input", return_value="y") as ask:
            out, code, _ = self.run_update()
        self.assertIsNone(code, out)
        ask.assert_called_once()
        self.assertEqual(len(self.backups(self.claude)), 1)

    def test_interactive_plain_update_declined_writes_nothing(self):
        self.put_sources()
        for g in GRAPHS:
            patcher = mock.patch.object(g, "stdin_is_interactive", return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        with mock.patch("builtins.input", return_value="n"):
            out, _code, _ = self.run_update()
        self.assertEqual(self.backups(self.claude), [])
        self.assertIn(self.all_command(), out)

    def test_error_item_prints_manual_runs_doctor_and_exits_1(self):
        # Python < 3.11 (no tomllib): a Codex config.toml source cannot be merged.
        self.put_sources()
        _write(self.data / uc.CONFIG_SRC, 'model = "x"\n')
        with mock.patch.object(uc, "tomllib", None):
            out, code, steps = self.run_update(all=True, yes=True)
        self.assertEqual(code, 1)
        self.assertIn("manual:", out)
        self.assertIn(self.all_command(), out)
        self.assertEqual(steps[-1], "doctor")
        # the healthy items are still applied
        self.assertEqual(len(self.backups(self.claude)), 1)

    def test_exception_prints_manual_runs_doctor_and_exits_1(self):
        with mock.patch.object(uc, "evaluate", side_effect=RuntimeError("boom")):
            out, code, steps = self.run_update(all=True, yes=True)
        self.assertEqual(code, 1)
        self.assertIn("manual:", out)
        self.assertIn("boom", out)
        self.assertEqual(steps[-1], "doctor")

    def test_install_exception_exits_1(self):
        self.put_sources()
        with mock.patch.object(uc, "install", side_effect=OSError("disk full")):
            out, code, steps = self.run_update(all=True, yes=True)
        self.assertEqual(code, 1)
        self.assertIn("manual:", out)
        self.assertEqual(steps[-1], "doctor")

    def test_module_unavailable_prints_manual_and_exits_1(self):
        with mock.patch.dict(sys.modules, {"user_config": None}):
            out, code, steps = self.run_update(all=True, yes=True)
        self.assertEqual(code, 1)
        self.assertIn("manual:", out)
        self.assertEqual(steps[-1], "doctor")

    def test_module_unavailable_without_all_is_silent(self):
        with mock.patch.dict(sys.modules, {"user_config": None}):
            out, code, _ = self.run_update(yes=True)
        self.assertIsNone(code)
        self.assertNotIn("manual:", out)

    def test_step_runs_after_the_other_steps_and_before_doctor(self):
        order = []
        names = ("_handle_agents_md", "_agents_step", "_claude_hooks_step", "_models_step",
                 "_codex_hooks_step", "_user_config_step")
        for name in names:
            patcher = mock.patch(f"update.{name}",
                                 side_effect=lambda *a, _n=name, **k: order.append(_n) or True)
            patcher.start()
            self.addCleanup(patcher.stop)
        with mock.patch("update.run_step",
                        side_effect=lambda e, d, cmd: order.append(cmd[0]) or OK), \
                redirect_stdout(StringIO()):
            update._post_update(self.engine, self.data, Args(), moved=False)
        self.assertEqual(order, list(names) + ["doctor"])


class TestAgentsMdUnderAll(UpdateAllCase):
    def test_all_replaces_a_stale_file_and_commits_without_apply_agents(self):
        _write(self.agents, "old text\n")
        _commit(self.data, "old agents")
        out, code, _ = self.run_update(all=True, yes=True)
        self.assertIsNone(code, out)
        self.assertEqual(self.agents.read_text(encoding="utf-8"), "AGENTS v1\ncontent-a\n")
        self.assertEqual(self.log()[0], "docs: update AGENTS.md to the engine's template")
        self.assertIn("-old text", out)
        self.assertIn("+content-a", out)
        self.assertNotIn("--apply-agents", out)

    def test_all_writes_a_missing_file(self):
        self.agents.unlink()
        _commit(self.data, "drop agents")
        out, code, _ = self.run_update(all=True, yes=True)
        self.assertIsNone(code, out)
        self.assertEqual(self.agents.read_text(encoding="utf-8"), "AGENTS v1\ncontent-a\n")
        self.assertEqual(self.log()[0], "docs: update AGENTS.md to the engine's template")

    def _with_template_revision(self, revision: int):
        template = (f"AGENTS v1\ncontent-a\n"
                    f"<!-- vault-engine AGENTS.md template: {revision}. Keep this line. -->\n")
        _write(self.engine / "templates" / "AGENTS.md", template)  # engine tree is not checked here
        return template

    def run_handler(self, **kw) -> str:
        with redirect_stdout(StringIO()) as buf:
            update._handle_agents_md(self.engine, self.data, Args(**kw))
        return buf.getvalue()

    def test_all_keeps_a_customized_file_and_says_why(self):
        self._with_template_revision(5)
        mine = "my translation\n<!-- vault-engine AGENTS.md template: 5. Keep this line. -->\n"
        _write(self.agents, mine)
        _commit(self.data, "translated")
        self.assertEqual(update.agents_md_status(self.engine, self.data)[0], "customized")
        out = self.run_handler(all=True, yes=True)
        self.assertEqual(self.agents.read_text(encoding="utf-8"), mine)
        self.assertIn("kept as is", out)
        self.assertIn("--apply-agents", out)

    def test_apply_agents_still_forces_a_customized_file(self):
        template = self._with_template_revision(5)
        _write(self.agents, "my translation\n<!-- vault-engine AGENTS.md template: 5. Keep this line. -->\n")
        _commit(self.data, "translated")
        self.run_handler(all=True, yes=True, apply_agents=True)
        self.assertEqual(self.agents.read_text(encoding="utf-8"), template)
        self.assertEqual(self.log()[0], "docs: update AGENTS.md to the engine's template (5)")

    def test_all_keeps_a_newer_file_and_says_so(self):
        self._with_template_revision(5)
        mine = "from a newer engine\n<!-- vault-engine AGENTS.md template: 9. Keep this line. -->\n"
        _write(self.agents, mine)
        _commit(self.data, "newer")
        self.assertEqual(update.agents_md_status(self.engine, self.data)[0], "newer")
        out = self.run_handler(all=True, yes=True)
        self.assertEqual(self.agents.read_text(encoding="utf-8"), mine)
        self.assertIn("not replaced", out)
        self.assertIn("newer than this engine's", out)

    def test_plain_yes_still_only_shows_the_diff(self):
        _write(self.agents, "old text\n")
        out, _code, _ = self.run_update(yes=True)
        self.assertEqual(self.agents.read_text(encoding="utf-8"), "old text\n")
        self.assertIn("-old text", out)
        self.assertIn("rerun with --apply-agents", out)


class TestPlainYesWritesNothing(UpdateAllCase):
    def test_yes_alone_leaves_user_config_and_agents_md_alone(self):
        self.put_sources()
        _write(self.agents, "old text\n")
        out, code, _ = self.run_update(yes=True)
        self.assertIsNone(code)
        self.assertEqual(self.backups(self.claude), [])
        self.assertEqual(self.backups(self.codex), [])
        self.assertEqual(self.agents.read_text(encoding="utf-8"), "old text\n")
        self.assertIn(self.all_command(), out)


class TestContextHint(UpdateAllCase):
    def _hint(self):
        _write(self.engine / "tools" / "graph.py", "# stub\n")  # untracked is fine for the hint
        update._save_cache(self.engine, {"latest": "v0.10.0",
                                         "checked": datetime.now().isoformat(timespec="seconds")})
        return update.context_hint(self.paths)

    def test_hint_recommends_update_all(self):
        hint = self._hint()
        self.assertIsNotNone(hint)
        self.assertIn("update --all", hint)

    def test_hint_never_recommends_plain_update(self):
        hint = self._hint()
        # the only update command named in the hint is the --all one
        self.assertEqual(hint.count(" update"), hint.count(" update --all"))
        self.assertNotIn("`python", hint.replace(self.all_command(), ""))


class TestNothingManualLeftAfterAll(UpdateAllCase):
    FORBIDDEN = ("user-config --install", "--apply-agents", "manual:")

    def test_everything_drifted_then_all_leaves_no_manual_command(self):
        self.put_sources()
        _write(self.agents, "old text\n")
        _commit(self.data, "old agents")
        first, code, _ = self.run_update(all=True, yes=True)
        self.assertIsNone(code, first)
        for text in self.FORBIDDEN:
            self.assertNotIn(text, first)
        # a second run has nothing to do, and says nothing about manual commands
        second, code, _ = self.run_update(all=True, yes=True)
        self.assertIsNone(code, second)
        for text in self.FORBIDDEN:
            self.assertNotIn(text, second)
        self.assertNotIn("differs from the vault templates", second)

    def test_doctor_sources_are_quiet_afterwards(self):
        self.put_sources()
        _write(self.agents, "old text\n")
        _commit(self.data, "old agents")
        self.run_update(all=True, yes=True)
        self.assertEqual(update.agents_md_status(self.engine, self.data)[0], "current")
        results = uc.statuses(self.paths)
        self.assertTrue(results)
        self.assertTrue(all(level == "OK" for level, _msg in results), results)


class TestDoctorWarnsNameUpdateAll(UpdateAllCase):
    def test_user_config_drift_warn_names_update_all(self):
        self.put_sources()
        warns = [msg for level, msg in uc.statuses(self.paths) if level == "WARN"]
        self.assertEqual(len(warns), 2)
        for msg in warns:
            self.assertIn("update --all", msg)
            self.assertNotIn("user-config --install", msg)

    def test_install_hint_and_standalone_command_are_unchanged(self):
        self.assertEqual(uc.INSTALL_HINT, 'python "$VAULT_ENGINE/tools/graph.py" user-config --install')
        self.put_sources()
        buf = StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            uc.cmd_user_config(argparse.Namespace(install=False, data=str(self.data)))
        self.assertEqual(cm.exception.code, 1)
        with redirect_stdout(StringIO()) as out, self.assertRaises(SystemExit) as cm:
            uc.cmd_user_config(argparse.Namespace(install=True, data=str(self.data)))
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("updated", out.getvalue())

    def test_doctor_agents_md_stale_line_names_update_all(self):
        import onboarding
        text = Path(onboarding.__file__).read_text(encoding="utf-8")
        self.assertIn("_graph_cmd('update --all')", text)
        self.assertNotIn("_graph_cmd('update --apply-agents')", text)


class TestHelpText(unittest.TestCase):
    def test_all_help_no_longer_says_agents_md_needs_apply_agents(self):
        parser = argparse.ArgumentParser()
        update.register(parser.add_subparsers())
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        help_text = " ".join(
            " ".join(a.help.split()) for a in sub.choices["update"]._actions
            if a.option_strings == ["--all"])
        self.assertNotIn("still needs --apply-agents", help_text)
        self.assertIn("user config", help_text)
        self.assertIn("AGENTS.md", help_text)


if __name__ == "__main__":
    unittest.main()
