"""The instructions an agent reads on every task must point at real things.

templates/AGENTS.md (copied into every vault) and the default notes under
defaults/ name files and graph.py commands. This test initialises a fresh vault
with `init`, then checks that every file path they mention exists there (or in
the engine, for `$VAULT_ENGINE/...`) and runs every graph.py command they show,
from a folder outside the vault, the way an agent would. It also follows the
`reinforce` hint that `context` prints, literally.

Every command runs as a subprocess with VAULT_ENGINE/VAULT_DATA, HOME and the
git config pointed into a temp folder; nothing touches the real user's setup.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shlex
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
TEMPLATE = REPO_ROOT / "templates" / "AGENTS.md"
DEFAULTS = REPO_ROOT / "defaults"

_spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)

# update.py is not imported here: other test modules import it (and schema.py)
# themselves and patch the `graph` they are bound to. How update reads the line
# is tested in test_update (TestAgentsMdStatus) and test_schema (doctor).
REVISION_LINE = re.compile(r"<!-- vault-engine AGENTS\.md template: (\d+)\. ")

# The template's revision line and the text it stands for. When you change
# templates/AGENTS.md, raise N in its last line (`vault-engine AGENTS.md
# template: N`) so vaults with a translated or edited copy are told about it,
# then record the new revision and hash here.
TEMPLATE_REVISION = 3
TEMPLATE_SHA256 = "0a039809bcd9b3563e048f099b90d29ccc6208214c1dc453e3782e293e8f1fc2"

GRAPH_COMMAND = 'python "$VAULT_ENGINE/tools/graph.py"'
CODE_SPAN = re.compile(r"`([^`\n]+)`")
FENCED = re.compile(r"^```.*?^```", re.S | re.M)
PATH_LIKE = re.compile(r"(\$VAULT_ENGINE/)?[\w.-]+(/[\w.-]+)*/?")
# Placeholders an agent fills in, and the value this test uses for each.
PLACEHOLDERS = {"<task summary; keywords in the languages you use>": "release checklist"}
# Folders the user creates when they first need them (vault-notes, "Folders").
CREATED_ON_DEMAND = {"notes/", "standards/", "projects/"}


def _instruction_files() -> list[Path]:
    return [TEMPLATE] + sorted(DEFAULTS.rglob("*.md"))


def _spans(path: Path) -> list[str]:
    text = FENCED.sub("", path.read_text(encoding="utf-8"))
    return CODE_SPAN.findall(text)


def _is_path(span: str) -> bool:
    if any(c in span for c in "<>*~ ") or span.startswith((".git/", "-")):
        return False
    if not PATH_LIKE.fullmatch(span):
        return False
    return "/" in span or span.endswith((".md", ".json", ".yml"))


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


class TestTemplateRevision(unittest.TestCase):
    def test_template_has_a_revision_line(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        match = REVISION_LINE.match(text.rstrip("\n").splitlines()[-1])
        self.assertIsNotNone(match, "the revision line must stay the last line")
        self.assertEqual(int(match.group(1)), TEMPLATE_REVISION)

    def test_revision_raised_with_every_template_change(self):
        self.assertEqual(
            _file_sha(TEMPLATE), TEMPLATE_SHA256,
            "templates/AGENTS.md changed: raise N in its last line (`vault-engine AGENTS.md "
            "template: N`) so translated copies get a diff, then set TEMPLATE_REVISION and "
            "TEMPLATE_SHA256 in this test.")


class FreshVault(unittest.TestCase):
    """One `init`-ed vault for the whole class; each command runs in a subprocess."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp()).resolve()
        cls.data = cls.tmp / "vault"
        cls.elsewhere = cls.tmp / "some-project"
        cls.elsewhere.mkdir()
        home = cls.tmp / "home"
        home.mkdir()
        gitconfig = cls.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test Runner\n\temail = tester@example.com\n"
                             "[commit]\n\tgpgsign = false\n", encoding="utf-8", newline="\n")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("VAULT_", "GIT_"))}
        env.update({"VAULT_ENGINE": str(REPO_ROOT), "VAULT_DATA": str(cls.data),
                    "HOME": str(home), "USERPROFILE": str(home),
                    "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
                    # Nothing listens there: search falls back to keywords at once.
                    "VAULT_OLLAMA_URL": "http://127.0.0.1:9",
                    "PYTHONIOENCODING": "utf-8"})
        cls.env = env
        result = cls.graph(["init", str(cls.data), "--yes"])
        if result.returncode != 0:
            raise RuntimeError(f"init failed: {result.stdout}\n{result.stderr}")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def graph(cls, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(GRAPH_PATH), *args], cwd=cls.elsewhere,
                              env=cls.env, capture_output=True, text=True, encoding="utf-8",
                              timeout=120)

    def _resolve(self, span: str) -> Path:
        if span.startswith("$VAULT_ENGINE/"):
            return REPO_ROOT / span.removeprefix("$VAULT_ENGINE/")
        return self.data / span


