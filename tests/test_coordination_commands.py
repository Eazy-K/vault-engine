"""Coordination shell accounting and fail-open deny-event telemetry."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
spec = importlib.util.spec_from_file_location("coordination_shell_classify", TOOLS / "shell_classify.py")
classifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(classifier)

COORDINATION = (
    "python tools/graph.py codex-worker run 62",
    'python "$VAULT_ENGINE/tools/graph.py" codex-worker status',
    '"C:/Program Files/Python/python.exe" "C:/engine/tools/graph.py" codex-worker run 62',
    '/usr/bin/python3 /opt/engine/tools/graph.py codex-worker status',
    '/usr/bin/python3.12 -I /opt/engine/tools/graph.py codex-worker status',
    r'C:\Python\python.exe C:\engine\tools\graph.py codex-worker status',
    "git worktree add ../worker feat/worker",
    "git worktree list",
    "git fetch origin",
    "gh pr create --title 'task; details | checks'",
    "gh pr create --title 'A & B'",
    "gh pr view 62",
    "gh pr checks 62",
    "gh pr list",
    "gh pr status",
    'cd "some directory"',
    'cd "some directory" && git worktree list || echo unavailable; git fetch | echo done',
    "echo 'a && b; c | d'",
)


class TestCoordinationPredicate(unittest.TestCase):
    def test_allowlist_and_quoted_segments(self):
        for command in COORDINATION:
            with self.subTest(command=command):
                self.assertTrue(classifier.is_coordination_command(command))

    def test_every_segment_must_be_coordination(self):
        for command in (
            "git worktree add x && rm -rf y",
            "git fetch || cat secret.txt",
            "gh pr view 62 | head -5",
            "echo done; python build.py",
            "python tools/graph.py context",
            "python tools/graph.py codex-worker-other",
            "python other.py codex-worker",
            "python -c 'print(1)' tools/graph.py codex-worker",
            "gh pr merge 62",
            "gh pr diff 62",
            "git status",
            "git push origin branch",
            "echo hi > output.txt",
            "echo $(python build.py)",
            "echo `whoami`",
            "git fetch & python build.py",
            'git fetch && echo "unclosed',
            "", None, 42,
        ):
            with self.subTest(command=command):
                self.assertFalse(classifier.is_coordination_command(command))


class TestCoordinationHookProcesses(unittest.TestCase):
    def setUp(self):
        # Inherit the workspace ACL rather than tempfile's owner-only Windows ACL,
        # which is inaccessible to restricted test processes on newer Python.
        self.tmp = ROOT / ".cache" / ("coordination-test-" + uuid.uuid4().hex)
        self.tmp.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.env = dict(os.environ)
        for key in ("VAULT_READ_GUARD", "VAULT_READ_BUDGET", "VAULT_GIT_GUARD",
                    "VAULT_DELEGATION_AB", "VAULT_HOME"):
            self.env.pop(key, None)
        self.env.update(VAULT_DATA=str(self.data), TMPDIR=str(self.tmp),
                        TEMP=str(self.tmp), TMP=str(self.tmp),
                        CODEX_DELEGATION_WARN_STATE_DIR=str(self.tmp / "codex-state"))

    def run_hook(self, adapter, hook, tool="Bash", command=None, event=None):
        payload = {"session_id": "coordination-test", "prompt_id": "p1", "turn_id": "t1",
                   "tool_name": tool, "tool_input": {"command": command} if command else {}}
        if event:
            payload["hook_event_name"] = event
        result = subprocess.run(
            [sys.executable, str(TOOLS / (adapter + "-hooks") / (hook + ".py"))],
            input=json.dumps(payload), capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result

    def events(self):
        path = self.data / ".graph" / "usage.log"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []

    def assert_denied(self, result):
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def assert_event_base(self, event, agent, kind):
        self.assertEqual(event["event"], kind)
        self.assertEqual(event["agent"], agent)
        self.assertEqual(event["session"], "coordination-test")
        self.assertTrue(event["ts"])
        self.assertTrue(event["machine"])

    def test_coordination_preserves_read_budget(self):
        for command in COORDINATION:
            self.assertEqual(self.run_hook("claude", "read-guard", command=command).stdout, "")
        for _ in range(3):
            self.assertEqual(self.run_hook("claude", "read-guard", tool="Read").stdout, "")
        self.assertEqual(self.events(), [])
        self.assert_denied(self.run_hook("claude", "read-guard", tool="Read"))

    def test_coordination_tallied_separately_in_both_adapters(self):
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                for command in COORDINATION:
                    self.assertEqual(self.run_hook(adapter, "delegation-warn", command=command).stdout, "")
                self.run_hook(adapter, "delegation-warn", tool="Edit")
                self.run_hook(adapter, "delegation-warn", tool="Stop", event="Stop")
                event = self.events()[-1]
                self.assertEqual(event["agent"], adapter)
                self.assertEqual(event["coordination_calls"], len(COORDINATION))
                self.assertEqual(event["inline_calls"], 1)
                self.assertEqual(event["read_calls"], 0)
                self.assertEqual(event["spawned"], 0)

    def test_mixed_unsafe_command_counts_inline_in_both_adapters(self):
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                self.run_hook(adapter, "delegation-warn", command="git worktree add x && rm -rf y")
                self.run_hook(adapter, "delegation-warn", tool="Stop", event="Stop")
                event = self.events()[-1]
                self.assertEqual((event["inline_calls"], event["coordination_calls"]), (1, 0))

    def test_read_block_event_only_on_deny(self):
        for _ in range(3):
            self.run_hook("claude", "read-guard", tool="Read")
        self.assertEqual(self.events(), [])
        for count in (4, 5):
            self.assert_denied(self.run_hook("claude", "read-guard", tool="Read"))
            event = self.events()[-1]
            self.assert_event_base(event, "claude", "read_block")
            self.assertEqual((event["limit"], event["count"]), (3, count))
        self.assertEqual(len(self.events()), 2)

    def test_git_block_events_only_on_deny_in_both_adapters(self):
        for adapter in ("claude", "codex"):
            self.assertEqual(self.run_hook(adapter, "git-guard", command="git status").stdout, "")
        self.assertEqual(self.events(), [])
        for adapter in ("claude", "codex"):
            result = self.run_hook(adapter, "git-guard", command="git push --force")
            self.assert_denied(result)
            event = self.events()[-1]
            self.assert_event_base(event, adapter, "git_block")
            reason = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertEqual(event["reason"], reason[:200])
        self.assertEqual(len(self.events()), 2)

    def test_unwritable_usage_log_does_not_suppress_denials(self):
        (self.data / ".graph" / "usage.log").mkdir(parents=True)
        for adapter in ("claude", "codex"):
            self.assert_denied(self.run_hook(adapter, "git-guard", command="git push --force"))
        for _ in range(3):
            self.run_hook("claude", "read-guard", tool="Read")
        self.assert_denied(self.run_hook("claude", "read-guard", tool="Read"))


if __name__ == "__main__":
    unittest.main()
