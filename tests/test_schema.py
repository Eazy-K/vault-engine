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


class TestMigrate(SchemaTestCase):
    def test_no_op_when_already_current_and_field_explicit(self):
        write_config(self.paths, {"schema": 1})
        reached = schema.migrate(self.paths)
        self.assertEqual(reached, [])

    def test_writes_implicit_field_when_current_and_missing(self):
        write_config(self.paths, {"project_roots": ["/x"]})
        reached = schema.migrate(self.paths, commit=False)
        self.assertEqual(reached, [1])
        # Only recording the field is not a migration; the commit says so.
        init_repo(self.data)
        write_config(self.paths, {"project_roots": ["/x"]})
        schema.migrate(self.paths)
        self.assertIn("chore: record vault schema 1", git(["log", "--format=%s"], self.data).stdout)
        data = json.loads(self.paths.config_file.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["project_roots"], ["/x"])

    def test_refuses_newer_vault(self):
        write_config(self.paths, {"schema": 99})
        with self.assertRaises(SystemExit):
            schema.migrate(self.paths)

    def test_missing_step_raises(self):
        write_config(self.paths, {"project_roots": ["/x"]})  # schema 1
        with mock.patch.object(schema, "SCHEMA_VERSION", 3), \
             mock.patch.object(schema, "MIGRATIONS", {1: _step_one}):  # step 2->3 missing
            with self.assertRaises(SystemExit) as ctx:
                schema.migrate(self.paths, commit=False)
        self.assertIn("2 -> 3", str(ctx.exception))

    def test_two_step_migration_writes_schema_preserves_keys_and_commits_once(self):
        write_config(self.paths, {"project_roots": ["/x"], "feedback": {"level": "off"}})
        init_repo(self.data)
        git(["add", "vault.config.json"], self.data)
        git(["commit", "-q", "-m", "initial"], self.data)

        with mock.patch.object(schema, "SCHEMA_VERSION", 3), \
             mock.patch.object(schema, "MIGRATIONS", {1: _step_one, 2: _step_two}):
            reached = schema.migrate(self.paths)

        self.assertEqual(reached, [2, 3])
        data = json.loads(self.paths.config_file.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], 3)
        self.assertEqual(data["project_roots"], ["/x"])
        self.assertEqual(data["feedback"], {"level": "off"})
        self.assertTrue((self.data / "migrated-1.txt").exists())
        self.assertTrue((self.data / "migrated-2.txt").exists())

        log = git(["log", "--oneline"], self.data).stdout.strip().splitlines()
        self.assertEqual(len(log), 2)  # initial commit + exactly one migration commit
        self.assertIn("chore: migrate vault schema 1 -> 3", log[0])

        show = git(["show", "--stat", "--format=", "HEAD"], self.data).stdout
        self.assertIn("vault.config.json", show)
        self.assertIn("migrated-1.txt", show)
        self.assertIn("migrated-2.txt", show)

    def test_no_commit_flag_leaves_changes_uncommitted(self):
        write_config(self.paths, {"project_roots": ["/x"]})
        init_repo(self.data)
        git(["add", "vault.config.json"], self.data)
        git(["commit", "-q", "-m", "initial"], self.data)

        with mock.patch.object(schema, "SCHEMA_VERSION", 2), \
             mock.patch.object(schema, "MIGRATIONS", {1: _step_one}):
            schema.migrate(self.paths, commit=False)

        log = git(["log", "--oneline"], self.data).stdout.strip().splitlines()
        self.assertEqual(len(log), 1)  # only the initial commit
        status = git(["status", "--porcelain"], self.data).stdout
        self.assertIn("vault.config.json", status)

    def test_preexisting_staged_changes_block_the_commit(self):
        write_config(self.paths, {"project_roots": ["/x"]})
        init_repo(self.data)
        git(["add", "vault.config.json"], self.data)
        git(["commit", "-q", "-m", "initial"], self.data)
        (self.data / "unrelated.txt").write_text("pending\n", encoding="utf-8", newline="\n")
        git(["add", "unrelated.txt"], self.data)

        with mock.patch.object(schema, "SCHEMA_VERSION", 2), \
             mock.patch.object(schema, "MIGRATIONS", {1: _step_one}), \
             redirect_stdout(StringIO()) as buf:
            reached = schema.migrate(self.paths)

        self.assertEqual(reached, [2])  # the field/step still ran locally
        self.assertIn("commit them yourself", buf.getvalue())
        log = git(["log", "--oneline"], self.data).stdout.strip().splitlines()
        self.assertEqual(len(log), 1)  # migration was not committed


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
        self.assertEqual(data["project_roots"], ["/new/root"])
        self.assertEqual(data["feedback"], {"level": "reports", "mode": "auto"})


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

class TestDoctorSchemaLine(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
