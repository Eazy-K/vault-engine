"""Engine commands leave the data repo's working tree clean.

Every command that writes shared files into the data repo (`onboard`,
`reinforce`/`decay`, `update`'s CI pin and AGENTS.md, `migrate`) commits exactly
those files with graph.commit_own_files; `doctor` warns about engine files that
are still uncommitted; a new vault's `.gitattributes` keeps commits free of CRLF
warnings. The last class walks the whole flow an agent follows (init, onboard,
two tasks with pull/push against a local bare remote) and checks `git status`.

Temp folders and temp git repos only, with the git config pointed into them;
nothing touches the real user's setup.
"""
from __future__ import annotations

import importlib.util
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
from io import StringIO
from pathlib import Path
from unittest import mock

for _var in ("VAULT_DATA", "VAULT_HOME"):
    os.environ.pop(_var, None)


def _no_registry(*_args):
    raise OSError("tests never read the real registry")


sys.modules["winreg"] = types.SimpleNamespace(HKEY_CURRENT_USER=None, OpenKey=_no_registry)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
GRAPH_PATH = TOOLS_DIR / "graph.py"

_spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)

sys.path.insert(0, str(TOOLS_DIR))
# A private copy of onboarding.py, bound to the graph instance above: importing
# it as `onboarding` here would hand test_onboarding (which runs later and
# patches its own graph instance) a module bound to this one.
_ob_spec = importlib.util.spec_from_file_location("onboarding_clean_tree", TOOLS_DIR / "onboarding.py")
onboarding = importlib.util.module_from_spec(_ob_spec)
_ob_spec.loader.exec_module(onboarding)


