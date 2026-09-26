"""Tests for the non-Windows shell-rc env wiring in tools/onboarding.py
(`setup --no-env` off on macOS/Linux, and the matching `doctor` warning).

Never touches the real home, $SHELL or environment: everything is patched to
tempfile-based paths and fixed values.
Run with: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import sys
import types
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAPH_PATH = REPO_ROOT / "tools" / "graph.py"
TOOLS_DIR = REPO_ROOT / "tools"

for _var in ("VAULT_DATA", "VAULT_HOME"):
    os.environ.pop(_var, None)


# Nor through the Windows registry, where graph finds the VAULT_DATA that setup
# saved for the user: every test sees an empty HKCU\Environment instead.
def _no_registry(*_args):
    raise OSError("tests never read the real registry")


sys.modules["winreg"] = types.SimpleNamespace(HKEY_CURRENT_USER=None, OpenKey=_no_registry)

_spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)

sys.path.insert(0, str(TOOLS_DIR))
import onboarding  # noqa: E402


class TestRcFileChoice(unittest.TestCase):
    def setUp(self):
        self.home = Path("/home/example")  # never touched: pure function

    def test_zsh(self):
        self.assertEqual(onboarding._rc_file_for_shell("zsh", "linux", self.home),
                          self.home / ".zshrc")

    def test_bash_linux(self):
        self.assertEqual(onboarding._rc_file_for_shell("bash", "linux", self.home),
                          self.home / ".bashrc")

    def test_bash_macos(self):
        self.assertEqual(onboarding._rc_file_for_shell("bash", "darwin", self.home),
                          self.home / ".bash_profile")

    def test_fish(self):
        self.assertEqual(onboarding._rc_file_for_shell("fish", "linux", self.home),
                          self.home / ".config" / "fish" / "config.fish")

    def test_unknown_shell_falls_back_to_profile(self):
        self.assertEqual(onboarding._rc_file_for_shell("dash", "linux", self.home),
                          self.home / ".profile")
        self.assertEqual(onboarding._rc_file_for_shell("", "linux", self.home),
                          self.home / ".profile")


class TestRenderBlock(unittest.TestCase):
    PAIRS = [("VAULT_ENGINE", "/opt/vault engine"), ("VAULT_DATA", "/data/it's mine")]

    def test_bash_export_lines_quoted(self):
        block = onboarding._render_rc_block(self.PAIRS, "bash")
        self.assertTrue(block.startswith(onboarding.RC_BLOCK_START + "\n"))
        self.assertTrue(block.endswith(onboarding.RC_BLOCK_END + "\n"))
        self.assertIn("export VAULT_ENGINE='/opt/vault engine'", block)
        self.assertIn("export VAULT_DATA=", block)
        # round-trips through shlex so a shell would see the literal value back
        import shlex
        line = [l for l in block.splitlines() if l.startswith("export VAULT_DATA=")][0]
        value = line.split("=", 1)[1]
        self.assertEqual(shlex.split(value)[0], "/data/it's mine")

    def test_fish_uses_set_gx(self):
        block = onboarding._render_rc_block(self.PAIRS, "fish")
        self.assertIn("set -gx VAULT_ENGINE '/opt/vault engine'", block)
        # embedded single quote is escaped fish-style, not POSIX-style
        self.assertIn("\\'", block)
        self.assertNotIn('"\'"', block)


class TestUpsertBlock(unittest.TestCase):
    def test_append_to_empty(self):
        block = "# >>> vault-engine >>>\nexport X=1\n# <<< vault-engine <<<\n"
        self.assertEqual(onboarding._upsert_block("", block), block)

    def test_append_keeps_prior_content(self):
        prior = "export PATH=$PATH:/usr/local/bin\n"
        block = "# >>> vault-engine >>>\nexport X=1\n# <<< vault-engine <<<\n"
        result = onboarding._upsert_block(prior, block)
        self.assertTrue(result.startswith(prior))
        self.assertTrue(result.endswith(block))

    def test_append_adds_blank_line_separator(self):
        prior = "export PATH=$PATH\n"
        block = "# >>> vault-engine >>>\nexport X=1\n# <<< vault-engine <<<\n"
        result = onboarding._upsert_block(prior, block)
        self.assertEqual(result, prior + "\n" + block)

    def test_rerun_replaces_block_in_place(self):
        prior = ("before\n"
                  "# >>> vault-engine >>>\n"
                  "export X=1\n"
                  "# <<< vault-engine <<<\n"
                  "after\n")
        new_block = "# >>> vault-engine >>>\nexport X=2\n# <<< vault-engine <<<\n"
        result = onboarding._upsert_block(prior, new_block)
        self.assertEqual(result, "before\n" + new_block + "after\n")
        self.assertEqual(result.count(onboarding.RC_BLOCK_START), 1)


class TestWriteRcBlock(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_creates_file_and_reports_changed(self):
        path = self.tmp / "rc"
        changed = onboarding._write_rc_block(path, [("X", "1")], "bash")
        self.assertTrue(changed)
        self.assertIn("export X=1", path.read_text(encoding="utf-8"))

    def test_creates_parent_dir_for_fish(self):
        path = self.tmp / ".config" / "fish" / "config.fish"
        changed = onboarding._write_rc_block(path, [("X", "1")], "fish")
        self.assertTrue(changed)
        self.assertIn("set -gx X '1'", path.read_text(encoding="utf-8"))

    def test_rerun_same_values_reports_unchanged(self):
        path = self.tmp / "rc"
        onboarding._write_rc_block(path, [("X", "1")], "bash")
        text_after_first = path.read_text(encoding="utf-8")
        changed = onboarding._write_rc_block(path, [("X", "1")], "bash")
        self.assertFalse(changed)
        self.assertEqual(path.read_text(encoding="utf-8"), text_after_first)

    def test_rerun_new_values_updates_block(self):
        path = self.tmp / "rc"
        onboarding._write_rc_block(path, [("X", "1")], "bash")
        changed = onboarding._write_rc_block(path, [("X", "2")], "bash")
        self.assertTrue(changed)
        text = path.read_text(encoding="utf-8")
        self.assertIn("export X=2", text)
        self.assertNotIn("export X=1", text)
        self.assertEqual(text.count(onboarding.RC_BLOCK_START), 1)

    def test_uses_lf_newlines(self):
        path = self.tmp / "rc"
        onboarding._write_rc_block(path, [("X", "1")], "bash")
        raw = path.read_bytes()
        self.assertNotIn(b"\r\n", raw)


class TestRcBlockPresent(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_missing_file(self):
        self.assertFalse(onboarding._rc_block_present(self.tmp / "nope", "VAULT_ENGINE"))

    def test_no_block(self):
        path = self.tmp / "rc"
        path.write_text("export PATH=x\n", encoding="utf-8")
        self.assertFalse(onboarding._rc_block_present(path, "VAULT_ENGINE"))

    def test_block_with_var(self):
        path = self.tmp / "rc"
        onboarding._write_rc_block(path, [("VAULT_ENGINE", "/e"), ("VAULT_DATA", "/d")], "bash")
        self.assertTrue(onboarding._rc_block_present(path, "VAULT_ENGINE"))
        self.assertTrue(onboarding._rc_block_present(path, "VAULT_DATA"))
        self.assertFalse(onboarding._rc_block_present(path, "OTHER_VAR"))


class TestSetupEnvNonWindows(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "example-data"
        self.data.mkdir()

    def _args(self, **kw):
        base = dict(data=str(self.data), user_level=False, no_env=False,
                    no_agents=True, no_routing=True, yes=True)
        base.update(kw)
        return Namespace(**base)

    def test_yes_flag_writes_rc_file_non_interactively(self):
        with mock.patch("onboarding._is_windows", return_value=False), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}, clear=True), \
             mock.patch("sys.stdin.isatty", return_value=False), \
             redirect_stdout(StringIO()) as buf:
            onboarding.cmd_setup(self._args(yes=True))
        rc = self.home / ".bashrc"
        self.assertTrue(rc.exists())
        # shlex only quotes when needed: Windows temp paths get quotes, /tmp/... doesn't.
        self.assertIn(f"export VAULT_DATA={shlex.quote(str(self.data))}", rc.read_text(encoding="utf-8"))
        self.assertIn("written:", buf.getvalue())

    def test_no_tty_no_yes_only_prints(self):
        with mock.patch("onboarding._is_windows", return_value=False), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}, clear=True), \
             mock.patch("sys.stdin.isatty", return_value=False), \
             redirect_stdout(StringIO()) as buf:
            onboarding.cmd_setup(self._args(yes=False))
        self.assertFalse((self.home / ".bashrc").exists())
        self.assertIn("Add the following lines", buf.getvalue())

    def test_interactive_prompt_yes_writes(self):
        with mock.patch("onboarding._is_windows", return_value=False), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch.dict(os.environ, {"SHELL": "/bin/zsh"}, clear=True), \
             mock.patch.object(onboarding.g, "stdin_is_interactive", return_value=True), \
             mock.patch("onboarding._ask_yn", return_value=True), \
             redirect_stdout(StringIO()):
            onboarding.cmd_setup(self._args(yes=False))
        self.assertTrue((self.home / ".zshrc").exists())

    def test_interactive_prompt_no_only_prints(self):
        with mock.patch("onboarding._is_windows", return_value=False), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch.dict(os.environ, {"SHELL": "/bin/zsh"}, clear=True), \
             mock.patch.object(onboarding.g, "stdin_is_interactive", return_value=True), \
             mock.patch("onboarding._ask_yn", return_value=False), \
             redirect_stdout(StringIO()) as buf:
            onboarding.cmd_setup(self._args(yes=False))
        self.assertFalse((self.home / ".zshrc").exists())
        self.assertIn("Add the following lines", buf.getvalue())

    def test_rerun_reports_already_set(self):
        with mock.patch("onboarding._is_windows", return_value=False), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}, clear=True), \
             mock.patch("sys.stdin.isatty", return_value=False):
            with redirect_stdout(StringIO()):
                onboarding.cmd_setup(self._args(yes=True))
            with redirect_stdout(StringIO()) as buf:
                onboarding.cmd_setup(self._args(yes=True))
        self.assertIn("already set", buf.getvalue())

    def test_no_env_flag_writes_nothing(self):
        with mock.patch("onboarding._is_windows", return_value=False), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}, clear=True), \
             mock.patch("sys.stdin.isatty", return_value=False), \
             redirect_stdout(StringIO()):
            onboarding.cmd_setup(self._args(no_env=True, yes=True))
        self.assertFalse((self.home / ".bashrc").exists())

    def test_fish_shell_writes_fish_syntax(self):
        with mock.patch("onboarding._is_windows", return_value=False), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch.dict(os.environ, {"SHELL": "/usr/bin/fish"}, clear=True), \
             mock.patch("sys.stdin.isatty", return_value=False), \
             redirect_stdout(StringIO()):
            onboarding.cmd_setup(self._args(yes=True))
        config = self.home / ".config" / "fish" / "config.fish"
        self.assertTrue(config.exists())
        self.assertIn("set -gx VAULT_ENGINE", config.read_text(encoding="utf-8"))


class TestDoctorRcWarning(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "example-data"
        self.data.mkdir()
        (self.data / "AGENTS.md").write_text("root\n", encoding="utf-8")
        import subprocess
        subprocess.run(["git", "init"], cwd=self.data, capture_output=True)
        subprocess.run(["git", "config", "core.hooksPath",
                         (graph.ENGINE / "tools" / "hooks").as_posix()],
                        cwd=self.data, capture_output=True)

    def _env(self):
        return {"VAULT_DATA": str(self.data), "SHELL": "/bin/bash",
                "PATH": os.environ.get("PATH", "")}

    def test_warn_rc_block_present_but_not_in_process(self):
        onboarding._write_rc_block(self.home / ".bashrc",
                                    [("VAULT_ENGINE", str(graph.ENGINE)), ("VAULT_DATA", str(self.data))],
                                    "bash")
        with mock.patch.dict(os.environ, self._env(), clear=True), \
             mock.patch("onboarding._is_windows", return_value=False), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding.urllib.request.urlopen", side_effect=OSError("no ollama")), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit):
                onboarding.cmd_doctor(Namespace())
        self.assertIn("set in", buf.getvalue())
        self.assertIn("restart the terminal and the agent", buf.getvalue())

    def test_warn_plain_not_set_when_no_rc_block(self):
        with mock.patch.dict(os.environ, self._env(), clear=True), \
             mock.patch("onboarding._is_windows", return_value=False), \
             mock.patch("pathlib.Path.home", return_value=self.home), \
             mock.patch("onboarding.urllib.request.urlopen", side_effect=OSError("no ollama")), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit):
                onboarding.cmd_doctor(Namespace())
        self.assertIn("VAULT_ENGINE not set", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
