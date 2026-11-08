"""Tests for tools/codex_worker.py (`graph.py codex-worker`) with a fake codex executable."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_core import GRAPH_PATH, graph  # noqa: E402

sys.path.insert(0, str(GRAPH_PATH.parent))
import codex_worker  # noqa: E402

codex_worker.g = graph  # other test modules replace sys.modules["graph"]; bind to ours

FAKE_CODEX = r'''
import json, os, subprocess, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
mode = os.environ.get("FAKE_MODE", "ok")
wt = args[args.index("-C") + 1]
last = args[args.index("-o") + 1]
with open(os.path.join(os.path.dirname(last), "fake-args.json"), "w") as f:
    json.dump({"args": args, "prompt": prompt}, f)
if mode == "hang":
    time.sleep(60)
print(json.dumps({"type": "thread.started", "thread_id": "thread-123"}))
for n in (1, 2):
    print(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 100 * n, "cached_input_tokens": 10 * n, "cache_write_input_tokens": 0,
        "output_tokens": 5 * n, "reasoning_output_tokens": 2 * n}}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}))
if mode == "commit":
    with open(os.path.join(wt, "new.txt"), "w") as f:
        f.write("x\n")
    subprocess.run(["git", "-C", wt, "add", "new.txt"], check=True)
    subprocess.run(["git", "-C", wt, "commit", "-qm", "wip: new"], check=True)
if mode != "nostatus":
    with open(last, "w") as f:
        f.write("All done.\n\nSTATUS: SUCCESS\nChanged files:\n- new.txt\nDecisions:\n- kept it simple\n"
                "Evidence:\n- ran it\nBlockers:\n- none\n")
'''

TASK = """---
type: task
status: in-progress
---
Add a file.