def git(args: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", env=env)


def init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    git(["init", "-q"], root)
    git(["config", "user.email", "tester@example.com"], root)
    git(["config", "user.name", "Test Runner"], root)
    git(["config", "commit.gpgsign", "false"], root)
    git(["config", "core.autocrlf", "false"], root)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def porcelain(repo: Path) -> list[str]:
    return git(["status", "--porcelain", "--untracked-files=all"], repo).stdout.splitlines()


def head_files(repo: Path) -> list[str]:
    out = git(["show", "--name-only", "--format=", "HEAD"], repo).stdout
    return sorted(l for l in out.splitlines() if l)


def subject(repo: Path) -> str:
    return git(["log", "-1", "--format=%s"], repo).stdout.strip()


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = self.tmp / "data"
        init_repo(self.data)
        write(self.data / "README.md", "vault\n")
        shutil.copy2(REPO_ROOT / "templates" / ".gitignore", self.data / ".gitignore")
        git(["add", "-A"], self.data)
        git(["commit", "-q", "-m", "initial"], self.data)


class TestCommitOwnFiles(RepoCase):
    def test_commits_only_the_named_files_and_keeps_other_staged_changes(self):
        write(self.data / "vault.config.json", "{}\n")
        write(self.data / "notes" / "mine.md", "# mine\n")
        git(["add", "notes/mine.md"], self.data)
        write(self.data / "README.md", "vault, edited\n")
        status, detail = graph.commit_own_files(self.data, ["vault.config.json"], "chore: x")
        self.assertEqual((status, detail), ("committed", "chore: x"))
        self.assertEqual(head_files(self.data), ["vault.config.json"])
        self.assertEqual(sorted(porcelain(self.data)), [" M README.md", "A  notes/mine.md"])

    def test_unchanged_files_make_no_commit(self):
        before = git(["rev-parse", "HEAD"], self.data).stdout
        self.assertEqual(graph.commit_own_files(self.data, ["README.md"], "chore: x"),
                         ("unchanged", ""))
        self.assertEqual(graph.commit_own_files(self.data, ["missing.json"], "chore: x"),
                         ("unchanged", ""))
        self.assertEqual(git(["rev-parse", "HEAD"], self.data).stdout, before)

    def test_skipped_outside_a_repo_and_before_the_first_commit(self):
        plain = self.tmp / "plain"
        write(plain / "a.json", "{}\n")
        status, detail = graph.commit_own_files(plain, ["a.json"], "chore: x")
        self.assertEqual(status, "skipped")
        self.assertIn("not a git repo", detail)
        fresh = self.tmp / "fresh"
        init_repo(fresh)
        write(fresh / "a.json", "{}\n")
        status, detail = graph.commit_own_files(fresh, ["a.json"], "chore: x")
        self.assertEqual(status, "skipped")
        self.assertIn("no commits yet", detail)

    def test_skipped_during_a_merge(self):
        (Path(self.data / ".git" / "MERGE_HEAD")).write_text(
            git(["rev-parse", "HEAD"], self.data).stdout, encoding="utf-8")
        write(self.data / "vault.config.json", "{}\n")
        status, detail = graph.commit_own_files(self.data, ["vault.config.json"], "chore: x")
        self.assertEqual(status, "skipped")
        self.assertIn("merge or rebase", detail)

    def test_failed_commit_is_reported_with_the_manual_command(self):
        hooks = self.tmp / "hooks"
        write(hooks / "pre-commit", "#!/bin/sh\necho blocked >&2\nexit 1\n")
        (hooks / "pre-commit").chmod(0o755)
        git(["config", "core.hooksPath", hooks.as_posix()], self.data)
        write(self.data / "vault.config.json", "{}\n")
        outcome = graph.commit_own_files(self.data, ["vault.config.json"], "chore: x")
        self.assertEqual(outcome[0], "failed")
        with redirect_stdout(StringIO()) as buf:
            graph.report_commit(self.data, ["vault.config.json"], outcome)
        self.assertIn("not committed", buf.getvalue())
        self.assertIn("commit -m", buf.getvalue())

    def test_git_status_lists_paths_or_none(self):
        write(self.data / "a b.md", "x\n")
        self.assertEqual(graph.git_status(self.data), ["a b.md"])
        self.assertEqual(graph.git_status(self.data, ["vault.config.json"]), [])
        self.assertIsNone(graph.git_status(self.tmp))


class TestReinforceCommits(RepoCase):
    def setUp(self):
        super().setUp()
        for name in ("alpha", "beta"):
            write(self.data / "notes" / f"{name}.md", f"# {name}\n")
        git(["add", "-A"], self.data)
        git(["commit", "-q", "-m", "notes"], self.data)
        self.paths = graph.Paths(graph.ENGINE, self.data)

    def _run(self, func, args) -> str:
        with mock.patch.object(graph, "default_paths", return_value=self.paths), \
             mock.patch.dict(os.environ, {"VAULT_MACHINE": "pc-test"}), \
             mock.patch.dict(sys.modules, {"update": None, "feedback": None, "schema": None}), \
             redirect_stdout(StringIO()) as out:
            func(args)
        return out.getvalue()

    def test_reinforce_commits_its_learned_file_only(self):
        write(self.data / "notes" / "draft.md", "# draft\n")  # the agent's own work in progress
        out = self._run(graph.cmd_reinforce, Namespace(notes=["alpha", "beta"], task="t1",
                                                       rate=graph.LEARNING_RATE))
        self.assertIn("committed in the data repo: chore: update learned links", out)
        self.assertEqual(head_files(self.data), [".graph/learned/pc-test.json"])
        self.assertEqual(porcelain(self.data), ["?? notes/draft.md"])

    def test_reinforce_without_edges_commits_a_leftover_learned_file(self):
        write(self.data / ".graph" / "learned" / "pc-test.json", '{\n  "alpha|beta": 0.1\n}\n')
        self._run(graph.cmd_reinforce, Namespace(notes=[], task="t2", rate=graph.LEARNING_RATE))
        self.assertEqual(porcelain(self.data), [])
        self.assertEqual(subject(self.data), "chore: update learned links")

    def test_decay_commits_its_learned_file(self):
        self._run(graph.cmd_reinforce, Namespace(notes=["alpha", "beta"], task="t1",
                                                 rate=graph.LEARNING_RATE))
        self._run(graph.cmd_decay, Namespace(rate=0.5))
        self.assertEqual(porcelain(self.data), [])
        self.assertEqual(subject(self.data), "chore: decay learned links")


class TestOnboardCommits(RepoCase):
    def setUp(self):
        super().setUp()
        (self.data / "profile").mkdir()
        for name in ("language.md", "working-style.md"):
            shutil.copy2(REPO_ROOT / "templates" / "profile" / name, self.data / "profile" / name)
        git(["add", "-A"], self.data)
        git(["commit", "-q", "-m", "skeleton"], self.data)

    def test_onboard_commits_the_profile_notes(self):
        write(self.data / "notes" / "staged.md", "# staged\n")
        git(["add", "notes/staged.md"], self.data)
        with redirect_stdout(StringIO()) as buf:
            onboarding._run_onboard(self.data, {}, force=False)
        self.assertIn("committed in the data repo: chore: fill in profile", buf.getvalue())
        self.assertEqual(head_files(self.data), ["profile/language.md", "profile/working-style.md"])
        self.assertEqual(porcelain(self.data), ["A  notes/staged.md"])

    def test_kept_notes_make_no_commit(self):
        with redirect_stdout(StringIO()):
            onboarding._run_onboard(self.data, {}, force=False)
            before = git(["rev-parse", "HEAD"], self.data).stdout
            onboarding._run_onboard(self.data, {}, force=False)
        self.assertEqual(git(["rev-parse", "HEAD"], self.data).stdout, before)


class TestDoctorUncommitted(RepoCase):
    def test_clean_repo_is_ok(self):
        self.assertEqual(onboarding._uncommitted_checks(self.data),
                         [("OK", "data repo: nothing uncommitted")])

    def test_engine_files_warn_and_other_changes_are_counted(self):
        write(self.data / ".github" / "workflows" / "vault.yml", "on: push\n")
        write(self.data / ".graph" / "learned" / "pc-test.json", "{}\n")
        write(self.data / "notes" / "mine.md", "# mine\n")
        checks = onboarding._uncommitted_checks(self.data)
        self.assertEqual([c[0] for c in checks], ["WARN", "INFO"])
        self.assertIn(".github/workflows/vault.yml", checks[0][1])
        self.assertIn(".graph/learned/pc-test.json", checks[0][1])
        self.assertIn("commit", checks[0][1])
        self.assertIn("1 other uncommitted change", checks[1][1])

    def test_repo_without_commits_and_plain_folder(self):
        fresh = self.tmp / "fresh"
        init_repo(fresh)
        write(fresh / "AGENTS.md", "x\n")
        checks = onboarding._uncommitted_checks(fresh)
        self.assertEqual(checks[0][0], "WARN")
        self.assertIn("no commits yet", checks[0][1])
        self.assertEqual(onboarding._uncommitted_checks(self.tmp / "missing"), [])


class TestFreshVaultFlow(unittest.TestCase):
    """init, onboarding and two tasks the way AGENTS.md describes them, against a
    local bare remote, with Git for Windows' default core.autocrlf=true."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp()).resolve()
        cls.data = cls.tmp / "vault"
        cls.remote = cls.tmp / "remote.git"
        cls.elsewhere = cls.tmp / "some-project"
        cls.elsewhere.mkdir()
        home = cls.tmp / "home"
        home.mkdir()
        gitconfig = cls.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test Runner\n\temail = tester@example.com\n"
                             "[commit]\n\tgpgsign = false\n[core]\n\tautocrlf = true\n"
                             "[init]\n\tdefaultBranch = main\n",
                             encoding="utf-8", newline="\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("VAULT_", "GIT_"))}
        env.update({"VAULT_ENGINE": str(REPO_ROOT), "VAULT_DATA": str(cls.data),
                    "VAULT_MACHINE": "pc-test", "HOME": str(home), "USERPROFILE": str(home),
                    "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
                    "VAULT_OLLAMA_URL": "http://127.0.0.1:9", "PYTHONIOENCODING": "utf-8"})
        cls.env = env

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def graph(self, args: list[str]) -> subprocess.CompletedProcess:
        result = subprocess.run([sys.executable, str(GRAPH_PATH), *args], cwd=self.elsewhere,
                                env=self.env, capture_output=True, text=True, encoding="utf-8",
                                timeout=120)
        self.assertEqual(result.returncode, 0, f"{args}: {result.stdout}\n{result.stderr}")
        return result

    def git(self, args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
        result = git(args, cwd or self.data, env=self.env)
        self.assertEqual(result.returncode, 0, f"git {args}: {result.stdout}\n{result.stderr}")
        self.assertNotIn("CRLF", result.stderr, f"git {args}")
        return result

    def task(self, notes: list[str]) -> None:
        self.git(["pull", "--rebase", "--autostash"])
        out = self.graph(["context", "release checklist"]).stdout
        hint = out.strip().splitlines()[-1]
        task = hint.split("--task ", 1)[1].split()[0]
        self.graph(["reinforce", "--task", task, *notes])
        self.git(["pull", "--rebase", "--autostash"])
        self.git(["push"])

    def test_tree_stays_clean_through_onboarding_and_two_tasks(self):
        self.graph(["init", str(self.data), "--yes"])
        self.assertTrue((self.data / ".gitattributes").is_file())
        self.git(["init", "-q", "--bare", str(self.remote)], cwd=self.tmp)
        self.git(["remote", "add", "origin", str(self.remote)])
        self.git(["push", "-q", "-u", "origin", "main"])
        self.assertEqual(porcelain(self.data), [])

        answers = self.tmp / "answers.json"
        answers.write_text("{}", encoding="utf-8")
        self.graph(["onboard", "--answers", str(answers), "--data", str(self.data)])
        self.assertEqual(porcelain(self.data), [])

        self.task(["language", "working-style"])
        # The agent writes a note and commits it itself (step 4), CRLF-free.
        write(self.data / "notes" / "release.md",
              "---\nkeywords: [release]\nlinks: []\n---\n\n# Release\n\n- Tag after merge.\n")
        self.git(["add", "notes/release.md"])
        self.git(["commit", "-q", "-m", "docs: add release note"])
        self.task(["language", "working-style", "release"])

        self.assertEqual(porcelain(self.data), [])
        doctor = subprocess.run([sys.executable, str(GRAPH_PATH), "doctor", "--data", str(self.data)],
                                cwd=self.elsewhere, env=self.env, capture_output=True, text=True,
                                encoding="utf-8", timeout=120)
        self.assertIn("data repo: nothing uncommitted", doctor.stdout)
        log = self.git(["log", "--format=%s", "origin/main"]).stdout.splitlines()
        self.assertEqual(log.count("chore: update learned links"), 2)
        self.assertIn("chore: fill in profile", log)

    def test_without_gitattributes_git_would_warn(self):
        # Proves the CRLF assertion above can fail: same config, no .gitattributes.
        plain = self.tmp / "plain"
        self.git(["init", "-q", str(plain)], cwd=self.tmp)
        write(plain / "a.md", "line\n")
        result = git(["add", "a.md"], plain, env=self.env)
        self.assertIn("CRLF", result.stderr)


if __name__ == "__main__":
    unittest.main()
