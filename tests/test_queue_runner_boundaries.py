"""Boundary tests for tools/queue_runner.py: every place the runner touches the outside world
runs for real here (shell, git, child processes, command-line quoting). Only `claude` (a fake
script behind `--claude <file>.py`) and `gh` (a fake script behind the `gh` argv) are replaced.
No network, no live claude; git runs on local temp repositories only.

Helpers are copied from test_queue_runner.py on purpose (importing that module would run its
tests a second time under discovery)."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_core import GRAPH_PATH, graph  # noqa: E402

sys.path.insert(0, str(GRAPH_PATH.parent))
import queue_runner as qr  # noqa: E402

qr.g = graph  # other test modules replace sys.modules["graph"]; bind to ours

TOOLS_DIR = GRAPH_PATH.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude_p"
FIXTURE_NAMES = ("success", "error_max_turns", "error_max_budget_usd")
PY = sys.executable
WINDOWS = sys.platform == "win32"

TASK = """---
type: task
project: {project}
status: {status}
created: {created}
keywords: [queue]
---

# {title}

## Goal
Something.

## Acceptance
```
{acceptance}
```

## Log
<!-- agent fills -->
"""

# Hermetic git: no global or system config (autocrlf, gpg signing, hooks), fixed identity.
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "Boundary Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",  # guard:ignore
           "GIT_COMMITTER_NAME": "Boundary Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",  # guard:ignore
           "GIT_TERMINAL_PROMPT": "0"}

FAKE_GH = '''\
import json, os, sys
with open(os.environ["FAKE_GH_OUT"], "w", encoding="utf-8") as fh:
    json.dump(sys.argv[1:], fh)
mode = os.environ.get("FAKE_GH_MODE", "url")
if mode == "fail":
    sys.stderr.write("GraphQL: could not resolve to a Repository\\n")
    sys.exit(1)
sys.stdout.write("Warning: 1 uncommitted change\\n\\nCreating pull request for the branch\\n")
if mode == "url":
    sys.stdout.write("https://github.com/example-owner/example-repo/pull/42\\n")
'''

FAKE_CLAUDE = '''\
import json, os, subprocess, sys
with open(os.environ["FAKE_CLAUDE_OUT"], "w", encoding="utf-8") as fh:
    json.dump({"argv": sys.argv[1:], "cwd": os.getcwd()}, fh)
with open("claude_was_here.txt", "w", encoding="utf-8") as fh:
    fh.write("work\\n")
subprocess.run(["git", "add", "claude_was_here.txt"], check=True)
subprocess.run(["git", "commit", "-q", "-m", "feat: work done by the fake claude"], check=True)
with open(os.environ["FAKE_CLAUDE_FIXTURE"], encoding="utf-8") as fh:
    sys.stdout.write(fh.read())
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
'''

FAKE_ENGINE = '''\
import json, os, sys
print(json.dumps({"VAULT_DATA": os.environ.get("VAULT_DATA"), "argv": sys.argv[1:],
                  "isolated": sys.flags.isolated}))
'''


def force_rmtree(path) -> None:
    """rmtree that also removes read-only files (git objects on Windows)."""
    def fix(func, p, *_exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    kw = {"onexc": fix} if sys.version_info >= (3, 12) else {"onerror": fix}
    shutil.rmtree(path, **kw)


def git(repo, *args, check=True):
    cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                        encoding="utf-8", check=check)
    return cp.stdout.strip()


def pycmd(code: str, *extra: str) -> str:
    """A shell string that runs `python -c "<code>"`; `code` must not hold double quotes."""
    return " ".join([f'"{PY}" -c "{code}"', *(f'"{e}"' for e in extra)])


def args(**kw):
    base = dict(action="run", dry_run=False, print_schedule=False, time="02:00", timeout=60,
                claude="claude-bin")
    base.update(kw)
    return argparse.Namespace(**base)


def fixture_text(name: str) -> str:
    return (FIXTURES / f"{name}.json").read_text(encoding="utf-8")


class TempBase(unittest.TestCase):
    """A temp root, hermetic git environment, and helpers to build bare-origin + clone pairs."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="qrb-"))
        self.addCleanup(force_rmtree, self.root)
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_origin_and_clone(self, name: str, parent: Path | None = None) -> tuple[Path, Path]:
        """(bare origin, clone) with one commit on main, pushed."""
        origin = self.root / f"{name}-origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        seed = self.root / f"{name}-seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        (seed / "README.md").write_text("seed\n", encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "seed")
        git(seed, "push", "-q", str(origin), "main")
        clone = (parent or self.root) / name
        clone.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
        return origin, clone

    def push_commit_to_origin(self, origin: Path, name: str) -> str:
        """A second clone adds a commit to origin/main; returns the new sha."""
        other = self.root / f"other-{name}"
        subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
        (other / f"{name}.txt").write_text(name, encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-qm", f"add {name}")
        git(other, "push", "-q", "origin", "main")
        return git(other, "rev-parse", "HEAD")

    def patch_gh(self, mode: str = "url") -> Path:
        """Route only the `gh ...` argv to a fake script run by a real process; returns the
        file the fake writes its argv to."""
        script = self.root / "fake_gh.py"
        script.write_text(FAKE_GH, encoding="utf-8")
        out = self.root / "gh_argv.json"
        env = mock.patch.dict(os.environ, {"FAKE_GH_OUT": str(out), "FAKE_GH_MODE": mode})
        env.start()
        self.addCleanup(env.stop)
        real_run = qr._run

        def run(argv, *a, **kw):
            if isinstance(argv, list) and argv and argv[0] == "gh":
                argv = [sys.executable, str(script), *argv[1:]]
            return real_run(argv, *a, **kw)

        patcher = mock.patch.object(qr, "_run", run)
        patcher.start()
        self.addCleanup(patcher.stop)
        return out


# --- 1. acceptance command in the real shell ----------------------------------

class AcceptanceShellTests(TempBase):
    def acceptance(self, command):
        wt = self.root / "wt"
        wt.mkdir(exist_ok=True)
        return qr.run_acceptance(command, wt)

    def test_simple_command_exit_zero(self):
        res = self.acceptance(pycmd("print(1)"))
        self.assertEqual((res["exit"], res["tail"]), (0, "1"))

    def test_and_chain_runs_both_and_keeps_both_outputs(self):
        res = self.acceptance(f"{pycmd('print(11)')} && {pycmd('print(22)')}")
        self.assertEqual(res["exit"], 0)
        self.assertEqual(res["tail"].splitlines(), ["11", "22"])

    def test_and_chain_stops_at_first_failure(self):
        res = self.acceptance(f"{pycmd('import sys; sys.exit(3)')} && {pycmd('print(22)')}")
        self.assertEqual(res["exit"], 3)
        self.assertNotIn("22", res["tail"])

    def test_double_quoted_argument_is_not_mangled(self):
        res = self.acceptance(pycmd("import sys; print(sys.argv[1])", "two  words & more"))
        self.assertEqual((res["exit"], res["tail"]), (0, "two  words & more"))

    def test_exit_code_is_returned_unchanged(self):
        for code in (1, 3, 7):
            with self.subTest(code=code):
                self.assertEqual(self.acceptance(pycmd(f"import sys; sys.exit({code})"))["exit"], code)

    def test_runs_in_the_worktree_and_merges_stderr(self):
        res = self.acceptance(pycmd("import os,sys; print(os.getcwd()); sys.stderr.write('oops')"))
        self.assertEqual(res["exit"], 0)
        first, second = res["tail"].splitlines()[0], res["tail"].splitlines()[-1]
        self.assertTrue(Path(first).samefile(self.root / "wt"))
        self.assertIn("oops", second)

    def test_unknown_command_is_nonzero(self):
        self.assertNotEqual(self.acceptance("definitely-not-a-command-0065")["exit"], 0)


# --- 2. children get no stdin ---------------------------------------------------

class StdinTests(TempBase):
    """Run the runner function in a parent whose own stdin holds data; a child that inherited
    it would read it back."""

    def child_stdin_seen(self, body: str) -> str:
        parent = "\n".join(["import sys", f"sys.path.insert(0, {str(TOOLS_DIR)!r})",
                            "import queue_runner as qr", body, ""])
        cp = subprocess.run([PY, "-c", parent], input="LEAKED-STDIN", capture_output=True,
                            text=True, timeout=120, cwd=str(self.root))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout.strip()

    def test_run_helper_child_reads_nothing(self):
        out = self.child_stdin_seen(
            "cp = qr._run([sys.executable, '-c', 'import sys; print(repr(sys.stdin.read()))'])\n"
            "print(cp.stdout)")
        self.assertEqual(out, "''")

    def test_acceptance_child_reads_nothing(self):
        cmd = pycmd("import sys; print(repr(sys.stdin.read()))")
        out = self.child_stdin_seen(f"print(qr.run_acceptance({cmd!r}, '.')['tail'])")
        self.assertEqual(out, "''")

    def test_run_passes_devnull(self):
        with mock.patch.object(qr.subprocess, "run") as run:
            qr._run(["x"])
            qr._run("y", shell=True)
        for call in run.call_args_list:
            self.assertIs(call.kwargs["stdin"], subprocess.DEVNULL)


# --- 3. real claude -p fixtures -------------------------------------------------

class FixtureParseTests(unittest.TestCase):
    def load(self, name):
        return json.loads(fixture_text(name))

    def test_no_identity_fields_anywhere(self):
        forbidden = {"session_id", "uuid", "credential_expires_at"}

        def walk(node):
            if isinstance(node, dict):
                self.assertFalse(forbidden & set(node), node.keys())
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        for name in FIXTURE_NAMES:
            with self.subTest(name=name):
                walk(self.load(name))
                text = fixture_text(name)
                self.assertNotRegex(text, r"[A-Za-z]:\\\\|/Users/|/home/")

    def test_parse_every_fixture(self):
        expected = {"success": ("success", False), "error_max_turns": ("error_max_turns", True),
                    "error_max_budget_usd": ("error_max_budget_usd", True)}
        for name, (subtype, is_error) in expected.items():
            with self.subTest(name=name):
                res = qr.parse_claude_result(fixture_text(name))
                self.assertTrue(res["parsed"])
                self.assertEqual((res["subtype"], res["is_error"]), (subtype, is_error))
                self.assertGreater(res["usd"], 0)
                self.assertGreaterEqual(res["turns"], 1)
                self.assertGreater(res["tokens"], 0)
                self.assertGreater(res["duration_ms"], 0)
                self.assertEqual(res["usd"], self.load(name)["total_cost_usd"])

    def test_success_has_a_result_errors_have_none(self):
        self.assertEqual(qr.parse_claude_result(fixture_text("success"))["result"], "ok")
        for name in ("error_max_turns", "error_max_budget_usd"):
            self.assertEqual(qr.parse_claude_result(fixture_text(name))["result"], "")

    def test_budget_subtypes_match_the_real_names(self):
        for name in ("error_max_turns", "error_max_budget_usd"):
            self.assertIn(self.load(name)["subtype"], qr.BUDGET_SUBTYPES)
        self.assertNotIn(self.load("success")["subtype"], qr.BUDGET_SUBTYPES)


class MockedQueue(unittest.TestCase):
    """execute() with claude, acceptance, worktree and PR replaced (decision logic only)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.projects = root / "projects"
        (self.data / ".graph").mkdir(parents=True)
        (self.data / "inbox" / "vault-engine").mkdir(parents=True)
        (self.projects / "vault-engine" / ".git").mkdir(parents=True)
        self.paths = graph.Paths(engine=graph.ENGINE, data=self.data)
        (self.data / ".graph" / "machine.json").write_text(json.dumps(
            {"project_roots": [str(self.projects)],
             "queue": {"enabled": True, "projects": ["vault-engine"], "budgets_usd": 5}}),
            encoding="utf-8")
        self.task_path = self.data / "inbox" / "vault-engine" / "0001-x.md"
        self.task_path.write_text(TASK.format(
            project="vault-engine", status="ready", created="2026-11-01", title="x",
            acceptance="python -c pass"), encoding="utf-8")
        self.wt = root / "wt"
        self.wt.mkdir()

    def run_queue(self, claude_stdout, claude_rc):
        qr.set_status(self.task_path, "ready")  # execute() claims the task; start each run fresh
        def fake_claude(argv, wt, timeout):
            return subprocess.CompletedProcess(argv, claude_rc, claude_stdout, ""), False

        with mock.patch.object(qr, "run_claude", fake_claude), \
                mock.patch.object(qr, "run_acceptance",
                                  lambda c, w: {"command": c, "exit": 0, "tail": "OK"}), \
                mock.patch.object(qr, "make_worktree",
                                  lambda repo, n, slug: (self.wt, f"feat/queue-{slug}")), \
                mock.patch.object(qr, "create_pr",
                                  lambda wt, b, t, body: ("https://example.invalid/pr/1", "")), \
                contextlib.redirect_stdout(io.StringIO()):
            return qr.execute(args(), self.paths)

    def test_success_fixture_opens_a_pr(self):
        event = self.run_queue(fixture_text("success"), 0)
        data = json.loads(fixture_text("success"))
        self.assertEqual(event["outcome"], "pr")
        self.assertEqual(event["usd"], data["total_cost_usd"])
        self.assertGreater(event["tokens"], 0)

    def test_budget_fixtures_with_exit_1_are_budget_hits_not_claude_errors(self):
        for name in ("error_max_turns", "error_max_budget_usd"):
            with self.subTest(name=name):
                event = self.run_queue(fixture_text(name), 1)
                self.assertEqual(event["outcome"], "blocked")
                self.assertEqual(event["reason"], f"budget hit: {name}")
                self.assertNotIn("claude error", event["reason"])
                self.assertGreater(event["usd"], 0)
                self.assertGreater(event["tokens"], 0)

    def test_other_error_subtype_stays_a_claude_error(self):
        data = json.loads(fixture_text("error_max_turns"))
        data["subtype"] = "error_during_execution"
        event = self.run_queue(json.dumps(data), 1)
        self.assertTrue(event["reason"].startswith("claude error: exit 1"), event["reason"])


# --- 4. make_worktree with real git ---------------------------------------------

class WorktreeTests(TempBase):
    def setUp(self):
        super().setUp()
        self.origin, self.repo = self.make_origin_and_clone("proj", self.root / "projects")

    def test_worktree_path_and_branch_come_from_origin_main(self):
        new_sha = self.push_commit_to_origin(self.origin, "later")  # local main is now behind
        wt, branch = qr.make_worktree(self.repo, "0007", "fix-it")
        self.assertEqual(branch, "feat/queue-fix-it")
        self.assertEqual(wt, self.repo / ".claude" / "worktrees" / "queue-0007")
        self.assertTrue((wt / "README.md").is_file())
        self.assertEqual(git(wt, "branch", "--show-current"), "feat/queue-fix-it")
        self.assertEqual(git(wt, "rev-parse", "HEAD"), new_sha)
        self.assertNotEqual(git(self.repo, "rev-parse", "main"), new_sha)  # main untouched
        self.assertEqual(git(self.repo, "branch", "--show-current"), "main")

    def test_second_call_for_the_same_task_is_a_queue_error(self):
        wt, _ = qr.make_worktree(self.repo, "0007", "fix-it")
        (wt / "wip.txt").write_text("keep me", encoding="utf-8")
        with self.assertRaises(qr.QueueError) as ctx:
            qr.make_worktree(self.repo, "0007", "fix-it")
        self.assertIn("worktree add failed", str(ctx.exception))
        self.assertEqual((wt / "wip.txt").read_text(encoding="utf-8"), "keep me")

    def test_main_checkout_stays_clean(self):
        qr.make_worktree(self.repo, "0007", "fix-it")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_exclude_entry_is_written_once(self):
        qr.make_worktree(self.repo, "0007", "one")
        qr.make_worktree(self.repo, "0008", "two")
        exclude = (self.repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertEqual(exclude.splitlines().count(".claude/worktrees/"), 1)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_fetch_failure_is_a_queue_error(self):
        git(self.repo, "remote", "set-url", "origin", str(self.root / "missing.git"))
        with self.assertRaises(qr.QueueError) as ctx:
            qr.make_worktree(self.repo, "0007", "fix-it")
        self.assertIn("git fetch failed", str(ctx.exception))


# --- 5. create_pr with a real push ----------------------------------------------

BODY = ("Task: 0065-x\n\nAcceptance (run by the queue runner): `python -c \"print(1)\"` exit 0\n"
        "```\nRan 5 tests\nOK\n```\n\nCost: $0.31, 4 turns\n\n"
        "Result with 'single' and \"double\" quotes, $HOME, %PATH%, back\\slash, trailing\\\\\n"
        "line with trailing spaces   \n\n\n- bullet `code` end\n")


class CreatePrTests(TempBase):
    def setUp(self):
        super().setUp()
        self.origin, self.repo = self.make_origin_and_clone("proj", self.root / "projects")
        self.wt, self.branch = qr.make_worktree(self.repo, "0065", "boundary")
        (self.wt / "change.txt").write_text("x\n", encoding="utf-8")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-qm", "feat: change")

    def test_push_reaches_origin_and_body_arrives_byte_for_byte(self):
        out = self.patch_gh()
        url, err = qr.create_pr(self.wt, self.branch, "vault-engine: boundary", BODY)
        self.assertEqual((url, err), ("https://github.com/example-owner/example-repo/pull/42", ""))
        remote = subprocess.run(["git", "ls-remote", "--heads", str(self.origin)],
                                capture_output=True, text=True, check=True).stdout
        self.assertIn(f"{git(self.wt, 'rev-parse', 'HEAD')}	refs/heads/{self.branch}", remote)
        argv = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(argv[:2], ["pr", "create"])
        self.assertEqual(argv[argv.index("--body") + 1], BODY)
        self.assertEqual(argv[argv.index("--title") + 1], "vault-engine: boundary")
        self.assertEqual(argv[argv.index("--head") + 1], self.branch)
        self.assertEqual(argv[argv.index("--base") + 1], "main")

    def test_url_is_taken_from_realistic_multiline_output(self):
        self.patch_gh("url")
        url, _ = qr.create_pr(self.wt, self.branch, "t", "b")
        self.assertEqual(url, "https://github.com/example-owner/example-repo/pull/42")

    def test_push_failure_blocks_with_reason_and_never_calls_gh(self):
        out = self.patch_gh()
        git(self.wt, "remote", "set-url", "origin", str(self.root / "missing.git"))
        url, err = qr.create_pr(self.wt, self.branch, "t", "b")
        self.assertIsNone(url)
        self.assertTrue(err.startswith("git push failed"), err)
        self.assertFalse(out.exists())

    def test_gh_output_without_url_gives_a_reason(self):
        self.patch_gh("nourl")
        url, err = qr.create_pr(self.wt, self.branch, "t", "b")
        self.assertEqual((url, err), (None, "gh returned no PR url"))

    def test_gh_failure_gives_a_reason(self):
        self.patch_gh("fail")
        url, err = qr.create_pr(self.wt, self.branch, "t", "b")
        self.assertIsNone(url)
        self.assertTrue(err.startswith("gh pr create failed"), err)
        self.assertIn("could not resolve", err)


# --- 6. schedule ------------------------------------------------------------------

def windows_argv(command_line: str) -> list[str]:
    """Split with the Windows rules (CommandLineToArgvW), as schtasks / CreateProcess do."""
    import ctypes
    from ctypes import wintypes
    shell32 = ctypes.windll.shell32
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    count = ctypes.c_int()
    ptr = shell32.CommandLineToArgvW(command_line, ctypes.byref(count))
    if not ptr:
        raise OSError("CommandLineToArgvW failed")
    try:
        return [ptr[i] for i in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(ptr)


def printed_windows_line(text: str) -> str:
    return next(ln for ln in text.splitlines() if ln.startswith("schtasks "))


class ScheduleTests(TempBase):
    def fake_engine(self, base: str = "my engine dir") -> graph.Paths:
        engine = self.root / base
        (engine / "tools").mkdir(parents=True)
        (engine / "tools" / "queue_runner.py").write_text(FAKE_ENGINE, encoding="utf-8")
        data = self.root / "my data dir"
        data.mkdir()
        return graph.Paths(engine=engine, data=data)

    @staticmethod
    def tr_value(text: str) -> str:
        """The /TR argument without a Windows API (undo the \\" escaping)."""
        line = printed_windows_line(text)
        return line.split(' /TR "', 1)[1][:-1].replace('\\"', '"')

    def test_tr_over_261_characters_raises(self):
        paths = graph.Paths(engine=Path("E:/engine"), data=Path("C:/" + "d" * 300))
        with self.assertRaises(qr.QueueError) as ctx:
            qr.schedule_text(paths, "02:00")
        self.assertIn("261", str(ctx.exception))

    def test_tr_limit_is_exact(self):
        probe = qr.schedule_text(graph.Paths(engine=Path("E:/engine"), data=Path("C:/d")), "02:00")
        pad = qr.SCHTASKS_TR_LIMIT - len(self.tr_value(probe))
        self.assertGreater(pad, 0)
        ok = graph.Paths(engine=Path("E:/engine"), data=Path("C:/d" + "x" * pad))
        self.assertEqual(len(self.tr_value(qr.schedule_text(ok, "02:00"))), qr.SCHTASKS_TR_LIMIT)
        over = graph.Paths(engine=Path("E:/engine"), data=Path("C:/d" + "x" * (pad + 1)))
        with self.assertRaises(qr.QueueError):
            qr.schedule_text(over, "02:00")

    @unittest.skipUnless(WINDOWS, "Windows command-line rules")
    def test_windows_line_parses_and_payload_runs_from_a_path_with_spaces(self):
        paths = self.fake_engine()
        argv = windows_argv(printed_windows_line(qr.schedule_text(paths, "03:30")))
        self.assertEqual(argv[:6], ["schtasks", "/Create", "/SC", "DAILY", "/ST", "03:30"])
        self.assertEqual(argv[argv.index("/TN") + 1], "vault-queue-runner")
        payload = argv[argv.index("/TR") + 1]
        self.assertLessEqual(len(payload), 261)
        # Task Scheduler starts the /TR text as a command line; so do we (no extra shell).
        cp = subprocess.run(payload, capture_output=True, text=True, timeout=120,
                            stdin=subprocess.DEVNULL, cwd=str(self.root))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        seen = json.loads(cp.stdout)
        self.assertEqual(seen["VAULT_DATA"], str(paths.data))
        self.assertEqual(seen["argv"], ["run"])
        self.assertEqual(seen["isolated"], 1)

    @unittest.skipIf(WINDOWS, "POSIX cron line")
    def test_cron_line_runs_with_sh(self):
        paths = self.fake_engine()
        text = qr.schedule_text(paths, "03:30")
        line = next(ln for ln in text.splitlines() if re.match(r"^\d+ \d+ \* \* \* ", ln))
        self.assertTrue(line.startswith("30 3 * * * "))
        command = line.split(None, 5)[5]  # what cron hands to /bin/sh -c
        cp = subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=120,
                            stdin=subprocess.DEVNULL, cwd=str(self.root))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        seen = json.loads(cp.stdout)
        self.assertEqual(seen["VAULT_DATA"], str(paths.data))
        self.assertEqual(seen["argv"], ["run"])
        self.assertEqual(seen["isolated"], 1)


# --- 7. end to end: only claude and gh are fakes -----------------------------------

class EndToEndTests(TempBase):
    def setUp(self):
        super().setUp()
        self.projects = self.root / "projects"
        self.proj_origin, self.repo = self.make_origin_and_clone("vault-engine", self.projects)
        self.repo = self.repo.resolve()
        # data repo with its own bare origin so claim / push / pull run for real too
        self.data_origin = self.root / "data-origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.data_origin)],
                       check=True)
        self.data = self.root / "data"
        (self.data / ".graph").mkdir(parents=True)
        (self.data / "inbox" / "vault-engine").mkdir(parents=True)
        (self.data / ".gitignore").write_text(".graph/\n", encoding="utf-8")
        (self.data / ".graph" / "machine.json").write_text(json.dumps(
            {"project_roots": [str(self.projects)],
             "queue": {"enabled": True, "projects": ["vault-engine"], "budgets_usd": 5,
                       "max_turns": 7}}), encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.data)], check=True)
        self.paths = graph.Paths(engine=graph.ENGINE, data=self.data)
        self.fake_claude = self.root / "fake_claude.py"
        self.fake_claude.write_text(FAKE_CLAUDE, encoding="utf-8")
        self.claude_out = self.root / "claude_call.json"
        self.gh_out = self.patch_gh()

    def run_scenario(self, acceptance: str, fixture: str = "success", claude_exit: int = 0):
        self.task_path = self.data / "inbox" / "vault-engine" / "0001-fix-thing.md"
        self.task_path.write_text(TASK.format(project="vault-engine", status="ready",
                                              created="2026-11-01", title="fix thing",
                                              acceptance=acceptance), encoding="utf-8")
        git(self.data, "add", "-A")
        git(self.data, "commit", "-qm", "init")
        git(self.data, "remote", "add", "origin", str(self.data_origin))
        git(self.data, "push", "-q", "-u", "origin", "main")
        env = mock.patch.dict(os.environ, {
            "FAKE_CLAUDE_OUT": str(self.claude_out),
            "FAKE_CLAUDE_FIXTURE": str(FIXTURES / f"{fixture}.json"),
            "FAKE_CLAUDE_EXIT": str(claude_exit)})
        with env, contextlib.redirect_stdout(io.StringIO()) as out:
            event = qr.execute(args(claude=str(self.fake_claude)), self.paths)
        self.stdout = out.getvalue()
        return event

    def events(self):
        log = self.data / ".graph" / "usage.log"
        return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]

    def status(self):
        return re.search(r"^status: (\S+)$", self.task_path.read_text(encoding="utf-8"), re.M).group(1)

    def test_green_acceptance_opens_a_pr(self):
        accept = pycmd("import os,sys; sys.exit(0 if os.path.exists('claude_was_here.txt') else 9)")
        event = self.run_scenario(accept)
        usd = json.loads(fixture_text("success"))["total_cost_usd"]
        pr = "https://github.com/example-owner/example-repo/pull/42"
        self.assertEqual((event["outcome"], event["pr"], event["usd"]), ("pr", pr, usd))
        # the task moved on and says so
        self.assertEqual(self.status(), "review")
        log = self.task_path.read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn(f"queue-runner: PR {pr}", log)
        self.assertIn("acceptance green", log)
        # usage event
        last = self.events()[-1]
        self.assertEqual((last["event"], last["outcome"], last["task"]), ("queue_run", "pr", "inbox/vault-engine/0001-fix-thing"))
        self.assertEqual(last["usd"], usd)
        # the branch really reached origin, with the fake claude's commit
        heads = subprocess.run(["git", "ls-remote", "--heads", str(self.proj_origin)],
                               capture_output=True, text=True, check=True).stdout
        self.assertIn("refs/heads/feat/queue-fix-thing", heads)
        # gh got the PR text
        gh = json.loads(self.gh_out.read_text(encoding="utf-8"))
        self.assertEqual(gh[gh.index("--head") + 1], "feat/queue-fix-thing")
        self.assertIn("exit 0", gh[gh.index("--body") + 1])
        # neither checkout is dirty, and the data repo carries the final status upstream
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(git(self.data, "status", "--porcelain"), "")
        self.assertEqual(git(self.data, "rev-parse", "HEAD"), git(self.data_origin, "rev-parse", "main"))

    def test_prompt_arrives_as_one_argument_with_its_line_breaks(self):
        self.run_scenario(pycmd("pass"))
        call = json.loads(self.claude_out.read_text(encoding="utf-8"))
        argv = call["argv"]
        wt = self.repo / ".claude" / "worktrees" / "queue-0001"
        self.assertTrue(Path(call["cwd"]).samefile(wt))
        self.assertEqual(argv[0], "-p")
        self.assertEqual(argv[2:], ["--permission-mode", "auto", "--max-turns", "7",
                                    "--max-budget-usd", "5", "--output-format", "json"])
        prompt = argv[1]
        expected = qr.PROMPT.format(task_path=self.task_path, task_id="inbox/vault-engine/0001-fix-thing",
                                    project="vault-engine", worktree=wt,
                                    branch="feat/queue-fix-thing")
        self.assertEqual(prompt, expected)
        self.assertGreater(prompt.count("\n"), 3)

    def test_red_acceptance_blocks_and_logs_the_exit_code(self):
        event = self.run_scenario(pycmd("import sys; print('boom'); sys.exit(3)"))
        self.assertEqual((event["outcome"], event["reason"]), ("blocked", "acceptance red (exit 3)"))
        self.assertEqual(self.status(), "blocked")
        log = self.task_path.read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn("blocked, acceptance red (exit 3)", log)
        self.assertIn("acceptance exit 3", log)
        self.assertIn("boom", log)
        self.assertEqual(self.events()[-1]["outcome"], "blocked")
        self.assertFalse(self.gh_out.exists())  # no PR attempt
        heads = subprocess.run(["git", "ls-remote", "--heads", str(self.proj_origin)],
                               capture_output=True, text=True, check=True).stdout
        self.assertNotIn("feat/queue-fix-thing", heads)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_budget_fixture_blocks_even_with_green_acceptance(self):
        event = self.run_scenario(pycmd("pass"), fixture="error_max_budget_usd", claude_exit=1)
        self.assertEqual((event["outcome"], event["reason"]),
                         ("blocked", "budget hit: error_max_budget_usd"))
        self.assertGreater(event["usd"], 0)
        self.assertFalse(self.gh_out.exists())


if __name__ == "__main__":
    unittest.main()
