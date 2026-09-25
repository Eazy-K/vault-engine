"""Tests for tools/schema.py (data-repo schema version and migrate) and for
the require_writable guard wired into save_learned/mv/onboard/init/feedback-set.

Uses only tempfile-based directories and temp git repos with a fake local
identity; never touches the real engine or any user's notes. Run with:
    python -m unittest discover -s tests -v
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
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

# Never let a test reach the real user's data repo through the environment
# (a missing patch then fails loudly instead of writing into it).
for _var in ("VAULT_DATA", "VAULT_HOME"):
    os.environ.pop(_var, None)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
GRAPH_PATH = TOOLS_DIR / "graph.py"

# Load graph.py the same way the other tool test files do, then load schema.py
# for the first time in this process so its own `import graph as g` binds to
# this exact instance.
_spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)

sys.path.insert(0, str(TOOLS_DIR))
import schema  # noqa: E402  (path must be set up first)
import move  # noqa: E402  (may already be cached by test_move.py; that's fine)
import onboarding  # noqa: E402
import feedback  # noqa: E402


def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8")


def init_repo(root: Path, name: str = "Test Runner", email: str = "tester@example.com") -> None:
    root.mkdir(parents=True, exist_ok=True)
    git(["init", "-q"], root)
    git(["config", "user.email", email], root)
    git(["config", "user.name", name], root)
    git(["config", "commit.gpgsign", "false"], root)


def write_config(paths: "graph.Paths", data: dict) -> None:
    paths.config_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8", newline="\n")


class SchemaTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.paths = graph.Paths(graph.ENGINE, self.data)


# --- read_schema / require_writable -------------------------------------------

class TestReadSchema(SchemaTestCase):
    def test_missing_file_is_schema_1(self):
        self.assertEqual(schema.read_schema(self.paths), 1)

    def test_missing_field_is_schema_1(self):
        write_config(self.paths, {"project_roots": ["/x"]})
        self.assertEqual(schema.read_schema(self.paths), 1)

    def test_invalid_json_is_schema_1(self):
        self.paths.config_file.write_text("{not json", encoding="utf-8", newline="\n")
        self.assertEqual(schema.read_schema(self.paths), 1)

    def test_explicit_field_is_read(self):
        write_config(self.paths, {"schema": 4})
        self.assertEqual(schema.read_schema(self.paths), 4)

    def test_non_int_field_falls_back_to_1(self):
        write_config(self.paths, {"schema": "four"})
        self.assertEqual(schema.read_schema(self.paths), 1)


class TestRequireWritable(SchemaTestCase):
    def test_passes_when_equal_or_older(self):
        write_config(self.paths, {"schema": 1})
        schema.require_writable(self.paths)  # must not raise

    def test_refuses_newer_vault(self):
        write_config(self.paths, {"schema": 99})
        with self.assertRaises(SystemExit) as ctx:
            schema.require_writable(self.paths)
        self.assertIn("newer engine", str(ctx.exception))


# --- migrate -------------------------------------------------------------------

def _step_one(paths: "graph.Paths") -> list[Path]:
    marker = paths.data / "migrated-1.txt"
    marker.write_text("one\n", encoding="utf-8", newline="\n")
    return [marker]


def _step_two(paths: "graph.Paths") -> list[Path]:
    marker = paths.data / "migrated-2.txt"
    marker.write_text("two\n", encoding="utf-8", newline="\n")
    return [marker]


class MigrateRepoCase(SchemaTestCase):
    """A data repo with vault.config.json committed, like a v0.2.0 vault."""

    def commit_config(self, data: dict) -> None:
        write_config(self.paths, data)
        init_repo(self.data)
        git(["add", "vault.config.json"], self.data)
        git(["commit", "-q", "-m", "initial"], self.data)

    def log(self) -> list[str]:
        return git(["log", "--format=%s"], self.data).stdout.strip().splitlines()

    def head_files(self) -> list[str]:
        return git(["show", "--name-only", "--format=", "HEAD"], self.data).stdout.split()

    def config(self) -> dict:
        return json.loads(self.paths.config_file.read_text(encoding="utf-8"))

    def machine(self) -> dict:
        return json.loads((self.data / ".graph" / "machine.json").read_text(encoding="utf-8"))

    def other_root(self) -> Path:
        root = self.tmp / "projects"
        root.mkdir(exist_ok=True)
        return root

    def missing_root(self) -> str:
        return str(self.tmp / "other-computer" / "Dev")


class TestMigrate(MigrateRepoCase):
    def test_no_op_when_already_current_and_field_explicit(self):
        self.commit_config({"schema": 1})
        result = schema.migrate(self.paths)
        self.assertTrue(result.plan.empty)
        self.assertFalse(result.written)
        self.assertIsNone(result.committed)
        self.assertEqual(self.log(), ["initial"])

    def test_writes_implicit_field_when_current_and_missing(self):
        self.commit_config({"feedback": {"level": "off"}})
        result = schema.migrate(self.paths)
        self.assertTrue(result.plan.record_field)
        self.assertEqual(result.committed, "chore: record vault schema 1")
        self.assertEqual(self.log()[0], "chore: record vault schema 1")
        self.assertEqual(self.config(), {"feedback": {"level": "off"}, "schema": 1})

    def test_not_a_git_repo_just_writes(self):
        write_config(self.paths, {"feedback": {"level": "off"}})
        result = schema.migrate(self.paths)
        self.assertTrue(result.written)
        self.assertIsNone(result.committed)
        self.assertIn("not a git repo", result.uncommitted_reason)
        self.assertEqual(self.config()["schema"], 1)

    def test_refuses_newer_vault(self):
        write_config(self.paths, {"schema": 99})
        with self.assertRaises(SystemExit):
            schema.migrate(self.paths)

    def test_missing_step_raises_before_writing(self):
        write_config(self.paths, {"feedback": {"level": "off"}})  # schema 1
        before = self.paths.config_file.read_bytes()
        with mock.patch.object(schema, "SCHEMA_VERSION", 3), \
             mock.patch.object(schema, "MIGRATIONS", {1: _step_one}):  # step 2->3 missing
            with self.assertRaises(SystemExit) as ctx:
                schema.migrate(self.paths, commit=False)
        self.assertIn("2 -> 3", str(ctx.exception))
        self.assertEqual(self.paths.config_file.read_bytes(), before)
        self.assertFalse((self.data / "migrated-1.txt").exists())

    def test_two_step_migration_writes_schema_preserves_keys_and_commits_once(self):
        self.commit_config({"feedback": {"level": "off"}, "exclude": ["tmp"]})

        with mock.patch.object(schema, "SCHEMA_VERSION", 3), \
             mock.patch.object(schema, "MIGRATIONS", {1: _step_one, 2: _step_two}):
            result = schema.migrate(self.paths)

        self.assertEqual(result.plan.steps, [2, 3])
        data = self.config()
        self.assertEqual(list(data), ["feedback", "exclude", "schema"])  # order kept
        self.assertEqual(data["schema"], 3)
        self.assertTrue((self.data / "migrated-1.txt").exists())
        self.assertTrue((self.data / "migrated-2.txt").exists())

        log = self.log()
        self.assertEqual(len(log), 2)  # initial commit + exactly one migration commit
        self.assertEqual(log[0], "chore: migrate vault schema 1 -> 3")
        self.assertEqual(sorted(self.head_files()),
                         ["migrated-1.txt", "migrated-2.txt", "vault.config.json"])

    def test_no_commit_flag_leaves_changes_uncommitted(self):
        self.commit_config({"feedback": {"level": "off"}})

        with mock.patch.object(schema, "SCHEMA_VERSION", 2), \
             mock.patch.object(schema, "MIGRATIONS", {1: _step_one}):
            result = schema.migrate(self.paths, commit=False)

        self.assertEqual(result.uncommitted_reason, "--no-commit")
        self.assertEqual(self.log(), ["initial"])
        status = git(["status", "--porcelain"], self.data).stdout
        self.assertIn("vault.config.json", status)

    def test_dry_run_writes_nothing(self):
        root = self.other_root()
        self.commit_config({"project_roots": [str(root)]})
        before = self.paths.config_file.read_bytes()
        result = schema.migrate(self.paths, dry_run=True)
        self.assertEqual(result.plan.roots_action, "move")
        self.assertTrue(result.plan.record_field)
        self.assertFalse(result.written)
        self.assertEqual(self.paths.config_file.read_bytes(), before)
        self.assertFalse((self.data / ".graph" / "machine.json").exists())
        self.assertEqual(self.log(), ["initial"])


class TestMigrateStagedChanges(MigrateRepoCase):
    def test_unrelated_staged_changes_stay_staged_and_out_of_the_commit(self):
        self.commit_config({"feedback": {"level": "off"}})
        (self.data / "unrelated.txt").write_text("pending\n", encoding="utf-8", newline="\n")
        git(["add", "unrelated.txt"], self.data)

        with mock.patch.object(schema, "SCHEMA_VERSION", 2), \
             mock.patch.object(schema, "MIGRATIONS", {1: _step_one}):
            result = schema.migrate(self.paths)

        self.assertEqual(result.committed, "chore: migrate vault schema 1 -> 2")
        self.assertEqual(len(self.log()), 2)
        self.assertEqual(sorted(self.head_files()), ["migrated-1.txt", "vault.config.json"])
        staged = git(["diff", "--cached", "--name-only"], self.data).stdout.split()
        self.assertEqual(staged, ["unrelated.txt"])
        # A rerun has nothing left to do and makes no commit.
        with mock.patch.object(schema, "SCHEMA_VERSION", 2), \
             mock.patch.object(schema, "MIGRATIONS", {1: _step_one}):
            again = schema.migrate(self.paths)
        self.assertTrue(again.plan.empty)
        self.assertIsNone(again.committed)
        self.assertEqual(len(self.log()), 2)

    def test_own_uncommitted_config_edit_blocks_and_writes_nothing(self):
        self.commit_config({"feedback": {"level": "off"}})
        write_config(self.paths, {"feedback": {"level": "reports"}})  # the user's edit
        git(["add", "vault.config.json"], self.data)
        before = self.paths.config_file.read_bytes()
        with self.assertRaises(SystemExit) as ctx:
            schema.migrate(self.paths)
        self.assertIn("uncommitted changes", str(ctx.exception))
        self.assertIn("nothing was written", str(ctx.exception))
        self.assertEqual(self.paths.config_file.read_bytes(), before)
        self.assertEqual(self.log(), ["initial"])

    def test_rerun_commits_an_earlier_uncommitted_migration(self):
        self.commit_config({"feedback": {"level": "off"}})
        schema.migrate(self.paths, commit=False)
        self.assertEqual(self.log(), ["initial"])

        result = schema.migrate(self.paths)
        self.assertTrue(result.plan.empty)
        self.assertIsNotNone(result.leftover)
        self.assertEqual(result.committed, "chore: record vault schema 1")
        self.assertEqual(self.log()[0], "chore: record vault schema 1")
        self.assertEqual(git(["status", "--porcelain"], self.data).stdout, "")

        third = schema.migrate(self.paths)
        self.assertIsNone(third.committed)
        self.assertEqual(len(self.log()), 2)

    def test_v030_leftover_plus_roots_move_is_one_commit(self):
        # v0.3.0's migrate wrote "schema" but left it uncommitted when something
        # else was staged, and never touched project_roots.
        root = self.other_root()
        self.commit_config({"project_roots": [str(root)]})
        write_config(self.paths, {"project_roots": [str(root)], "schema": 1})

        result = schema.migrate(self.paths)
        self.assertEqual(result.plan.roots_action, "move")
        self.assertEqual(result.committed,
                         "chore: record vault schema 1, move project_roots to machine.json")
        self.assertEqual(len(self.log()), 2)
        self.assertEqual(self.config(), {"schema": 1})
        self.assertEqual(self.machine()["project_roots"], [str(root)])


class TestMigrateProjectRoots(MigrateRepoCase):
    def test_moves_existing_roots_to_machine_json_in_one_commit(self):
        root = self.other_root()
        self.commit_config({"project_roots": [str(root)], "feedback": {"level": "off"}})
        (self.data / ".graph").mkdir()
        (self.data / ".graph" / "machine.json").write_text(
            json.dumps({"updates": {"check": False}}), encoding="utf-8", newline="\n")

        result = schema.migrate(self.paths)

        self.assertEqual(result.plan.roots_action, "move")
        self.assertEqual(self.config(), {"feedback": {"level": "off"}, "schema": 1})
        self.assertEqual(self.machine(), {"updates": {"check": False}, "project_roots": [str(root)]})
        self.assertEqual(len(self.log()), 2)
        self.assertEqual(self.head_files(), ["vault.config.json"])  # machine.json never committed
        self.assertEqual(graph.project_roots(self.paths), [root.resolve()])
        self.assertEqual(schema.shared_project_roots(self.paths), [])

    def test_engine_parent_is_just_removed(self):
        default = str(graph.ENGINE.resolve().parent)
        self.commit_config({"schema": 1, "project_roots": [default]})
        result = schema.migrate(self.paths)
        self.assertEqual(result.plan.roots_action, "drop")
        self.assertEqual(result.committed, "chore: remove project_roots from the shared config")
        self.assertEqual(self.config(), {"schema": 1})
        self.assertFalse((self.data / ".graph" / "machine.json").exists())

    def test_roots_missing_here_stay_with_a_note(self):
        missing = self.missing_root()
        self.commit_config({"project_roots": [missing]})
        result = schema.migrate(self.paths)
        self.assertEqual(result.plan.roots_action, "keep")
        self.assertTrue(any("do not exist on this computer" in n for n in result.plan.notes))
        self.assertEqual(result.committed, "chore: record vault schema 1")
        self.assertEqual(self.config(), {"project_roots": [missing], "schema": 1})
        self.assertFalse((self.data / ".graph" / "machine.json").exists())

    def test_machine_json_roots_are_not_overwritten(self):
        root = self.other_root()
        own = self.tmp / "own"
        own.mkdir()
        self.commit_config({"schema": 1, "project_roots": [str(root)]})
        (self.data / ".graph").mkdir()
        (self.data / ".graph" / "machine.json").write_text(
            json.dumps({"project_roots": [str(own)]}), encoding="utf-8", newline="\n")
        result = schema.migrate(self.paths)
        self.assertEqual(result.plan.roots_action, "drop")
        self.assertEqual(self.machine(), {"project_roots": [str(own)]})
        self.assertNotIn("project_roots", self.config())


class TestCmdMigrate(MigrateRepoCase):
    def _run(self, **kw) -> str:
        args = Namespace(yes=kw.get("yes", True), no_commit=kw.get("no_commit", False),
                         dry_run=kw.get("dry_run", False))
        with mock.patch.object(graph, "default_paths", return_value=self.paths), \
             redirect_stdout(StringIO()) as buf:
            schema.cmd_migrate(args)
        return buf.getvalue()

    def test_up_to_date_prints_one_line_verdict(self):
        self.commit_config({"schema": 1})
        out = self._run()
        self.assertIn("vault schema 1; this engine writes schema 1", out)
        self.assertIn("nothing to migrate", out)
        self.assertNotIn("committed", out)

    def test_implicit_field_output_is_consistent(self):
        self.commit_config({"feedback": {"level": "off"}})
        out = self._run()
        self.assertIn("vault schema 1 (implicit)", out)
        self.assertIn("migrate will:", out)
        self.assertIn('record "schema": 1', out)
        self.assertIn("committed: chore: record vault schema 1", out)
        self.assertNotIn("up to date", out)
        # Rerun: the same header, then nothing to do.
        again = self._run()
        self.assertIn("nothing to migrate", again)

    def test_dry_run_prints_plan_and_writes_nothing(self):
        self.commit_config({"feedback": {"level": "off"}})
        before = self.paths.config_file.read_bytes()
        out = self._run(yes=False, dry_run=True)
        self.assertIn("migrate will:", out)
        self.assertIn("dry run: nothing written", out)
        self.assertEqual(self.paths.config_file.read_bytes(), before)

    def test_non_interactive_without_yes_writes_nothing(self):
        self.commit_config({"feedback": {"level": "off"}})
        before = self.paths.config_file.read_bytes()
        with mock.patch("sys.stdin.isatty", return_value=False), \
             self.assertRaises(SystemExit) as ctx:
            self._run(yes=False)
        self.assertIn("--yes", str(ctx.exception))
        self.assertEqual(self.paths.config_file.read_bytes(), before)

    def test_leftover_is_reported_and_committed(self):
        self.commit_config({"feedback": {"level": "off"}})
        self._run(no_commit=True)
        out = self._run()
        self.assertIn("never committed", out)
        self.assertIn("committed: chore: record vault schema 1", out)


# --- schema_of_engine_ref -------------------------------------------------------

class TestSchemaOfEngineRef(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engine = self.tmp / "engine"
        init_repo(self.engine)
        (self.engine / "README.md").write_text("x\n", encoding="utf-8", newline="\n")
        git(["add", "README.md"], self.engine)
        git(["commit", "-q", "-m", "before schema.py"], self.engine)
        self.old_ref = git(["rev-parse", "HEAD"], self.engine).stdout.strip()

        (self.engine / "tools").mkdir()
        (self.engine / "tools" / "schema.py").write_text(
            "SCHEMA_VERSION = 2\n", encoding="utf-8", newline="\n")
        git(["add", "tools/schema.py"], self.engine)
        git(["commit", "-q", "-m", "add schema.py"], self.engine)
        self.new_ref = git(["rev-parse", "HEAD"], self.engine).stdout.strip()

    def test_file_absent_at_ref_is_schema_1(self):
        self.assertEqual(schema.schema_of_engine_ref(self.engine, self.old_ref), 1)

    def test_file_present_is_parsed(self):
        self.assertEqual(schema.schema_of_engine_ref(self.engine, self.new_ref), 2)

    def test_bad_ref_is_none(self):
        self.assertIsNone(schema.schema_of_engine_ref(self.engine, "not-a-real-ref"))


# --- write guards on commands --------------------------------------------------

class TestSaveLearnedGuard(SchemaTestCase):
    def test_refuses_on_newer_vault(self):
        write_config(self.paths, {"schema": 99})
        g = graph.Graph(self.paths)
        with self.assertRaises(SystemExit):
            g.save_learned()

    def test_works_on_current_vault(self):
        write_config(self.paths, {"schema": 1})
        g = graph.Graph(self.paths)
        g.save_learned()  # must not raise
        self.assertTrue((self.paths.learned_dir / f"{g.machine}.json").exists())

    def test_reinforce_refuses_before_printing_unsaved_changes(self):
        write_config(self.paths, {"schema": 99})
        for name in ("alpha", "beta"):
            (self.data / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8", newline="\n")
        args = Namespace(notes=["alpha", "beta"], task=None, rate=graph.LEARNING_RATE)
        with mock.patch.object(graph, "default_paths", return_value=self.paths), \
             redirect_stdout(StringIO()) as out, self.assertRaises(SystemExit):
            graph.cmd_reinforce(args)
        self.assertNotIn("->", out.getvalue())


class TestMoveGuard(SchemaTestCase):
    def test_cmd_mv_refuses_on_newer_vault_before_any_change(self):
        write_config(self.paths, {"schema": 99})
        (self.data / "a.md").write_text("---\n---\n# A\n", encoding="utf-8", newline="\n")
        with mock.patch.dict(os.environ, {"VAULT_DATA": str(self.data)}), \
             mock.patch.object(move.g, "default_paths", return_value=self.paths):
            with self.assertRaises(SystemExit):
                move.cmd_mv(Namespace(source="a", dest="b"))
        self.assertTrue((self.data / "a.md").exists())
        self.assertFalse((self.data / "b.md").exists())


class TestFeedbackSetGuard(SchemaTestCase):
    def test_refuses_shared_config_write_on_newer_vault(self):
        write_config(self.paths, {"schema": 99})
        with mock.patch.object(feedback.g, "default_paths", return_value=self.paths):
            with self.assertRaises(SystemExit):
                feedback.cmd_set(Namespace(level="metrics", mode=None, repo=None, machine=False))

    def test_machine_override_bypasses_guard(self):
        write_config(self.paths, {"schema": 99})
        with mock.patch.object(feedback.g, "default_paths", return_value=self.paths):
            feedback.cmd_set(Namespace(level="off", mode=None, repo=None, machine=True))  # must not raise
        data = json.loads((self.data / ".graph" / "machine.json").read_text(encoding="utf-8"))
        self.assertEqual(data["feedback"]["level"], "off")


class TestOnboardGuard(SchemaTestCase):
    def test_cmd_onboard_refuses_on_newer_vault(self):
        write_config(self.paths, {"schema": 99})
        answers_file = self.tmp / "answers.json"
        answers_file.write_text(json.dumps({}), encoding="utf-8", newline="\n")
        with self.assertRaises(SystemExit), redirect_stdout(StringIO()):
            onboarding.cmd_onboard(Namespace(questions=False, answers=str(answers_file),
                                             data=str(self.data), force=False))
        self.assertFalse((self.data / "profile").exists())


class TestInitGuard(SchemaTestCase):
    def _args(self, target: Path, **kw):
        base = dict(dir=str(target), feedback=None, feedback_mode=None,
                    project_root=None, yes=True)
        base.update(kw)
        return Namespace(**base)

    def test_refuses_when_existing_config_is_newer(self):
        target = self.tmp / "vault"
        target.mkdir()
        write_config(graph.Paths(graph.ENGINE, target), {"schema": 99})
        with self.assertRaises(SystemExit), redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))
        self.assertFalse((target / "AGENTS.md").exists())

    def test_fresh_target_is_not_guarded(self):
        target = self.tmp / "fresh-vault"
        with mock.patch("sys.stdin.isatty", return_value=False), redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))  # must not raise
        self.assertTrue((target / "AGENTS.md").exists())

    def test_keeps_existing_config_keys(self):
        target = self.tmp / "existing-vault"
        target.mkdir()
        write_config(graph.Paths(graph.ENGINE, target), {
            "project_roots": ["/custom/root"],
            "feedback": {"level": "metrics", "mode": "auto"},
            "custom_key": "kept",
        })
        with mock.patch("sys.stdin.isatty", return_value=False), redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))
        data = json.loads((target / "vault.config.json").read_text(encoding="utf-8"))
        self.assertEqual(data["project_roots"], ["/custom/root"])
        self.assertEqual(data["feedback"], {"level": "metrics", "mode": "auto"})
        self.assertEqual(data["custom_key"], "kept")
        self.assertEqual(data["schema"], schema.SCHEMA_VERSION)

    def test_does_not_add_this_computers_roots_to_a_shared_config(self):
        target = self.tmp / "shared-vault"
        target.mkdir()
        write_config(graph.Paths(graph.ENGINE, target), {"feedback": {"level": "off", "mode": "ask"}})
        with mock.patch("sys.stdin.isatty", return_value=False), redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))
        data = json.loads((target / "vault.config.json").read_text(encoding="utf-8"))
        self.assertNotIn("project_roots", data)

    def test_explicit_flags_override_existing_config(self):
        target = self.tmp / "override-vault"
        target.mkdir()
        write_config(graph.Paths(graph.ENGINE, target), {
            "project_roots": ["/old/root"], "feedback": {"level": "off", "mode": "ask"},
        })
        with redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target, feedback="reports", feedback_mode="auto",
                                           project_root=["/new/root"]))
        data = json.loads((target / "vault.config.json").read_text(encoding="utf-8"))
        # --project-root is per computer: it goes to machine.json, and the
        # shared config's own project_roots is left untouched.
        self.assertEqual(data["project_roots"], ["/old/root"])
        self.assertEqual(data["feedback"], {"level": "reports", "mode": "auto"})
        machine = json.loads((target / ".graph" / "machine.json").read_text(encoding="utf-8"))
        self.assertEqual(machine["project_roots"], ["/new/root"])


# --- reading commands stay ungated ---------------------------------------------

class TestReadingStillWorks(SchemaTestCase):
    def test_context_and_lint_work_on_newer_vault(self):
        write_config(self.paths, {"schema": 99})
        with mock.patch.dict(os.environ, {"VAULT_DATA": str(self.data)}), \
             mock.patch.object(graph, "default_paths", return_value=self.paths), \
             redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                graph.cmd_lint(Namespace())
        self.assertEqual(ctx.exception.code, 0)  # ran to completion, no guard triggered


# --- doctor ---------------------------------------------------------------------

class DoctorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "data"
        self.data.mkdir()
        (self.data / "AGENTS.md").write_text("root\n", encoding="utf-8", newline="\n")
        init_repo(self.data)
        git(["config", "core.hooksPath", (graph.ENGINE / "tools" / "hooks").as_posix()], self.data)

    def _env(self):
        return {"VAULT_ENGINE": str(graph.ENGINE), "VAULT_DATA": str(self.data),
                "PATH": os.environ.get("PATH", "")}

    def _run_doctor(self):
        with mock.patch.dict(os.environ, self._env(), clear=True), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding.urllib.request.urlopen", side_effect=OSError("no ollama")), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit):
                onboarding.cmd_doctor(Namespace())
        return buf.getvalue()


class TestDoctorSchemaLine(DoctorCase):
    def test_ok_when_current(self):
        write_config(graph.Paths(graph.ENGINE, self.data), {"schema": schema.SCHEMA_VERSION})
        out = self._run_doctor()
        self.assertIn(f"OK   vault schema {schema.SCHEMA_VERSION}", out)

    def test_warn_when_older(self):
        with mock.patch.object(schema, "SCHEMA_VERSION", 5):
            write_config(graph.Paths(graph.ENGINE, self.data), {"schema": 1})
            out = self._run_doctor()
        self.assertIn("WARN vault schema 1 is older than this engine (5): run migrate", out)

    def test_fail_when_newer(self):
        write_config(graph.Paths(graph.ENGINE, self.data), {"schema": 99})
        out = self._run_doctor()
        self.assertIn("FAIL vault schema 99 is newer than this engine", out)
        self.assertIn("run update on this computer", out)

    def test_warn_when_schema_field_is_missing(self):
        write_config(graph.Paths(graph.ENGINE, self.data), {"feedback": {"level": "off"}})
        out = self._run_doctor()
        self.assertIn("WARN vault schema 1 (implicit, not recorded in vault.config.json): "
                      "run migrate", out)
        self.assertNotIn("OK   vault schema", out)


class TestDoctorUpgradeLeftovers(DoctorCase):
    """What a vault upgraded by hand from v0.2.0 still carries (task 0007)."""

    WORKFLOW = ("name: vault guard\non:\n  push:\n    branches: [main]\n  pull_request:\n\n"
                "jobs:\n  guard:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - uses: example/vault-engine@{ref}\n")

    def setUp(self):
        super().setUp()
        write_config(graph.Paths(graph.ENGINE, self.data), {"schema": schema.SCHEMA_VERSION})

    def _workflow(self, ref: str) -> None:
        path = self.data / ".github" / "workflows" / "vault.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.WORKFLOW.format(ref=ref), encoding="utf-8", newline="\n")

    def _on_branch(self, name: str) -> None:
        git(["symbolic-ref", "HEAD", f"refs/heads/{name}"], self.data)

    def _run_doctor_on(self, channel: tuple[str, str | None]) -> str:
        import update
        with mock.patch.object(update, "channel", return_value=channel), \
             mock.patch.object(update, "record_check", return_value=None), \
             mock.patch.dict(sys.modules, {"update": update}):
            return self._run_doctor()

    def test_shared_project_roots_present_here(self):
        root = self.tmp / "projects"
        root.mkdir()
        write_config(graph.Paths(graph.ENGINE, self.data),
                     {"schema": schema.SCHEMA_VERSION, "project_roots": [str(root)]})
        out = self._run_doctor()
        self.assertIn("WARN vault.config.json sets project_roots, a per-computer path, in the "
                      "shared config: run migrate", out)

    def test_shared_project_roots_of_another_computer(self):
        write_config(graph.Paths(graph.ENGINE, self.data),
                     {"schema": schema.SCHEMA_VERSION,
                      "project_roots": [str(self.tmp / "other-computer")]})
        out = self._run_doctor()
        self.assertIn("WARN vault.config.json sets project_roots that do not exist on this "
                      "computer", out)

    def test_ci_pinned_to_main_on_a_stable_install(self):
        self._workflow("main")
        self._on_branch("main")
        out = self._run_doctor_on(("stable", "v0.3.0"))
        self.assertIn("WARN vault CI runs the engine at @main, this engine is v0.3.0: run update", out)

    def test_ci_pin_matches_stable_install(self):
        self._workflow("v0.3.0")
        self._on_branch("main")
        out = self._run_doctor_on(("stable", "v0.3.0"))
        self.assertNotIn("vault CI", out)

    def test_ci_pin_on_dev_channel_is_not_flagged(self):
        self._workflow("main")
        self._on_branch("main")
        out = self._run_doctor_on(("dev", "main"))
        self.assertNotIn("vault CI", out)

    def test_master_branch_never_triggers_ci(self):
        self._workflow("main")
        self._on_branch("master")
        out = self._run_doctor_on(("dev", "main"))
        self.assertIn("WARN vault CI runs on pushes to main, but the data repo is on master, "
                      "so CI never runs: git branch -m master main && git push -u origin main", out)


if __name__ == "__main__":
    unittest.main()
