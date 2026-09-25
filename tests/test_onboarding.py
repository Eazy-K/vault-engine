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


class TestInit(unittest.TestCase):
    def setUp(self):
        # Resolved so short (8.3) Windows path forms never mismatch a later .resolve().
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

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
        self.assertEqual(config["project_roots"], [str(graph.ENGINE.parent)])
        self.assertEqual(config["feedback"], {"level": "off", "mode": "ask"})

        hooks_path = git(["config", "core.hooksPath"], target).stdout.strip()
        self.assertEqual(hooks_path, (graph.ENGINE / "tools" / "hooks").as_posix())

    def test_default_feedback_is_off(self):
        target = self.tmp / "example-data2"
        with mock.patch("sys.stdin.isatty", return_value=False), redirect_stdout(StringIO()):
            onboarding.cmd_init(self._args(target))
        config = json.loads((target / "vault.config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["feedback"]["level"], "off")

    def test_ci_workflow_names_the_engine_repo(self):
        import feedback
        target = self.tmp / "example-data6"
        workflow = target / ".github" / "workflows" / "vault.yml"
        with mock.patch.object(feedback, "_derive_repo", return_value="example/vault-engine"), \
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
        base = dict(data=str(self.data), user_level=False, no_env=True,
                    no_agents=True, no_routing=True, yes=True)
        base.update(kw)
        return Namespace(**base)

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
        lang_body = (self.data / "profile" / "language.md").read_text(encoding="utf-8")
        self.assertIn("Talk in Turkish.", lang_body)
        self.assertNotIn("Talk in German.", lang_body)

    def test_force_overwrites_already_filled_note(self):
        self._run({"chat_language": "Turkish"})
        out = self._run({"chat_language": "German"}, force=True)
        self.assertIn("written", out)
        lang_body = (self.data / "profile" / "language.md").read_text(encoding="utf-8")
        self.assertIn("Talk in German.", lang_body)


class TestOnboardNonInteractive(unittest.TestCase):
    def test_no_flags_non_tty_prints_hint(self):
        with mock.patch("sys.stdin.isatty", return_value=False), redirect_stdout(StringIO()) as buf, \
             mock.patch.dict(os.environ, {"VAULT_DATA": str(Path(tempfile.mkdtemp()))}, clear=False):
            onboarding.cmd_onboard(Namespace(questions=False, answers=None, data=None, force=False))
        self.assertIn("--questions", buf.getvalue())
        self.assertIn("--answers", buf.getvalue())


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
