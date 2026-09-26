"""Tests for tools/onboarding.py (init/setup/doctor).

Uses only tempfile-based directories for anything written to; Path.home(),
env vars, the setx call and the Ollama reachability check are all patched so
no test ever touches the real user environment. Run with:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

# Never let a test reach the real user's data repo through the environment
# (a missing patch then fails loudly instead of writing into it).
for _var in ("VAULT_DATA", "VAULT_HOME"):
    os.environ.pop(_var, None)


# Nor through the Windows registry, where graph finds the VAULT_DATA that setup
# saved for the user: every test sees an empty HKCU\Environment instead.
def _no_registry(*_args):
    raise OSError("tests never read the real registry")


sys.modules["winreg"] = types.SimpleNamespace(HKEY_CURRENT_USER=None, OpenKey=_no_registry)
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAPH_PATH = REPO_ROOT / "tools" / "graph.py"
TOOLS_DIR = REPO_ROOT / "tools"

# Load graph.py the same way graph.py's own tests and the CLI do.
_spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)

sys.path.insert(0, str(TOOLS_DIR))
import onboarding  # noqa: E402  (path must be set up first)


def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                           text=True, encoding="utf-8")


def _isolated_git_env(tmp: Path, name: str | None, email: str | None) -> dict:
    """Env so any `git config`/`git commit` in a test reads/writes neither the
    real user's global nor system git config: GIT_CONFIG_GLOBAL points at a
    private fixture file and GIT_CONFIG_NOSYSTEM=1 skips /etc/gitconfig (or its
    Windows equivalent). name/email None means that fixture has no identity."""
    global_config = tmp / "isolated-gitconfig"
    lines = ["[commit]", "\tgpgsign = false"]
    if name is not None:
        lines += ["[user]", f"\tname = {name}", f"\temail = {email}"]
    global_config.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return {"GIT_CONFIG_GLOBAL": str(global_config), "GIT_CONFIG_NOSYSTEM": "1"}


class TestInit(unittest.TestCase):
    def setUp(self):
        # Resolved so short (8.3) Windows path forms never mismatch a later .resolve().
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # Every test in this class now runs `git init`/`git commit` for real (the
        # initial-commit feature); isolate that from the real user's identity so
        # results are deterministic regardless of the host's global git config.
        self._env_patch = mock.patch.dict(os.environ, self._no_identity_env(), clear=False)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def _no_identity_env(self) -> dict:
        return _isolated_git_env(self.tmp, name=None, email=None)

    def _identity_env(self, name: str = "Test Runner", email: str = "tester@example.com") -> dict:
        return _isolated_git_env(self.tmp, name=name, email=email)

    def _args(self, target: Path, **kw):
        base = dict(dir=str(target), feedback=None, feedback_mode=None,
                    project_root=None, yes=True)
        base.update(kw)
        return Namespace(**base)

    def test_creates_files_config_and_hookspath(self):
        target = self.tmp / "example-data"
        with mock.patch("sys.stdin.isatty", return_value=False), redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))

        self.assertTrue((target / "AGENTS.md").exists())
        self.assertTrue((target / "profile" / "language.md").exists())

        config = json.loads((target / "vault.config.json").read_text(encoding="utf-8"))
        # A fresh init never writes this computer's paths into the shared config.
        self.assertNotIn("project_roots", config)
        self.assertEqual(config["feedback"], {"level": "off", "mode": "ask"})

        hooks_path = git(["config", "core.hooksPath"], target).stdout.strip()
        self.assertEqual(hooks_path, (graph.ENGINE / "tools" / "hooks").as_posix())

    def test_next_steps_lead_agents_to_questions_and_answers(self):
        # An agent's stdin isn't a terminal; plain `onboard` can't ask it anything.
        target = self.tmp / "example-data-next"
        with redirect_stdout(StringIO()) as buf:
            onboarding.cmd_init(self._args(target))
        out = buf.getvalue()
        self.assertIn("onboard --questions", out)
        self.assertIn(f'onboard --answers <answers.json> --data "{target}"', out)

    def test_default_feedback_is_off(self):
        target = self.tmp / "example-data2"
        with mock.patch("sys.stdin.isatty", return_value=False), redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))
        config = json.loads((target / "vault.config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["feedback"]["level"], "off")

    def test_ci_workflow_names_the_engine_repo(self):
        import feedback
        import update
        target = self.tmp / "example-data6"
        workflow = target / ".github" / "workflows" / "vault.yml"
        # The ref half depends on how this checkout is run (a branch in CI, a
        # release tag for users); pin it so only the repo half is under test.
        with mock.patch.object(feedback, "_derive_repo", return_value="example/vault-engine"), \
                mock.patch.object(update, "channel", return_value=("dev", "main")), \
                redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("uses: example/vault-engine@main", text)
        self.assertNotIn(onboarding.ENGINE_REPO_PLACEHOLDER, text)

    def test_ci_workflow_placeholder_kept_without_remote(self):
        import feedback
        target = self.tmp / "example-data7"
        with mock.patch.object(feedback, "_derive_repo", return_value=None), \
                redirect_stdout(StringIO()) as buf:
            onboarding.cmd_init(self._args(target))
        text = (target / ".github" / "workflows" / "vault.yml").read_text(encoding="utf-8")
        self.assertIn(onboarding.ENGINE_REPO_PLACEHOLDER, text)
        self.assertIn("vault.yml", buf.getvalue())

    def test_ci_workflow_pins_stable_tag(self):
        import update
        target = self.tmp / "example-data8"
        workflow = target / ".github" / "workflows" / "vault.yml"
        with mock.patch.object(update, "channel", return_value=("stable", "v0.3.0")), \
                redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("@v0.3.0", text)
        self.assertNotIn(onboarding.ENGINE_REF_PLACEHOLDER, text)

    def test_ci_workflow_pins_main_on_dev_channel(self):
        import update
        target = self.tmp / "example-data9"
        workflow = target / ".github" / "workflows" / "vault.yml"
        with mock.patch.object(update, "channel", return_value=("dev", "main")), \
                redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("@main", text)
        self.assertNotIn(onboarding.ENGINE_REF_PLACEHOLDER, text)

    def test_ci_workflow_pins_main_when_update_module_unavailable(self):
        target = self.tmp / "example-data10"
        workflow = target / ".github" / "workflows" / "vault.yml"
        with mock.patch.dict(sys.modules, {"update": None}), redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("@main", text)

    def test_explicit_feedback_level_honoured(self):
        target = self.tmp / "example-data3"
        with redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target, feedback="metrics", feedback_mode="auto"))
        config = json.loads((target / "vault.config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["feedback"], {"level": "metrics", "mode": "auto"})

    def test_does_not_overwrite_existing_files(self):
        target = self.tmp / "example-data4"
        target.mkdir(parents=True)
        (target / "AGENTS.md").write_text("custom content\n", encoding="utf-8")
        with redirect_stdout(StringIO()) as buf:
            onboarding.cmd_init(self._args(target))
        self.assertEqual((target / "AGENTS.md").read_text(encoding="utf-8"), "custom content\n")
        self.assertIn("skipped", buf.getvalue())

    def test_refuses_inside_engine(self):
        target = graph.ENGINE / "example-data-inside-engine"
        with self.assertRaises(SystemExit), redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))
        self.assertFalse(target.exists())

    def test_second_run_is_a_noop_on_git_init(self):
        target = self.tmp / "example-data5"
        with redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))
            onboarding.cmd_init(self._args(target))  # must not fail on an existing repo
        self.assertTrue((target / ".git").is_dir())

    def test_project_root_flag_goes_to_machine_json_not_config(self):
        target = self.tmp / "example-data-proot"
        root = self.tmp / "custom-root"
        with redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target, project_root=[str(root)]))
        config = json.loads((target / "vault.config.json").read_text(encoding="utf-8"))
        self.assertNotIn("project_roots", config)
        machine = json.loads((target / ".graph" / "machine.json").read_text(encoding="utf-8"))
        self.assertEqual(machine["project_roots"], [str(root)])

    def test_interactive_answer_same_as_default_writes_nothing(self):
        target = self.tmp / "example-data-proot-default"
        default = str(graph.ENGINE.parent)
        with mock.patch.object(onboarding.g, "stdin_is_interactive", return_value=True), \
             mock.patch("builtins.input", return_value=""), \
             redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target, yes=False))
        config = json.loads((target / "vault.config.json").read_text(encoding="utf-8"))
        self.assertNotIn("project_roots", config)
        self.assertFalse((target / ".graph" / "machine.json").exists())

    def test_project_roots_precedence_machine_over_config_over_engine_parent(self):
        import discovery

        target = self.tmp / "example-data-precedence"
        with redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))
        paths = graph.Paths(graph.ENGINE, target)

        # Nothing set anywhere: falls back to the engine's own parent folder.
        self.assertEqual(graph.project_roots(paths), [graph.ENGINE.resolve().parent])

        # vault.config.json set (backward compatibility): used when there is no
        # per-computer override.
        config_root = self.tmp / "config-root"
        config_root.mkdir()
        (target / "vault.config.json").write_text(
            json.dumps({"project_roots": [str(config_root)]}), encoding="utf-8", newline="\n")
        self.assertEqual(graph.project_roots(paths), [config_root.resolve()])

        # machine.json wins over vault.config.json.
        machine_root = self.tmp / "machine-root"
        (machine_root / "some-project").mkdir(parents=True)
        (machine_root / "some-project" / ".git").mkdir()
        with redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target, project_root=[str(machine_root)]))
        self.assertEqual(graph.project_roots(paths), [machine_root.resolve()])

        # discovery.py must see the same precedence (it goes through g.project_roots).
        found = {p["name"] for p in discovery.discover(paths)}
        self.assertIn("some-project", found)

    def test_project_roots_skip_folders_missing_on_this_computer(self):
        # A v0.2.0 vault carries the first computer's absolute path in the
        # shared config; on a second computer that path does not exist and must
        # not hide the default (the engine's parent folder).
        target = self.tmp / "example-data-missing-roots"
        target.mkdir()
        paths = graph.Paths(graph.ENGINE, target)
        other_pc = str(self.tmp / "other-computer" / "Dev")
        (target / "vault.config.json").write_text(
            json.dumps({"project_roots": [other_pc]}), encoding="utf-8", newline="\n")
        self.assertEqual(graph.project_roots(paths), [graph.ENGINE.resolve().parent])

        # Only the missing ones are dropped from a list.
        here = self.tmp / "here"
        here.mkdir()
        (target / "vault.config.json").write_text(
            json.dumps({"project_roots": [other_pc, str(here)]}), encoding="utf-8", newline="\n")
        self.assertEqual(graph.project_roots(paths), [here.resolve()])

        # machine.json whose roots are all missing falls through to the config.
        (target / ".graph").mkdir()
        (target / ".graph" / "machine.json").write_text(
            json.dumps({"project_roots": [other_pc]}), encoding="utf-8", newline="\n")
        self.assertEqual(graph.project_roots(paths), [here.resolve()])

        # A project under the default root is still recognized.
        project = graph.ENGINE.resolve().parent / "some-project" / "src"
        (target / "vault.config.json").write_text(
            json.dumps({"project_roots": [other_pc]}), encoding="utf-8", newline="\n")
        self.assertEqual(graph.detect_project(project, paths), "some-project")

    def test_initial_commit_made_with_identity(self):
        target = self.tmp / "example-data-commit"
        with mock.patch.dict(os.environ, self._identity_env(), clear=False), \
             redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))

        log = git(["log", "--format=%s"], target).stdout.strip().splitlines()
        self.assertEqual(log, ["chore: initialize vault"])

        branch = git(["rev-parse", "--abbrev-ref", "HEAD"], target).stdout.strip()
        self.assertEqual(branch, "main")

        status = git(["status", "--porcelain"], target).stdout.strip()
        self.assertEqual(status, "")

    def test_no_identity_means_no_commit_and_hint_printed(self):
        target = self.tmp / "example-data-no-identity"
        # setUp's default env already has no identity anywhere.
        with redirect_stdout(StringIO()) as buf:
            onboarding.cmd_init(self._args(target))

        self.assertNotEqual(git(["rev-parse", "--verify", "-q", "HEAD"], target).returncode, 0)
        out = buf.getvalue()
        self.assertIn("user.name", out)
        self.assertIn("user.email", out)
        self.assertIn("commit", out)

    def test_rerun_on_repo_with_commits_makes_no_new_commit(self):
        target = self.tmp / "example-data-rerun-commit"
        with mock.patch.dict(os.environ, self._identity_env(), clear=False):
            with redirect_stdout(StringIO()):
                onboarding.cmd_init(self._args(target))
            first_log = git(["log", "--format=%H"], target).stdout.strip().splitlines()
            self.assertEqual(len(first_log), 1)

            with redirect_stdout(StringIO()) as buf:
                onboarding.cmd_init(self._args(target))  # rerun: repo already has commits
            second_log = git(["log", "--format=%H"], target).stdout.strip().splitlines()
        self.assertEqual(second_log, first_log)  # no new commit
        self.assertNotIn("initial commit created", buf.getvalue())

    def _outer_repo(self) -> Path:
        outer = self.tmp / "some-project"
        outer.mkdir()
        git(["init", "-q"], outer)
        git(["config", "core.hooksPath", ".husky"], outer)
        return outer

    def test_refuses_new_folder_inside_another_repo(self):
        outer = self._outer_repo()
        target = outer / "notes" / "vault"
        with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as ctx:
            onboarding.cmd_init(self._args(target))
        self.assertIn("inside another git repo", str(ctx.exception.code))
        self.assertFalse((outer / "notes").exists())
        self.assertEqual(git(["config", "core.hooksPath"], outer).stdout.strip(), ".husky")

    def test_refuses_existing_subfolder_of_another_repo(self):
        outer = self._outer_repo()
        target = outer / "docs"
        target.mkdir()
        with redirect_stdout(StringIO()), self.assertRaises(SystemExit):
            onboarding.cmd_init(self._args(target))
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(git(["config", "core.hooksPath"], outer).stdout.strip(), ".husky")
        self.assertEqual(git(["status", "--porcelain"], outer).stdout.strip(), "")

    def test_existing_repo_root_is_used_and_old_hooks_path_named(self):
        target = self._outer_repo()
        with redirect_stdout(StringIO()) as buf:
            onboarding.cmd_init(self._args(target))
        self.assertTrue((target / "AGENTS.md").exists())
        self.assertIn("(was .husky)", buf.getvalue())
        self.assertEqual(git(["config", "core.hooksPath"], target).stdout.strip(),
                         (graph.ENGINE / "tools" / "hooks").as_posix())

    def test_rerun_on_existing_vault_says_which_templates_came_back(self):
        target = self.tmp / "example-data-deleted-template"
        with redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))
        (target / "decisions" / "_template.md").unlink()
        with redirect_stdout(StringIO()) as buf:
            onboarding.cmd_init(self._args(target))
        out = buf.getvalue()
        self.assertIn("created: decisions/_template.md", out.replace("\\", "/"))
        self.assertIn("use `machine` instead of `init`", out)


class TestMachine(unittest.TestCase):
    """`machine` writes only this computer's .graph/machine.json."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = _isolated_git_env(self.tmp, name="Test Runner", email="tester@example.com")
        env_patch = mock.patch.dict(os.environ, env, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop("VAULT_MACHINE", None)
        self.data = self.tmp / "example-vault"
        with redirect_stdout(StringIO()):
            onboarding.cmd_init(Namespace(dir=str(self.data), feedback=None, feedback_mode=None,
                                          project_root=None, yes=True))
        # A second computer's clone: the user deleted a template on the first one.
        git(["rm", "-q", "decisions/_template.md"], self.data)
        git(["commit", "-q", "-m", "chore: drop template"], self.data)

    def _run(self, **kw):
        args = dict(data=str(self.data), project_root=None, name=None)
        args.update(kw)
        with redirect_stdout(StringIO()) as buf:
            onboarding.cmd_machine(Namespace(**args))
        return buf.getvalue()

    def _machine(self) -> dict:
        return json.loads((self.data / ".graph" / "machine.json").read_text(encoding="utf-8"))

    def test_project_root_changes_nothing_but_machine_json(self):
        config_before = (self.data / "vault.config.json").read_bytes()
        root = self.tmp / "code"
        root.mkdir()
        out = self._run(project_root=[str(root)])
        self.assertEqual(self._machine(), {"project_roots": [str(root)]})
        self.assertEqual((self.data / "vault.config.json").read_bytes(), config_before)
        self.assertFalse((self.data / "decisions" / "_template.md").exists())
        self.assertEqual(git(["status", "--porcelain"], self.data).stdout.strip(), "")
        self.assertIn(f"project roots: {root}", out)
        paths = graph.Paths(graph.ENGINE, self.data)
        self.assertEqual(graph.project_roots(paths), [root])

    def test_keeps_other_machine_settings(self):
        (self.data / ".graph").mkdir(exist_ok=True)
        (self.data / ".graph" / "machine.json").write_text(
            json.dumps({"updates": {"check": False}}), encoding="utf-8")
        self._run(name="Laptop 2")
        self.assertEqual(self._machine(), {"updates": {"check": False}, "machine": "laptop-2"})

    def test_missing_root_is_saved_with_a_note(self):
        out = self._run(project_root=[str(self.tmp / "not-there")])
        self.assertIn("is not a folder on this computer", out)
        self.assertEqual(self._machine()["project_roots"], [str(self.tmp / "not-there")])

    def test_name_that_looks_like_personal_data_is_refused(self):
        email = "jane" + ".doe@" + "example" + "-personal.com"
        with self.assertRaises(SystemExit):
            self._run(name=email)
        with self.assertRaises(SystemExit):
            self._run(name="!!!")
        self.assertFalse((self.data / ".graph" / "machine.json").exists())

    def test_rename_hint_keeps_the_old_learned_file(self):
        paths = graph.Paths(graph.ENGINE, self.data)
        old = graph.machine_name(paths)
        paths.learned_dir.mkdir(parents=True, exist_ok=True)
        (paths.learned_dir / f"{old}.json").write_text("{}\n", encoding="utf-8")
        out = self._run(name="pc-new")
        self.assertIn(f"mv .graph/learned/{old}.json .graph/learned/pc-new.json", out)
        self.assertTrue((paths.learned_dir / f"{old}.json").exists())
        self.assertEqual(graph.machine_name(paths), "pc-new")

    def test_refuses_a_folder_that_is_not_a_data_repo(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(SystemExit):
            self._run(data=str(empty), name="pc-1")
        self.assertFalse((empty / ".graph").exists())

    def test_without_options_only_shows(self):
        out = self._run()
        self.assertIn("machine name:", out)
        self.assertFalse((self.data / ".graph" / "machine.json").exists())


class TestMachineName(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.paths = graph.Paths(graph.ENGINE, self.tmp)
        env_patch = mock.patch.dict(os.environ, {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop("VAULT_MACHINE", None)

    def _save(self, name: str) -> None:
        (self.tmp / ".graph").mkdir(exist_ok=True)
        (self.tmp / ".graph" / "machine.json").write_text(json.dumps({"machine": name}),
                                                          encoding="utf-8")

    def test_hostname_without_a_chosen_name(self):
        with mock.patch("socket.gethostname", return_value="Work-PC.corp"):
            self.assertEqual(graph.machine_name(self.paths), "work-pc-corp")

    def test_machine_json_wins_over_hostname(self):
        self._save("pc-1a2b")
        with mock.patch("socket.gethostname", return_value="Work-PC"):
            self.assertEqual(graph.machine_name(self.paths), "pc-1a2b")
            self.assertEqual(graph.machine_name(), "work-pc")  # no data folder given

    def test_vault_machine_wins_over_machine_json(self):
        self._save("pc-1a2b")
        with mock.patch.dict(os.environ, {"VAULT_MACHINE": "Desk"}):
            self.assertEqual(graph.machine_name(self.paths), "desk")

    def test_learned_file_uses_the_chosen_name(self):
        self._save("pc-1a2b")
        (self.tmp / "a.md").write_text("---\nkeywords: [a]\nlinks: [\"[[b]]\"]\n---\n# A\n",
                                       encoding="utf-8")
        (self.tmp / "b.md").write_text("---\nkeywords: [b]\n---\n# B\n", encoding="utf-8")
        g = graph.Graph(self.paths)
        g.add_learned(("a", "b"), 0.1)
        g.save_learned()
        self.assertEqual([p.name for p in self.paths.learned_dir.iterdir()], ["pc-1a2b.json"])


class TestSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "example-data"
        self.data.mkdir()
        (self.data / "AGENTS.md").write_text("root\n", encoding="utf-8")

    def _args(self, **kw):
        base = dict(data=str(self.data), user_level=False, no_env=True, no_machine=True,
                    no_agents=True, no_routing=True, yes=True)
        base.update(kw)
        return Namespace(**base)

    def _setup(self, interactive: bool = False, answers: list[str] | None = None, **kw) -> str:
        env = {k: v for k, v in os.environ.items() if k != "VAULT_MACHINE"}
        with mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(onboarding.g, "stdin_is_interactive", return_value=interactive), \
             mock.patch("builtins.input", side_effect=list(answers or [])), \
             redirect_stdout(StringIO()) as buf:
            onboarding.cmd_setup(self._args(yes=not interactive, **kw))
        return buf.getvalue()

    def _agent(self, name: str = "worker-low.md") -> Path:
        return self.home / ".claude" / "agents" / name

    def _engine_agent(self, name: str = "worker-low.md") -> str:
        return (graph.ENGINE / "tools" / "claude-agents" / name).read_text(encoding="utf-8")

    def test_own_agent_file_is_kept_without_a_terminal(self):
        self._agent().parent.mkdir(parents=True)
        self._agent().write_text("my own worker\n", encoding="utf-8")
        out = self._setup(no_agents=False)
        self.assertEqual(self._agent().read_text(encoding="utf-8"), "my own worker\n")
        self.assertIn("skipped (your own or edited version, kept)", out)
        self.assertEqual(self._agent("worker-medium.md").read_text(encoding="utf-8"),
                         self._engine_agent("worker-medium.md"))

    def test_own_agent_file_replaced_after_yes_with_a_backup(self):
        self._agent().parent.mkdir(parents=True)
        self._agent().write_text("my own worker\n", encoding="utf-8")
        out = self._setup(interactive=True, answers=["y"], no_agents=False)
        self.assertEqual(self._agent().read_text(encoding="utf-8"), self._engine_agent())
        backup = self._agent("worker-low.md.bak")
        self.assertEqual(backup.read_text(encoding="utf-8"), "my own worker\n")
        self.assertIn("replaced", out)

    def test_own_agent_file_kept_after_no(self):
        self._agent().parent.mkdir(parents=True)
        self._agent().write_text("my own worker\n", encoding="utf-8")
        self._setup(interactive=True, answers=["n"], no_agents=False)
        self.assertEqual(self._agent().read_text(encoding="utf-8"), "my own worker\n")
        self.assertFalse(self._agent("worker-low.md.bak").exists())

    def test_earlier_engine_version_is_updated_silently(self):
        old = "an earlier version shipped by the engine\n"
        self._agent().parent.mkdir(parents=True)
        self._agent().write_text(old, encoding="utf-8")
        with mock.patch("onboarding._shipped_blob_ids",
                        return_value={onboarding._blob_id(old.encode("utf-8"))}):
            out = self._setup(no_agents=False)
        self.assertEqual(self._agent().read_text(encoding="utf-8"), self._engine_agent())
        self.assertIn("updated", out)
        self.assertFalse(self._agent("worker-low.md.bak").exists())

    def test_blob_id_matches_git(self):
        src = graph.ENGINE / "tools" / "claude-agents" / "worker-low.md"
        out = git(["hash-object", "--no-filters", str(src)], graph.ENGINE)
        if out.returncode != 0:
            self.skipTest("git hash-object unavailable")
        self.assertEqual(onboarding._blob_id(src.read_bytes()), out.stdout.strip())

    def test_agents_do_not_carry_the_maintainers_language(self):
        for src in (graph.ENGINE / "tools" / "claude-agents").glob("*.md"):
            text = src.read_text(encoding="utf-8")
            self.assertNotIn("Turkish", text, src.name)
            self.assertIn("language profile", text, src.name)

    def _machine_json(self) -> dict:
        path = self.data / ".graph" / "machine.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def test_machine_gets_a_neutral_name_with_yes(self):
        out = self._setup(no_machine=False)
        name = self._machine_json()["machine"]
        self.assertRegex(name, r"^pc-[0-9a-f]{4}$")
        self.assertIn(f".graph/learned/{name}.json", out)
        again = self._setup(no_machine=False)  # rerun keeps it
        self.assertEqual(self._machine_json()["machine"], name)
        self.assertIn(f"ok: {name}", again)

    def test_machine_keeps_an_existing_hostname_file(self):
        learned = self.data / ".graph" / "learned"
        learned.mkdir(parents=True)
        with mock.patch("socket.gethostname", return_value="Home-PC"):
            (learned / "home-pc.json").write_text("{}\n", encoding="utf-8")
            out = self._setup(no_machine=False)
        self.assertEqual(self._machine_json(), {})
        self.assertIn("kept: .graph/learned/home-pc.json", out)

    def test_machine_name_asked_in_a_terminal(self):
        out = self._setup(interactive=True, answers=["Laptop"], no_machine=False)
        self.assertEqual(self._machine_json()["machine"], "laptop")
        self.assertIn("neutral name", out)

    def test_machine_name_left_alone_with_vault_machine(self):
        env = {k: v for k, v in os.environ.items()}
        env["VAULT_MACHINE"] = "desk"
        with mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch.dict(os.environ, env, clear=True), \
             redirect_stdout(StringIO()) as buf:
            onboarding.cmd_setup(self._args(no_machine=False))
        self.assertEqual(self._machine_json(), {})
        self.assertIn("ok: desk (from VAULT_MACHINE)", buf.getvalue())

    def test_agents_copied_and_idempotent(self):
        with mock.patch("pathlib.Path.home", return_value=self.home), redirect_stdout(StringIO()):
            onboarding.cmd_setup(self._args(no_agents=False))
        dest_dir = self.home / ".claude" / "agents"
        src_names = sorted(p.name for p in (graph.ENGINE / "tools" / "claude-agents").glob("*.md"))
        self.assertEqual(sorted(p.name for p in dest_dir.glob("*.md")), src_names)

        with mock.patch("pathlib.Path.home", return_value=self.home), redirect_stdout(StringIO()) as buf:
            onboarding.cmd_setup(self._args(no_agents=False))
        self.assertIn("unchanged", buf.getvalue())
        self.assertNotIn("copied", buf.getvalue())

    def test_routing_written_outside_git_repo(self):
        root = self.tmp / "projects"
        root.mkdir()
        (self.data / "vault.config.json").write_text(
            json.dumps({"project_roots": [str(root)]}), encoding="utf-8")
        with mock.patch("pathlib.Path.home", return_value=self.home), redirect_stdout(StringIO()):
            onboarding.cmd_setup(self._args(no_routing=False))
        claude_md = root / "CLAUDE.md"
        self.assertTrue(claude_md.exists())
        self.assertIn(f"@{self.data.as_posix()}/AGENTS.md", claude_md.read_text(encoding="utf-8"))

    def test_routing_skips_git_repo(self):
        root = self.tmp / "projects-git"
        root.mkdir()
        git(["init"], root)
        (self.data / "vault.config.json").write_text(
            json.dumps({"project_roots": [str(root)]}), encoding="utf-8")
        with mock.patch("pathlib.Path.home", return_value=self.home), redirect_stdout(StringIO()) as buf:
            onboarding.cmd_setup(self._args(no_routing=False))
        self.assertFalse((root / "CLAUDE.md").exists())
        self.assertIn("skipped", buf.getvalue())

    def test_routing_respects_existing_file_without_line(self):
        root = self.tmp / "projects-existing"
        root.mkdir()
        (root / "CLAUDE.md").write_text("something else\n", encoding="utf-8")
        (self.data / "vault.config.json").write_text(
            json.dumps({"project_roots": [str(root)]}), encoding="utf-8")
        with mock.patch("pathlib.Path.home", return_value=self.home), redirect_stdout(StringIO()):
            onboarding.cmd_setup(self._args(no_routing=False))
        self.assertEqual((root / "CLAUDE.md").read_text(encoding="utf-8"), "something else\n")

    def test_relative_routing_import_counts(self):
        # A hand-written relative import (e.g. `@<data folder name>/AGENTS.md`) routes too.
        root = self.tmp
        (root / "CLAUDE.md").write_text(f"@{self.data.name}/AGENTS.md\n", encoding="utf-8")
        self.assertTrue(onboarding._routes_to(root / "CLAUDE.md", self.data))
        (root / "CLAUDE.md").write_text("@elsewhere/AGENTS.md\n", encoding="utf-8")
        self.assertFalse(onboarding._routes_to(root / "CLAUDE.md", self.data))

    def test_user_level_append_once(self):
        args = self._args(user_level=True)
        with mock.patch("pathlib.Path.home", return_value=self.home), redirect_stdout(StringIO()):
            onboarding.cmd_setup(args)
            onboarding.cmd_setup(args)  # second run must not duplicate the line
        line = f"@{self.data.as_posix()}/AGENTS.md"
        claude_md_text = (self.home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(claude_md_text.count(line), 1)
        codex_text = (self.home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(codex_text.count(self.data.as_posix()), 1)

    def test_env_uses_setx_when_unset(self):
        calls = []
        with mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding._set_user_env_var", side_effect=lambda n, v: calls.append((n, v))), \
             mock.patch("onboarding._is_windows", return_value=True), \
             mock.patch.dict(os.environ, {}, clear=True), \
             redirect_stdout(StringIO()):
            onboarding.cmd_setup(self._args(no_env=False))
        self.assertEqual({n for n, _ in calls}, {"VAULT_ENGINE", "VAULT_DATA"})

    def test_env_skipped_when_already_correct(self):
        calls = []
        env = {"VAULT_ENGINE": str(graph.ENGINE), "VAULT_DATA": str(self.data)}
        with mock.patch("onboarding._set_user_env_var", side_effect=lambda n, v: calls.append(n)), \
             mock.patch.dict(os.environ, env, clear=True), \
             redirect_stdout(StringIO()) as buf:
            onboarding.cmd_setup(self._args(no_env=False))
        self.assertEqual(calls, [])
        self.assertIn("already set", buf.getvalue())

    def test_env_rerun_in_the_same_session_skips_setx(self):
        # setup ran before in this session: setx saved both values, but this
        # process (started earlier) still has neither in its environment.
        calls = []
        saved = {"VAULT_ENGINE": str(graph.ENGINE), "VAULT_DATA": str(self.data)}
        with mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding._set_user_env_var", side_effect=lambda n, v: calls.append(n)), \
             mock.patch("onboarding._user_env_var", side_effect=saved.get), \
             mock.patch("onboarding._is_windows", return_value=True), \
             mock.patch.dict(os.environ, {}, clear=True), \
             redirect_stdout(StringIO()) as buf:
            onboarding.cmd_setup(self._args(no_env=False, yes=True))
        self.assertEqual(calls, [])
        self.assertIn("VAULT_DATA already saved for your user", buf.getvalue())


class TestDoctorTools(unittest.TestCase):
    def _run(self, installed: set[str]) -> str:
        home = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        with mock.patch("onboarding._which", side_effect=lambda t: f"/bin/{t}" if t in installed else None), \
             mock.patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True), \
             mock.patch("pathlib.Path.home", return_value=home), \
             mock.patch("onboarding.urllib.request.urlopen"), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit):
                onboarding.cmd_doctor(Namespace())
        return buf.getvalue()

    def test_reports_installed_agents(self):
        out = self._run({"claude", "ollama"})
        self.assertIn("OK   Claude Code: found", out)
        self.assertIn("INFO Codex: not found", out)
        self.assertNotIn("no supported agent CLI", out)
        self.assertNotIn("ollama CLI not found", out)

    def test_warns_without_any_agent(self):
        out = self._run(set())
        self.assertIn("WARN no supported agent CLI found", out)
        self.assertIn("INFO ollama CLI not found", out)


class TestDoctor(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "example-data"
        self.data.mkdir()
        (self.data / "AGENTS.md").write_text("root\n", encoding="utf-8")
        git(["init"], self.data)
        git(["config", "core.hooksPath", (graph.ENGINE / "tools" / "hooks").as_posix()], self.data)

    def _env(self):
        # PATH must survive the clear=True patches below, or the `git` subprocess
        # calls inside cmd_doctor can't find the git executable at all.
        return {"VAULT_ENGINE": str(graph.ENGINE), "VAULT_DATA": str(self.data),
                "PATH": os.environ.get("PATH", "")}

    def test_ok(self):
        with mock.patch("pathlib.Path.home", return_value=self.home), redirect_stdout(StringIO()):
            onboarding.cmd_setup(Namespace(data=str(self.data), user_level=False, no_env=True,
                                           no_agents=False, no_routing=True, yes=True))
        with mock.patch.dict(os.environ, self._env(), clear=True), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding.urllib.request.urlopen"), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit) as ctx:
                onboarding.cmd_doctor(Namespace())
        self.assertEqual(ctx.exception.code, 0)
        self.assertNotIn("FAIL", buf.getvalue())

    def test_fail_missing_agents_md(self):
        (self.data / "AGENTS.md").unlink()
        with mock.patch.dict(os.environ, self._env(), clear=True), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding.urllib.request.urlopen", side_effect=OSError("no ollama")), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit) as ctx:
                onboarding.cmd_doctor(Namespace())
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("FAIL", buf.getvalue())

    def test_fail_on_lint_errors(self):
        (self.data / "bad.md").write_text(
            '---\nlinks:\n  - "[[missing-note]]"\n---\n# Bad\n', encoding="utf-8")
        with mock.patch.dict(os.environ, self._env(), clear=True), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding.urllib.request.urlopen", side_effect=OSError("no ollama")), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit) as ctx:
                onboarding.cmd_doctor(Namespace())
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("note lint", buf.getvalue())

    def test_warn_when_vault_engine_unset(self):
        env = self._env()
        del env["VAULT_ENGINE"]
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("onboarding._user_env_var", return_value=None), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding.urllib.request.urlopen", side_effect=OSError("no ollama")), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit):
                onboarding.cmd_doctor(Namespace())
        self.assertIn("WARN", buf.getvalue())
        self.assertIn("VAULT_ENGINE", buf.getvalue())


    def test_warn_restart_when_set_for_user_only(self):
        env = self._env()
        del env["VAULT_ENGINE"]
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("onboarding._user_env_var", return_value=str(graph.ENGINE)), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding.urllib.request.urlopen", side_effect=OSError("no ollama")), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit):
                onboarding.cmd_doctor(Namespace())
        self.assertIn("set for the user but not in this process", buf.getvalue())

    def test_user_env_var_is_none_off_windows(self):
        with mock.patch("onboarding._is_windows", return_value=False):
            self.assertIsNone(onboarding._user_env_var("VAULT_ENGINE"))

    def _doctor_without_vault_data(self, saved: dict, data: str | None = None,
                                   windows: bool = True, shell: str = "") -> tuple[int, str]:
        env = self._env()
        del env["VAULT_DATA"]
        env["SHELL"] = shell
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("onboarding._user_env_var", side_effect=lambda name: saved.get(name)), \
             mock.patch("onboarding._is_windows", return_value=windows), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding.urllib.request.urlopen", side_effect=OSError("no ollama")), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit) as ctx:
                onboarding.cmd_doctor(Namespace(data=data))
        return ctx.exception.code, buf.getvalue()

    def test_data_flag_checks_the_vault_without_vault_data(self):
        # Same session as setup: VAULT_DATA isn't in this process yet.
        code, out = self._doctor_without_vault_data({}, data=str(self.data))
        self.assertEqual(code, 0, out)
        self.assertIn("WARN VAULT_DATA not set: run setup --data", out)
        self.assertIn(f"OK   data dir has AGENTS.md ({self.data})", out)
        self.assertNotIn("FAIL", out)

    def test_value_saved_for_the_user_is_checked_with_a_restart_hint(self):
        code, out = self._doctor_without_vault_data({"VAULT_DATA": str(self.data)})
        self.assertEqual(code, 0, out)
        self.assertIn("WARN VAULT_DATA is set for the user but not in this process: "
                      "restart the terminal and the agent", out)
        self.assertIn(f"OK   data dir has AGENTS.md ({self.data})", out)

    def test_no_vault_data_anywhere_fails_and_names_data_flag(self):
        code, out = self._doctor_without_vault_data({})
        self.assertEqual(code, 1)
        self.assertIn("FAIL VAULT_DATA (or VAULT_HOME) not set", out)
        self.assertIn("--data", out)
        self.assertNotIn("data dir has", out)

    def test_rc_block_without_data_flag_gives_restart_hint(self):
        rc = self.home / ".bashrc"
        onboarding._write_rc_block(rc, [("VAULT_DATA", str(self.data))], "bash")
        code, out = self._doctor_without_vault_data({}, windows=False, shell="/bin/bash")
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL VAULT_DATA is set in {rc} but not in this process", out)
        code, out = self._doctor_without_vault_data({}, data=str(self.data), windows=False,
                                                    shell="/bin/bash")
        self.assertEqual(code, 0, out)
        self.assertIn(f"WARN VAULT_DATA is set in {rc} but not in this process", out)


class TestOnboardQuestions(unittest.TestCase):
    def test_questions_json_shape(self):
        with redirect_stdout(StringIO()) as buf:
            onboarding.cmd_onboard(Namespace(questions=True, answers=None, data=None, force=False))
        data = json.loads(buf.getvalue())
        ids = [q["id"] for q in data]
        self.assertEqual(ids, ["chat_language", "notes_language", "code_language", "detail",
                                "explain_level", "ask_before", "extra"])
        for q in data:
            self.assertIn("question", q)
            self.assertIn("kind", q)
            self.assertIn(q["kind"], ("choice", "multi", "text"))
        by_id = {q["id"]: q for q in data}
        self.assertEqual(by_id["detail"]["options"], ["short", "balanced", "detailed"])
        self.assertEqual(by_id["explain_level"]["options"], ["beginner", "intermediate", "expert"])
        self.assertEqual(sorted(by_id["ask_before"]["options"]),
                          sorted(["architecture", "new-dependencies", "deleting",
                                  "paid-or-external-services", "pushing-or-publishing"]))


class TestResolveAnswers(unittest.TestCase):
    def test_defaults_when_keys_missing(self):
        answers = onboarding.resolve_answers({})
        self.assertEqual(answers["chat_language"], "English")
        self.assertEqual(answers["notes_language"], "English")  # same as chat by default
        self.assertEqual(answers["code_language"], "English")
        self.assertEqual(answers["detail"], "balanced")
        self.assertEqual(answers["explain_level"], "intermediate")
        self.assertEqual(sorted(answers["ask_before"]), sorted(onboarding.ASK_BEFORE_OPTIONS))
        self.assertEqual(answers["extra"], [])

    def test_notes_language_defaults_to_chat_language(self):
        answers = onboarding.resolve_answers({"chat_language": "Turkish"})
        self.assertEqual(answers["notes_language"], "Turkish")

    def test_invalid_choice_rejected(self):
        with self.assertRaises(onboarding.AnswersError):
            onboarding.resolve_answers({"detail": "extremely-long"})

    def test_invalid_multi_choice_rejected(self):
        with self.assertRaises(onboarding.AnswersError):
            onboarding.resolve_answers({"ask_before": ["not-a-real-option"]})

    def test_extra_capped_at_5_lines_and_200_chars(self):
        raw = {"extra": "\n".join([f"line{i} " + "x" * 300 for i in range(8)])}
        answers = onboarding.resolve_answers(raw)
        self.assertEqual(len(answers["extra"]), 5)
        for line in answers["extra"]:
            self.assertLessEqual(len(line), 200)


class TestOnboardAnswers(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = self.tmp / "example-data"
        self.data.mkdir()
        (self.data / "profile").mkdir()
        shutil.copy2(REPO_ROOT / "templates" / "profile" / "language.md",
                     self.data / "profile" / "language.md")
        shutil.copy2(REPO_ROOT / "templates" / "profile" / "working-style.md",
                     self.data / "profile" / "working-style.md")

    def _answers_file(self, answers: dict) -> Path:
        path = self.tmp / "answers.json"
        path.write_text(json.dumps(answers), encoding="utf-8")
        return path

    def _run(self, answers: dict, force: bool = False):
        answers_file = self._answers_file(answers)
        with redirect_stdout(StringIO()) as buf:
            onboarding.cmd_onboard(Namespace(questions=False, answers=str(answers_file),
                                             data=str(self.data), force=force))
        return buf.getvalue()

    def test_writes_both_notes_with_valid_frontmatter(self):
        self._run({"chat_language": "Turkish", "notes_language": "Turkish",
                    "code_language": "English", "detail": "short",
                    "explain_level": "beginner",
                    "ask_before": ["architecture", "deleting"],
                    "extra": "Prefer bullet points."})

        lang_path = self.data / "profile" / "language.md"
        style_path = self.data / "profile" / "working-style.md"
        self.assertTrue(lang_path.exists())
        self.assertTrue(style_path.exists())

        lang_meta, lang_body = graph.parse_frontmatter(lang_path.read_text(encoding="utf-8"))
        style_meta, style_body = graph.parse_frontmatter(style_path.read_text(encoding="utf-8"))

        self.assertIs(lang_meta["core"], True)
        self.assertIs(style_meta["core"], True)
        self.assertEqual(lang_meta["links"], [])
        self.assertIn(style_meta["weights"], ({}, "{}"))

        self.assertNotIn(onboarding.SKELETON_MARKER, lang_body)
        self.assertNotIn(onboarding.SKELETON_MARKER, style_body)

        for body in (lang_body, style_body):
            non_empty = [l for l in body.splitlines() if l.strip()]
            self.assertLessEqual(len(non_empty), graph.MAX_CORE_LINES)

        self.assertIn("Talk in Turkish.", lang_body)
        self.assertIn("Write notes and docs in Turkish.", lang_body)
        self.assertIn("Write code, identifiers and commit messages in English.", lang_body)

        self.assertIn("Keep answers short.", style_body)
        self.assertIn("Explain technical terms simply; the user is a beginner.", style_body)
        self.assertIn("Ask before: architecture, deleting.", style_body)
        self.assertIn("Prefer bullet points.", style_body)

    def test_defaults_when_answers_file_omits_keys(self):
        self._run({})
        lang_body = (self.data / "profile" / "language.md").read_text(encoding="utf-8")
        self.assertIn("Talk in English.", lang_body)
        self.assertIn("Write notes and docs in English.", lang_body)

    def test_invalid_choice_reported_and_nothing_written(self):
        answers_file = self._answers_file({"detail": "way-too-much"})
        with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as ctx:
            onboarding.cmd_onboard(Namespace(questions=False, answers=str(answers_file),
                                             data=str(self.data), force=False))
        self.assertIn("invalid answers", str(ctx.exception.code))
        # Still the skeleton (untouched).
        self.assertIn(onboarding.SKELETON_MARKER,
                       (self.data / "profile" / "language.md").read_text(encoding="utf-8"))

    def test_guard_refuses_without_echoing_value(self):
        local_part = "jane" + ".doe"
        domain = "example" + "-personal.com"
        email = f"{local_part}@{domain}"
        with self.assertRaises(SystemExit) as ctx, redirect_stdout(StringIO()):
            self._run({"extra": f"Contact me at {email}"})
        message = str(ctx.exception.code)
        self.assertIn("email", message)
        self.assertNotIn(email, message)
        self.assertIn(onboarding.SKELETON_MARKER,
                       (self.data / "profile" / "language.md").read_text(encoding="utf-8"))

    def test_skips_already_filled_note_without_force(self):
        self._run({"chat_language": "Turkish"})
        out = self._run({"chat_language": "German"})
        self.assertIn("already filled", out)
        self.assertIn("--force", out)
        lang_body = (self.data / "profile" / "language.md").read_text(encoding="utf-8")
        self.assertIn("Talk in Turkish.", lang_body)
        self.assertNotIn("Talk in German.", lang_body)

    def test_answers_file_with_bom_is_accepted(self):
        # PowerShell 5.1: Set-Content -Encoding UTF8 writes a BOM first.
        path = self.tmp / "answers-bom.json"
        path.write_text(json.dumps({"chat_language": "Turkish"}), encoding="utf-8-sig")
        with redirect_stdout(StringIO()):
            onboarding.cmd_onboard(Namespace(questions=False, answers=str(path),
                                             data=str(self.data), force=False))
        self.assertIn("Talk in Turkish.",
                      (self.data / "profile" / "language.md").read_text(encoding="utf-8"))

    def _bad_answers(self, content: str | None) -> str:
        path = self.tmp / "answers-bad.json"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as ctx:
            onboarding.cmd_onboard(Namespace(questions=False, answers=str(path),
                                             data=str(self.data), force=False))
        self.assertIn(onboarding.SKELETON_MARKER,
                      (self.data / "profile" / "language.md").read_text(encoding="utf-8"))
        return str(ctx.exception.code)

    def test_broken_json_is_a_clear_error(self):
        self.assertIn("not valid JSON", self._bad_answers('{"chat_language": '))

    def test_json_that_is_not_an_object_is_a_clear_error(self):
        self.assertIn("one JSON object", self._bad_answers('["Turkish"]'))

    def test_missing_answers_file_is_a_clear_error(self):
        self.assertIn("cannot read the answers file", self._bad_answers(None))

    def test_force_overwrites_already_filled_note(self):
        self._run({"chat_language": "Turkish"})
        out = self._run({"chat_language": "German"}, force=True)
        self.assertIn("written", out)
        lang_body = (self.data / "profile" / "language.md").read_text(encoding="utf-8")
        self.assertIn("Talk in German.", lang_body)


class TestOnboardNonInteractive(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = self.tmp / "example-data"
        (self.data / "profile").mkdir(parents=True)
        for name in ("language.md", "working-style.md"):
            shutil.copy2(REPO_ROOT / "templates" / "profile" / name, self.data / "profile" / name)

    def _onboard(self) -> str:
        with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as ctx:
            onboarding.cmd_onboard(Namespace(questions=False, answers=None,
                                             data=str(self.data), force=False))
        return str(ctx.exception.code)

    def assert_untouched(self):
        for name in ("language.md", "working-style.md"):
            self.assertIn(onboarding.SKELETON_MARKER,
                          (self.data / "profile" / name).read_text(encoding="utf-8"))

    def test_no_flags_non_tty_stops_with_hint(self):
        with mock.patch("sys.stdin.isatty", return_value=False):
            message = self._onboard()
        self.assertIn("nothing written", message)
        self.assertIn("--questions", message)
        self.assertIn("--answers", message)
        self.assert_untouched()

    def test_nul_stdin_on_windows_is_not_a_terminal(self):
        # `onboard < NUL` (or `</dev/null` in Git Bash): isatty() says True there.
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch.object(onboarding.g, "_is_windows", return_value=True), \
             mock.patch.object(onboarding.g, "_is_console", return_value=False):
            message = self._onboard()
        self.assertIn("not an interactive terminal", message)
        self.assert_untouched()

    def test_end_of_input_mid_questions_writes_nothing(self):
        # Before: every prompt fell back to its default on EOF, the default
        # profile was written, and a later `--answers` run was skipped.
        with mock.patch.object(onboarding.g, "stdin_is_interactive", return_value=True), \
             mock.patch("builtins.input", side_effect=["Turkish", EOFError()]):
            message = self._onboard()
        self.assertIn("input ended", message)
        self.assertIn("--answers", message)
        self.assert_untouched()

    def test_interactive_answers_are_written(self):
        answers = iter(["Turkish"])
        with mock.patch.object(onboarding.g, "stdin_is_interactive", return_value=True), \
             mock.patch("builtins.input", side_effect=lambda _p: next(answers, "")), \
             redirect_stdout(StringIO()):
            onboarding.cmd_onboard(Namespace(questions=False, answers=None,
                                             data=str(self.data), force=False))
        self.assertIn("Talk in Turkish.",
                      (self.data / "profile" / "language.md").read_text(encoding="utf-8"))


class TestOnboardDoctorIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "example-data"
        self.data.mkdir()
        (self.data / "AGENTS.md").write_text("root\n", encoding="utf-8")
        (self.data / "profile").mkdir()
        shutil.copy2(REPO_ROOT / "templates" / "profile" / "language.md",
                     self.data / "profile" / "language.md")
        shutil.copy2(REPO_ROOT / "templates" / "profile" / "working-style.md",
                     self.data / "profile" / "working-style.md")
        git(["init"], self.data)
        git(["config", "core.hooksPath", (graph.ENGINE / "tools" / "hooks").as_posix()], self.data)

    def _env(self):
        return {"VAULT_ENGINE": str(graph.ENGINE), "VAULT_DATA": str(self.data),
                "PATH": os.environ.get("PATH", "")}

    def test_warns_with_skeleton_marker(self):
        with mock.patch.dict(os.environ, self._env(), clear=True), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding.urllib.request.urlopen", side_effect=OSError("no ollama")), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit):
                onboarding.cmd_doctor(Namespace())
        self.assertIn("profile not filled yet", buf.getvalue())

    def test_no_warn_after_onboarding(self):
        answers_file = self.tmp / "answers.json"
        answers_file.write_text(json.dumps({"chat_language": "English"}), encoding="utf-8")
        with redirect_stdout(StringIO()):
            onboarding.cmd_onboard(Namespace(questions=False, answers=str(answers_file),
                                             data=str(self.data), force=False))
        with mock.patch.dict(os.environ, self._env(), clear=True), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding.urllib.request.urlopen", side_effect=OSError("no ollama")), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit) as ctx:
                onboarding.cmd_doctor(Namespace())
        self.assertNotIn("profile not filled yet", buf.getvalue())
        self.assertEqual(ctx.exception.code, 0)


class TestOnboardEndToEnd(unittest.TestCase):
    def test_init_then_onboard_answers_lints_clean(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        target = tmp / "example-data"

        init_args = Namespace(dir=str(target), feedback=None, feedback_mode=None,
                              project_root=None, yes=True)
        with mock.patch("sys.stdin.isatty", return_value=False), redirect_stdout(StringIO()):
            onboarding.cmd_init(init_args)

        answers_file = tmp / "answers.json"
        answers_file.write_text(json.dumps({
            "chat_language": "Turkish", "notes_language": "Turkish", "code_language": "English",
            "detail": "balanced", "explain_level": "intermediate",
            "ask_before": ["architecture"], "extra": "",
        }), encoding="utf-8")
        with redirect_stdout(StringIO()):
            onboarding.cmd_onboard(Namespace(questions=False, answers=str(answers_file),
                                             data=str(target), force=False))

        with mock.patch.dict(os.environ, {"VAULT_DATA": str(target), "VAULT_HOME": ""}, clear=False), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit) as ctx:
                graph.cmd_lint(Namespace())
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("0 errors", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