class TestReferencedPaths(FreshVault):
    def test_every_path_exists_in_a_fresh_vault_or_the_engine(self):
        checked, missing = 0, []
        for source in _instruction_files():
            for span in _spans(source):
                if not _is_path(span) or span in CREATED_ON_DEMAND:
                    continue
                checked += 1
                if not self._resolve(span).exists():
                    missing.append(f"{source.relative_to(REPO_ROOT).as_posix()}: `{span}`")
        self.assertEqual(missing, [])
        self.assertGreater(checked, 5)  # the extraction itself still finds paths

    def test_no_command_assumes_the_vault_or_engine_as_working_folder(self):
        wrong = []
        for source in _instruction_files():
            for span in _spans(source):
                if "graph.py" in span and not span.startswith(GRAPH_COMMAND):
                    wrong.append(f"{source.relative_to(REPO_ROOT).as_posix()}: `{span}`")
        self.assertEqual(wrong, [])


class TestReferencedCommands(FreshVault):
    def _commands(self) -> list[tuple[str, list[str]]]:
        found = []
        for source in _instruction_files():
            for span in _spans(source):
                if not span.startswith(GRAPH_COMMAND):
                    continue
                rest = span[len(GRAPH_COMMAND):].strip()
                if rest == "<command>":  # the generic form in vault-notes
                    continue
                for placeholder, value in PLACEHOLDERS.items():
                    rest = rest.replace(placeholder, value)
                found.append((span, shlex.split(rest.replace("$VAULT_DATA", str(self.data)))))
        return found

    def test_every_command_runs_from_another_folder(self):
        answers = self.tmp / "answers.json"
        answers.write_text("{}", encoding="utf-8", newline="\n")
        commands = self._commands()
        self.assertGreater(len(commands), 4)
        failures = []
        # onboard --answers fills the profile; the other commands follow it.
        for span, args in sorted(commands, key=lambda c: "--answers" not in c[1]):
            args = [str(answers) if a == "<file>" else a for a in args]
            self.assertFalse([a for a in args if a.startswith("<")], f"unfilled placeholder in {span}")
            result = self.graph(args)
            if result.returncode != 0:
                failures.append(f"`{span}` -> {result.returncode}: {result.stderr.strip()[-300:]}")
        self.assertEqual(failures, [])

    def test_reinforce_hint_runs_as_printed(self):
        """An agent that copies the hint without adding notes, or adds the word
        `none`, closes the task without an error."""
        for extra in ([], ["none"], ["None"]):
            with self.subTest(extra=extra):
                out = self.graph(["context", "release checklist"])
                self.assertEqual(out.returncode, 0, out.stderr)
                hint = out.stdout.strip().splitlines()[-1]
                self.assertTrue(hint.startswith("<!-- when done") and hint.endswith("-->"), hint)
                command = hint.removesuffix("-->").split(": ", 1)[1].strip()
                args = shlex.split(command, posix=True)
                self.assertEqual(Path(args[1]), GRAPH_PATH)
                result = self.graph(args[2:] + extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("recorded 0 used note(s)", result.stdout)


class TestBlockMessages(unittest.TestCase):
    def test_data_policy_hint_names_an_existing_file(self):
        path = graph.data_policy_hint().split(", ", 1)[1]
        self.assertTrue(Path(path).is_file(), path)

    def test_hook_and_messages_no_longer_point_at_a_vault_standards_folder(self):
        for source in (TOOLS_DIR / "hooks" / "pre-commit", TOOLS_DIR / "graph.py",
                       TOOLS_DIR / "onboarding.py"):
            text = source.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?<![/\w])standards/data-policy\.md", source.name)


class TestReinforceNone(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.paths = graph.Paths(graph.ENGINE, self.tmp)

    def _reinforce(self, notes: list[str]) -> str:
        args = Namespace(notes=notes, task="abc123", rate=graph.LEARNING_RATE)
        with mock.patch.object(graph, "default_paths", return_value=self.paths), \
             mock.patch.dict(sys.modules, {"update": None, "feedback": None}), \
             redirect_stdout(StringIO()) as out:  # no update check, no feedback send
            graph.cmd_reinforce(args)
        return out.getvalue()

    def test_word_none_means_no_notes(self):
        self.assertIn("recorded 0 used note(s)", self._reinforce(["none"]))

    def test_a_note_named_none_still_counts(self):
        for name in ("none", "beta"):
            (self.tmp / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8", newline="\n")
        self.assertIn("none <-> beta", self._reinforce(["none", "beta"]))

    def test_other_missing_notes_still_fail(self):
        with self.assertRaises(SystemExit):
            self._reinforce(["no-such-note"])


if __name__ == "__main__":
    unittest.main()
