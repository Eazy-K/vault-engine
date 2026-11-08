"""Tests for tools/claude-hooks/permission-request.py (PermissionRequest hook)."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _isolation  # noqa: E402,F401  (scrubs VAULT_DATA/VAULT_HOME)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "claude-hooks" / "permission-request.py"

_spec = importlib.util.spec_from_file_location("permission_request", SCRIPT)
perm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(perm)


class HookCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name).resolve()
        self.cfg = tmp / "cfg"
        self.cfg.mkdir()
        self.data = tmp / "data"
        (self.data / ".graph").mkdir(parents=True)
        self.main = tmp / "main"
        (self.main / ".git").mkdir(parents=True)
        self.wt = tmp / "wt"
        self.wt.mkdir()
        (self.wt / ".git").write_text(f"gitdir: {tmp}/main/.git/worktrees/wt\n", encoding="utf-8")
        self.set_allow(["Bash(git commit:*)", "Read"])

    def set_allow(self, rules):
        (self.cfg / "settings.json").write_text(
            json.dumps({"permissions": {"allow": rules}}), encoding="utf-8")

    def run_hook(self, payload, stdin=None, **env_extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("VAULT_DATA", "VAULT_HOME", "CLAUDE_CONFIG_DIR", "VAULT_PERMISSION_HOOK")}
        env["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        env["VAULT_DATA"] = str(self.data)
        env.update(env_extra)
        data = stdin if stdin is not None else json.dumps(payload).encode("utf-8")
        return subprocess.run([sys.executable, str(SCRIPT)], input=data, capture_output=True,
                              env=env, timeout=30)

    def decision(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        if not proc.stdout.strip():
            return None
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PermissionRequest")
        return out["decision"]["behavior"]

    def usage_events(self):
        path = self.data / ".graph" / "usage.log"
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]

    def bash(self, command, cwd=None, **extra):
        return {"hook_event_name": "PermissionRequest", "session_id": "s1", "tool_name": "Bash",
                "tool_input": {"command": command}, "cwd": str(cwd or self.wt), **extra}


class TestPolicy(HookCase):
    def test_worktree_matched_rule_allows_and_logs(self):
        proc = self.run_hook(self.bash("git commit -m x", agent_type="worker-medium"))
        self.assertEqual(self.decision(proc), "allow")
        (ev,) = self.usage_events()
        self.assertEqual((ev["event"], ev["tool"], ev["rule"], ev["worktree"], ev["agent_type"]),
                         ("permission_allow", "Bash", "Bash(git commit:*)", True, "worker-medium"))
        self.assertEqual((ev["agent"], ev["session"]), ("claude", "s1"))

    def test_worktree_unmatched_subagent_denied_with_message(self):
        proc = self.run_hook(self.bash("rm -rf build", agent_type="worker-medium"))
        self.assertEqual(self.decision(proc), "deny")
        msg = json.loads(proc.stdout)["hookSpecificOutput"]["decision"]["message"]
        self.assertIn("rm -rf build", msg)
        self.assertIn("worker-medium", msg)
        self.assertIn("report it as a blocker", msg)
        (ev,) = self.usage_events()
        self.assertEqual((ev["event"], ev["tool"], ev["summary"], ev["agent_type"], ev["worktree"]),
                         ("permission_deny", "Bash", "rm -rf build", "worker-medium", True))

    def test_main_checkout_matched_rule_subagent_denied(self):
        proc = self.run_hook(self.bash("git commit -m x", cwd=self.main, agent_type="worker-low"))
        self.assertEqual(self.decision(proc), "deny")
        self.assertIn("worktree=False", json.loads(proc.stdout)["hookSpecificOutput"]["decision"]["message"])
        (ev,) = self.usage_events()
        self.assertEqual((ev["event"], ev["worktree"]), ("permission_deny", False))

    def test_main_session_unmatched_prints_nothing_and_logs_prompt(self):
        proc = self.run_hook(self.bash("rm -rf build"))
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, b""))
        (ev,) = self.usage_events()
        self.assertEqual(ev["event"], "permission_prompt")
        self.assertNotIn("agent_type", ev)
        self.assertEqual(ev["summary"], "rm -rf build")

    def test_main_session_in_main_checkout_prints_nothing(self):
        proc = self.run_hook(self.bash("git commit -m x", cwd=self.main))
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, b""))
        self.assertEqual(self.usage_events()[0]["event"], "permission_prompt")

    def test_worktree_allow_from_vault_config(self):
        (self.data / "vault.config.json").write_text(
            json.dumps({"permissions": {"worktree_allow": ["Bash(make test:*)"]}}), encoding="utf-8")
        proc = self.run_hook(self.bash("make test -j2", agent_type="worker-low"))
        self.assertEqual(self.decision(proc), "allow")
        self.assertEqual(self.usage_events()[0]["rule"], "Bash(make test:*)")

    def test_project_settings_rules_are_read(self):
        (self.wt / ".claude").mkdir()
        (self.wt / ".claude" / "settings.local.json").write_text(
            json.dumps({"permissions": {"allow": ["Bash(make test)"]}}), encoding="utf-8")
        self.assertEqual(self.decision(self.run_hook(self.bash("make test"))), "allow")

    def test_env_off_gives_no_output_and_no_log(self):
        proc = self.run_hook(self.bash("rm x", agent_type="w"), VAULT_PERMISSION_HOOK="off")
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, b""))
        self.assertEqual(self.usage_events(), [])

    def test_malformed_stdin(self):
        for raw in (b"{not json", b"", b"[1,2]", b"\xff\xfe\x00garbage"):
            proc = self.run_hook(None, stdin=raw)
            self.assertEqual((proc.returncode, proc.stdout.strip()), (0, b""), raw)

    def test_missing_tool_name(self):
        proc = self.run_hook({"cwd": str(self.wt)})
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, b""))

    def test_no_log_without_data_dir(self):
        shutil.rmtree(self.data)
        proc = self.run_hook(self.bash("git commit -m x"))
        self.assertEqual(self.decision(proc), "allow")
        self.assertFalse(self.data.exists())

    def test_summary_truncated_and_masked_in_log(self):
        cmd = "curl -H 'Authorization: abc123secret' --data key=hunter2 " + "x" * 200
        self.run_hook(self.bash(cmd, agent_type="w"))
        summary = self.usage_events()[0]["summary"]
        self.assertLessEqual(len(summary), 120)
        self.assertNotIn("abc123secret", summary)
        self.assertNotIn("hunter2", summary)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class TestRealWorktree(HookCase):
    def test_real_git_worktree_detected(self):
        repo = Path(self._tmp.name).resolve() / "repo"
        repo.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]  # guard:ignore
        for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "i"],
                     ["worktree", "add", "-q", str(repo.parent / "linked"), "-b", "side"]):
            subprocess.run(git + args, cwd=repo, check=True, capture_output=True)
        self.assertEqual(self.decision(self.run_hook(
            self.bash("git commit -m x", cwd=repo.parent / "linked", agent_type="w"))), "allow")
        self.assertEqual(self.decision(self.run_hook(
            self.bash("git commit -m x", cwd=repo, agent_type="w"))), "deny")


class TestRuleMatching(unittest.TestCase):
    CWD = str(Path(tempfile.gettempdir()).resolve() / "wtx")

    def m(self, rule, tool, tool_input):
        return perm.rule_matches(rule, tool, tool_input, self.CWD)

    def test_bash_prefix_word_boundary(self):
        self.assertTrue(self.m("Bash(git commit:*)", "Bash", {"command": "git commit -m x"}))
        self.assertTrue(self.m("Bash(git commit:*)", "Bash", {"command": "git commit"}))
        self.assertFalse(self.m("Bash(git commit:*)", "Bash", {"command": "git commitx"}))

    def test_bash_exact_and_list_command(self):
        self.assertTrue(self.m("Bash(make test)", "Bash", {"command": "make test"}))
        self.assertFalse(self.m("Bash(make test)", "Bash", {"command": "make test all"}))
        self.assertTrue(self.m("Bash(git status:*)", "Bash", {"cmd": ["git", "status", "-s"]}))

    def test_chained_segments_each_need_a_match(self):
        rules = ["Bash(git add:*)", "Bash(git commit:*)"]
        ok = {"command": "git add . && git commit -m x"}
        bad = {"command": "git add . && rm -rf /"}
        self.assertEqual(perm.any_rule_matches(rules, "Bash", ok, self.CWD),
                         "Bash(git add:*), Bash(git commit:*)")
        self.assertIsNone(perm.any_rule_matches(rules, "Bash", bad, self.CWD))
        self.assertIsNone(perm.any_rule_matches(rules, "Bash", {"command": "git add $(rm x)"},
                                                self.CWD))

    def test_read_glob(self):
        self.assertTrue(self.m("Read(**/*.md)", "Read", {"file_path": self.CWD + "/docs/a.md"}))
        self.assertTrue(self.m("Read(**/*.md)", "Read", {"file_path": "a.md"}))
        self.assertFalse(self.m("Read(**/*.md)", "Read", {"file_path": "a.py"}))
        self.assertTrue(self.m("Edit(src/**)", "Write", {"file_path": "src/x/a.py"}))
        self.assertFalse(self.m("Edit(src/**)", "Write", {"file_path": "docs/a.md"}))

    def test_webfetch_domain(self):
        rule = "WebFetch(domain:example.com)"
        self.assertTrue(self.m(rule, "WebFetch", {"url": "https://example.com/x"}))
        self.assertTrue(self.m(rule, "WebFetch", {"url": "https://docs.example.com/x"}))
        self.assertFalse(self.m(rule, "WebFetch", {"url": "https://badexample.com/x"}))

    def test_bare_tool_and_mcp(self):
        self.assertTrue(self.m("Grep", "Grep", {"pattern": "x"}))
        self.assertFalse(self.m("Grep", "Bash", {"command": "ls"}))
        self.assertTrue(self.m("mcp__srv__tool", "mcp__srv__tool", {}))
        self.assertTrue(self.m("mcp__srv", "mcp__srv__other", {}))
        self.assertFalse(self.m("mcp__srv", "mcp__other__x", {}))

    def test_unknown_forms_never_match(self):
        self.assertFalse(self.m("Frobnicate(x)", "Bash", {"command": "x"}))
        self.assertFalse(self.m("", "Bash", {"command": "x"}))

    def test_mask_and_summarize(self):
        self.assertNotIn("hunter2", perm.mask("login password=hunter2"))  # guard:ignore
        self.assertNotIn("abc", perm.mask("curl token=abc"))
        self.assertNotIn("ghp_abcdefgh1234", perm.mask("x ghp_abcdefgh1234"))
        self.assertEqual(perm.summarize("Read", {"file_path": "/a/b.md"}), "/a/b.md")
        self.assertEqual(len(perm.summarize("Bash", {"command": "x" * 500})), 120)


if __name__ == "__main__":
    unittest.main()