## Acceptance
```
python -c "print('fine')"
```
"""

ENV_GIT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",  # guard:ignore
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}  # guard:ignore


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          env={**os.environ, **ENV_GIT}, check=True)


class CodexWorkerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.main = root / "main"
        self.main.mkdir()
        git(self.main, "init", "-q", "-b", "main")
        (self.main / "a.txt").write_text("a\n", encoding="utf-8")
        git(self.main, "add", "a.txt")
        git(self.main, "commit", "-qm", "init")
        self.wt = root / "wt"
        git(self.main, "worktree", "add", "-q", "-b", "feat/x", str(self.wt))
        self.fake = root / "fake_codex.py"
        self.fake.write_text(FAKE_CODEX, encoding="utf-8")
        self.task = root / "0099-demo.md"
        self.task.write_text(TASK, encoding="utf-8")
        self.out = root / "out"

    def run_cli(self, *extra, mode="ok", worktree=None, task=True):
        cmd = [sys.executable, str(GRAPH_PATH), "codex-worker",
               *(["--task", str(self.task)] if task else []),
               "--worktree", str(worktree or self.wt), "--out", str(self.out),
               "--base", "main", "--codex", str(self.fake), *extra]
        env = {**os.environ, **ENV_GIT, "FAKE_MODE": mode}
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)

    def test_prompt_has_rules_spec_and_instructions(self):
        prompt = codex_worker.build_prompt("/w", "feat/x", "Spec body.", "make test", "Be brief.")
        self.assertIn("Work only inside /w", prompt)
        self.assertIn("The branch is feat/x", prompt)
        self.assertIn("Never push, merge, rebase", prompt)
        self.assertIn("Acceptance command: make test", prompt)
        self.assertIn("STATUS: SUCCESS|PARTIAL|FAILED", prompt)
        self.assertIn("## Task\n\nSpec body.", prompt)
        self.assertTrue(prompt.rstrip().endswith("Be brief."))

    def test_refuses_main_checkout(self):
        r = self.run_cli(worktree=self.main)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("main checkout", r.stderr)

    def test_refuses_main_branch_worktree_without_flag(self):
        git(self.wt, "checkout", "-q", "-b", "main2")
        git(self.wt, "branch", "-m", "master")
        r = self.run_cli()
        self.assertEqual(r.returncode, 2)
        self.assertIn("--allow-main", r.stderr)
        self.assertEqual(self.run_cli("--allow-main").returncode, 0)

    def test_requires_task_or_prompt_file(self):
        r = self.run_cli(task=False)
        self.assertEqual(r.returncode, 2)
        self.assertIn("exactly one", r.stderr)

    def test_dry_run_prints_command_and_prompt_and_runs_nothing(self):
        r = self.run_cli("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("exec -C", r.stdout)
        self.assertIn("-s workspace-write", r.stdout)
        self.assertNotIn("danger-full-access", r.stdout)
        self.assertIn("## Task", r.stdout)
        self.assertIn("python -c", r.stdout)
        self.assertFalse(self.out.exists())

    def test_config_trim_flags(self):
        self.assertIn("--ignore-user-config", self.run_cli("--dry-run").stdout)
        blank = self.run_cli("--dry-run", "--config-trim", "blank-instructions").stdout
        self.assertIn('developer_instructions=""', blank)
        self.assertNotIn("--ignore-user-config", blank)
        none = self.run_cli("--dry-run", "--config-trim", "none").stdout
        self.assertNotIn("developer_instructions", none)
        self.assertNotIn("--ignore-user-config", none)

    def test_run_passes_flags_and_prompt_to_codex(self):
        r = self.run_cli("--model", "m-x", "--effort", "high")
        self.assertEqual(r.returncode, 0, r.stderr)
        seen = json.loads((self.out / "fake-args.json").read_text(encoding="utf-8"))
        args = seen["args"]
        self.assertEqual(args[0], "exec")
        self.assertIn("--ignore-user-config", args)
        self.assertEqual(args[args.index("-m") + 1], "m-x")
        self.assertIn('model_reasoning_effort="high"', args)
        self.assertEqual(args[-1], "-")
        self.assertIn("Add a file.", seen["prompt"])
        self.assertEqual((self.out / "prompt.md").read_text(encoding="utf-8"), seen["prompt"])

    def test_report_sums_usage_and_parses_status(self):
        r = self.run_cli("--json", mode="commit")
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = json.loads(r.stdout)
        self.assertEqual(rep["status"], "SUCCESS")
        self.assertEqual(rep["thread_id"], "thread-123")
        m = rep["metrics"]
        self.assertEqual((m["turns"], m["input"], m["cached"], m["output"], m["reasoning"]),
                         (2, 300, 30, 15, 6))
        self.assertEqual(rep["git"]["files"], ["new.txt"])
        self.assertEqual(len(rep["git"]["log"]), 1)
        self.assertEqual(rep["codex"]["Decisions"], ["kept it simple"])
        self.assertEqual(rep["blockers"], [])
        self.assertEqual(rep["acceptance"]["exit"], 0)
        self.assertEqual(rep["acceptance"]["summary"], "fine")
        self.assertTrue((self.out / "report.md").is_file())
        self.assertTrue((self.out / "events.jsonl").is_file())

    def test_text_report(self):
        r = self.run_cli(mode="commit")
        self.assertIn("Status: SUCCESS", r.stdout)
        self.assertIn("- new.txt", r.stdout)
        self.assertIn("tokens: in 300, cached 30, out 15, reasoning 6", r.stdout)
        self.assertIn("Codex thread id: thread-123", r.stdout)

    def test_missing_status_block_is_partial(self):
        rep = json.loads(self.run_cli("--json", mode="nostatus").stdout)
        self.assertEqual(rep["status"], "PARTIAL")
        self.assertIn("no STATUS block", rep["blockers"])

    def test_acceptance_failure_exit_code_reported(self):
        rep = json.loads(self.run_cli("--json", "--acceptance", 'python -c "import sys; sys.exit(3)"').stdout)
        self.assertEqual(rep["acceptance"]["exit"], 3)

    def test_prompt_file_has_no_acceptance(self):
        pf = Path(self._tmp.name) / "smoke.md"
        pf.write_text("Reply only.\n", encoding="utf-8")
        r = self.run_cli("--json", "--prompt-file", str(pf), task=False)
        rep = json.loads(r.stdout)
        self.assertFalse(rep["acceptance"]["ran"])

    def test_timeout_exits_3(self):
        r = self.run_cli("--timeout", "2", mode="hang")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("Status: FAILED", r.stdout)
        self.assertIn("timeout", r.stdout)

    def test_missing_codex_exits_3(self):
        cmd = [sys.executable, str(GRAPH_PATH), "codex-worker", "--task", str(self.task),
               "--worktree", str(self.wt), "--out", str(self.out), "--base", "main",
               "--codex", str(Path(self._tmp.name) / "nope-codex.exe")]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 3, r.stderr)

    def test_parse_status_sections_inline(self):
        status, sec = codex_worker.parse_status("x\nSTATUS: failed\nBlockers: broke\n")
        self.assertEqual(status, "FAILED")
        self.assertEqual(sec["Blockers"], ["broke"])


if __name__ == "__main__":
    unittest.main()
