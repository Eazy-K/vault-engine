"""Tests for the git-guard PreToolUse hooks (force push / --no-verify) in Claude and
Codex, plus the merge-permission checks in claude_hooks and codex_hooks."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

for _var in ("VAULT_DATA", "VAULT_HOME", "CLAUDE_CONFIG_DIR", "VAULT_GIT_GUARD"):
    os.environ.pop(_var, None)


def _no_registry(*_args):
    raise OSError("tests never read the real registry")


sys.modules["winreg"] = types.SimpleNamespace(HKEY_CURRENT_USER=None, OpenKey=_no_registry)
REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
CLAUDE_SCRIPT = TOOLS_DIR / "claude-hooks" / "git-guard.py"
CODEX_SCRIPT = TOOLS_DIR / "codex-hooks" / "git-guard.py"

_spec = importlib.util.spec_from_file_location("graph", TOOLS_DIR / "graph.py")
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)
sys.path.insert(0, str(TOOLS_DIR))
import claude_hooks  # noqa: E402
import codex_hooks  # noqa: E402

_gspec = importlib.util.spec_from_file_location("git_guard", CLAUDE_SCRIPT)
git_guard = importlib.util.module_from_spec(_gspec)
_gspec.loader.exec_module(git_guard)

DENIED = [
    "git push origin main --force",
    "git push -f",
    "git push origin HEAD --force-with-lease",
    "git push --force-with-lease=main origin main",
    "git push origin main --force-if-includes",
    "git push -uf origin main",
    "git push origin +main",
    "git push origin HEAD:+main",
    "git push origin main --no-verify",
    "git commit -m x --no-verify",
    "git commit --no-verify -m x",
    "git commit -n -m x",
    "git commit -an -m x",
    "git commit -m x -n",
    "git status && git push origin main -f",
    "git add . ; git commit -m x --no-verify",
    "echo hi | git push -f",
    "cd repo || git push --force",
    "git -C /tmp/r push --force",
    "GIT_TRACE=1 git push -f",
    "sudo git push -f",
    "/usr/bin/git push -f",
    'bash -c "git push --force origin main"',
    "git status\ngit push -f",
]
ALLOWED = [
    "git push origin main",
    "git push -u origin feature",
    "git push --dry-run -n",
    "git commit -m 'fix -n and --no-verify docs'",
    "git commit -m x",
    "git commit -mfoo",
    "git log --oneline -n 3",
    "git status && git diff -f",
    "gh pr create --title 'force push'",
    "echo git push -f",
    "git push origin feature:main",
    "git commit -m x -a",
    "ls",
]


class TestDecide(unittest.TestCase):
    def test_denied(self):
        for command in DENIED:
            with self.subTest(command=command):
                self.assertTrue(git_guard.decide(command))

    def test_allowed(self):
        for command in ALLOWED:
            with self.subTest(command=command):
                self.assertIsNone(git_guard.decide(command))

    def test_unbalanced_quote_does_not_crash(self):
        self.assertTrue(git_guard.decide("git push -f 'oops"))


def _run(script: Path, payload, extra_env=None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("VAULT_GIT_GUARD", None)
    env.update(extra_env or {})
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(script)], input=stdin,
                          capture_output=True, text=True, env=env)


class TestHookProcess(unittest.TestCase):
    def assert_denied(self, out):
        self.assertEqual(out.returncode, 0)
        decision = json.loads(out.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertEqual(decision["hookEventName"], "PreToolUse")

    def assert_silent(self, out):
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")

    def test_claude_denies_force_push_and_no_verify(self):
        for command in ("git push origin main --force", "git commit -m x --no-verify"):
            self.assert_denied(_run(CLAUDE_SCRIPT, {
                "tool_name": "Bash", "tool_input": {"command": command}}))

    def test_claude_allows_normal_and_other_tools(self):
        self.assert_silent(_run(CLAUDE_SCRIPT, {
            "tool_name": "Bash", "tool_input": {"command": "git push origin main"}}))
        self.assert_silent(_run(CLAUDE_SCRIPT, {
            "tool_name": "Write", "tool_input": {"command": "git push -f"}}))

    def test_claude_fails_open_and_off_switch(self):
        self.assert_silent(_run(CLAUDE_SCRIPT, "not json"))
        self.assert_silent(_run(CLAUDE_SCRIPT, ""))
        self.assert_silent(_run(CLAUDE_SCRIPT, {
            "tool_name": "Bash", "tool_input": {"command": "git push -f"}},
            {"VAULT_GIT_GUARD": "off"}))

    def test_codex_denies_shell_variants(self):
        for tool, key, value in (("Bash", "command", "git push origin main --force"),
                                 ("exec_command", "cmd", "git commit -m x --no-verify"),
                                 ("shell", "command", ["bash", "-lc", "git push -f"])):
            self.assert_denied(_run(CODEX_SCRIPT, {
                "tool_name": tool, "tool_input": {key: value}}))

    def test_codex_allows_normal_and_fails_open(self):
        self.assert_silent(_run(CODEX_SCRIPT, {
            "tool_name": "Bash", "tool_input": {"command": "git push origin main"}}))
        self.assert_silent(_run(CODEX_SCRIPT, {
            "tool_name": "apply_patch", "tool_input": {"command": "git push -f"}}))
        self.assert_silent(_run(CODEX_SCRIPT, "garbage"))


class TestInstallWiring(unittest.TestCase):
    def test_claude_merge_adds_bash_git_guard_idempotently(self):
        settings, changed = claude_hooks.merge({})
        self.assertTrue(changed)
        entries = [e for e in settings["hooks"]["PreToolUse"]
                   if "git-guard.py" in e["hooks"][0]["command"]]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["matcher"], "Bash")
        _, changed_again = claude_hooks.merge(settings)
        self.assertFalse(changed_again)

    def test_claude_git_guard_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertEqual(claude_hooks.git_guard_status(path)[0], "WARN")
            settings, _ = claude_hooks.merge({})
            path.write_text(json.dumps(settings), encoding="utf-8")
            self.assertEqual(claude_hooks.git_guard_status(path)[0], "OK")

    def test_codex_merge_registers_both_pretooluse_hooks(self):
        settings, _ = codex_hooks.merge({})
        commands = [(e.get("matcher"), e["hooks"][0]["command"])
                    for e in settings["hooks"]["PreToolUse"]]
        self.assertEqual(len(commands), 2)
        self.assertTrue(any("agent-guard.py" in c for _, c in commands))
        git = [m for m, c in commands if "git-guard.py" in c]
        self.assertEqual(git, [codex_hooks.GIT_MATCHER])
        _, changed = codex_hooks.merge(settings)
        self.assertFalse(changed)

    def test_codex_status_flags_missing_git_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.json"
            settings, _ = codex_hooks.merge({})
            settings["hooks"]["PreToolUse"] = [
                e for e in settings["hooks"]["PreToolUse"]
                if "git-guard.py" not in e["hooks"][0]["command"]]
            path.write_text(json.dumps(settings), encoding="utf-8")
            status, message = codex_hooks.hooks_status(path)
            self.assertEqual(status, "WARN")
            self.assertIn("git-guard.py", message)


class TestMergePermissions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_claude_warns_on_allowed_merge_and_fix_moves_rules(self):
        path = self.tmp / "settings.json"
        path.write_text(json.dumps({"permissions": {"allow": [
            "Bash(gh pr merge:*)", "Bash(git merge:*)", "Bash(git push:*)"]}}),
            encoding="utf-8")
        status, message = claude_hooks.merge_permission_status(path)
        self.assertEqual(status, "WARN")
        self.assertIn("--fix-permissions", message)
        settings = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(claude_hooks.fix_merge_permissions(settings))
        self.assertEqual(settings["permissions"]["allow"], ["Bash(git push:*)"])
        self.assertEqual(settings["permissions"]["deny"], ["Bash(gh pr merge:*)"])
        self.assertEqual(settings["permissions"]["ask"], ["Bash(git merge:*)"])
        self.assertFalse(claude_hooks.fix_merge_permissions(settings))
        path.write_text(json.dumps(settings), encoding="utf-8")
        self.assertEqual(claude_hooks.merge_permission_status(path)[0], "OK")

    def test_claude_missing_or_clean_settings_is_ok(self):
        self.assertEqual(claude_hooks.merge_permission_status(self.tmp / "none.json")[0], "OK")

    def test_claude_fix_permissions_flag_writes_settings(self):
        path = self.tmp / "settings.json"
        path.write_text(json.dumps({"permissions": {"allow": ["Bash(git merge:*)"]}}),
                        encoding="utf-8")
        out = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "graph.py"), "claude-hooks", "--install",
             "--fix-permissions", "--settings", str(path)],
            capture_output=True, text=True,
            env={**os.environ, "VAULT_DATA": "", "VAULT_HOME": ""})
        self.assertEqual(out.returncode, 0, out.stderr)
        perms = json.loads(path.read_text(encoding="utf-8"))["permissions"]
        self.assertEqual(perms["ask"], ["Bash(git merge:*)"])
        self.assertEqual(perms["allow"], [])

    def test_codex_rules_status(self):
        rules = self.tmp / "default.rules"
        self.assertEqual(codex_hooks.rules_status(rules)[0], "OK")  # no file
        rules.write_text(
            'prefix_rule(pattern=["gh", ["pr", "issue"]], decision="allow")\n'
            'prefix_rule(pattern=["git", ["merge", "rebase"]], decision="allow")\n',
            encoding="utf-8")
        status, message = codex_hooks.rules_status(rules)
        self.assertEqual(status, "WARN")
        self.assertIn("forbidden", message)
        rules.write_text(
            'prefix_rule(pattern=["gh", "pr", "merge"], decision="forbidden")\n'
            'prefix_rule(pattern=["git", "merge"], decision="prompt")\n', encoding="utf-8")
        self.assertEqual(codex_hooks.rules_status(rules)[0], "OK")


if __name__ == "__main__":
    unittest.main()
