"""Tests for tools/codex_worker.py (`graph.py codex-worker`) with a fake codex executable."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None
import unittest
from pathlib import Path
from unittest import mock

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
if mode == "write":
    with open(os.path.join(wt, "new.txt"), "w") as f:
        f.write("x\n")
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
        self.data = root / "vault-data"
        (self.data / ".graph").mkdir(parents=True)

    def run_cli(self, *extra, mode="ok", worktree=None, task=True, data="default"):
        cmd = [sys.executable, str(GRAPH_PATH), "codex-worker",
               *(["--task", str(self.task)] if task else []),
               "--worktree", str(worktree or self.wt), "--out", str(self.out),
               "--base", "main", "--codex", str(self.fake), *extra]
        env = {**os.environ, **ENV_GIT, "FAKE_MODE": mode}
        env.pop("VAULT_HOME", None)
        env["VAULT_DATA"] = str(self.data if data == "default" else data)  # never the real vault
        for k in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
            env.pop(k, None)
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)

    def test_logs_codex_exec_event(self):
        r = self.run_cli("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = (self.data / ".graph" / "usage.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        e = json.loads(lines[0])
        self.assertEqual((e["event"], e["agent"], e["agent_type"]), ("codex_exec", "claude", "worker-codex"))
        self.assertEqual((e["input_tokens"], e["cached_input_tokens"], e["output_tokens"],
                          e["reasoning_output_tokens"], e["turns"], e["exit_code"]),
                         (300, 30, 15, 6, 2, 0))
        self.assertEqual(e["cwd"], str(self.wt.resolve()))
        self.assertIn("ts", e)
        self.assertNotIn("session", e)

    def test_logging_fails_open_when_log_unwritable(self):
        (self.data / ".graph" / "usage.log").mkdir()  # a directory: opening it for append fails
        r = self.run_cli("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = json.loads(r.stdout)
        self.assertEqual(rep["status"], "SUCCESS")
        self.assertEqual(rep["metrics"]["input"], 300)
        self.assertTrue((self.out / "report.md").is_file())

    def test_no_vault_data_writes_nothing(self):
        missing = Path(self._tmp.name) / "no-such-data"
        r = self.run_cli("--json", data=missing)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(missing.exists())
        # unknown vault path (SystemExit from the lookup) is swallowed too
        orig = codex_worker.g.resolve_data_dir
        codex_worker.g.resolve_data_dir = lambda: sys.exit("no data")
        try:
            codex_worker.log_codex_exec(self.wt, {"input": 1, "cached": 0, "output": 1,
                                                  "reasoning": 0, "turns": 1}, 0)
        finally:
            codex_worker.g.resolve_data_dir = orig
        self.assertFalse((self.data / ".graph" / "usage.log").exists())

    def test_prompt_has_rules_spec_and_instructions(self):
        prompt = codex_worker.build_prompt("/w", "feat/x", "Spec body.", "make test", "Be brief.")
        self.assertIn("Work only inside /w", prompt)
        self.assertIn("The branch is feat/x", prompt)
        self.assertIn("Never push, merge, rebase", prompt)
        self.assertIn("Acceptance command: make test", prompt)
        self.assertIn("STATUS: SUCCESS|PARTIAL|FAILED", prompt)
        self.assertIn("## Task\n\nSpec body.", prompt)
        self.assertTrue(prompt.rstrip().endswith("Be brief."))

    def test_prompt_files_block_only_when_section_exists(self):
        body = "## Scope\nx\n## Files\n- a.py: f, 1-5\n## Goal\ny\n"
        prompt = codex_worker.build_prompt("/w", "b", body, None)
        self.assertIn("## Files to work on\n\n- a.py: f, 1-5\n\n## Task\n\n" + body.strip(), prompt)
        self.assertLess(prompt.index("STATUS: SUCCESS|PARTIAL|FAILED"),
                        prompt.index("## Files to work on"))
        plain = codex_worker.build_prompt("/w", "b", "Spec.", None)
        self.assertNotIn("Files to work on", plain)
        self.assertTrue(plain.endswith("## Task\n\nSpec.\n"))

    def test_prompt_rules_say_do_not_commit(self):
        prompt = codex_worker.build_prompt("/w", "feat/x", "Spec.", None)
        self.assertIn("Do not commit", prompt)

    def test_commit_flag_commits_codex_changes(self):
        r = self.run_cli("--json", "--commit", mode="write")
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = json.loads(r.stdout)
        h = rep["wip_commit"]
        self.assertRegex(h, r"^[0-9a-f]{7,}$")
        self.assertIn(h, git(self.wt, "log", "--oneline").stdout)
        self.assertIn("wip(codex): 0099-demo", git(self.wt, "log", "-1", "--format=%s").stdout)
        self.assertEqual(git(self.wt, "status", "--porcelain").stdout.strip(), "")
        self.assertEqual(rep["git"]["files"], ["new.txt"])

    def test_commit_flag_custom_message_and_text_report(self):
        r = self.run_cli("--commit", "--commit-message", "wip: custom", mode="write")
        self.assertRegex(r.stdout, r"WIP commit: [0-9a-f]{7,}")
        self.assertEqual(git(self.wt, "log", "-1", "--format=%s").stdout.strip(), "wip: custom")

    def test_commit_flag_on_clean_tree(self):
        r = self.run_cli("--json", "--commit")
        rep = json.loads(r.stdout)
        self.assertEqual(rep["wip_commit"], "none (clean)")
        self.assertEqual(len(git(self.wt, "log", "--oneline").stdout.splitlines()), 1)

    def test_no_commit_flag_leaves_file_untracked(self):
        rep = json.loads(self.run_cli("--json", mode="write").stdout)
        self.assertIsNone(rep["wip_commit"])
        self.assertEqual(git(self.wt, "status", "--porcelain").stdout.strip(), "?? new.txt")
        self.assertEqual(len(git(self.wt, "log", "--oneline").stdout.splitlines()), 1)

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

    def _di(self, argv_text):
        """The developer_instructions -c value from a fake-args.json argv, parsed back as TOML."""
        val = next(a for a in argv_text if a.startswith("developer_instructions="))
        if tomllib is None:  # Python 3.10: a JSON string is a valid TOML basic string
            return json.loads(val.split("=", 1)[1])
        return tomllib.loads(val)["developer_instructions"]

    def test_config_trim_flags(self):
        default = self.run_cli("--dry-run").stdout  # default: blank-instructions
        self.assertNotIn('developer_instructions=""', default)
        self.assertIn("developer_instructions=", default)
        self.assertNotIn("--ignore-user-config", default)
        ignore = self.run_cli("--dry-run", "--config-trim", "ignore-user-config").stdout
        self.assertIn("--ignore-user-config", ignore)
        self.assertIn("developer_instructions=", ignore)
        none = self.run_cli("--dry-run", "--config-trim", "none").stdout
        self.assertIn("developer_instructions=", none)
        self.assertNotIn("--ignore-user-config", none)

    def test_load_profile_both_files(self):
        for name, effort in (("worker-low", "low"), ("worker-medium", "medium")):
            p = codex_worker.load_profile(name)
            self.assertEqual((p["name"], p["effort"]), (name, effort))
            self.assertTrue(p["model"])
            self.assertIn("You are a worker subagent", p["instructions"])
            data = codex_worker._parse_profile((codex_worker.PROFILE_DIR / f"{name}.toml").read_text(encoding="utf-8"))
            self.assertEqual(p["instructions"], data["developer_instructions"])
            self.assertEqual(p["model"], data["model"])

    @unittest.skipIf(tomllib is None, "needs tomllib (Python 3.11+)")
    def test_fallback_parser_matches_tomllib(self):
        for name in codex_worker.PROFILES:
            text = (codex_worker.PROFILE_DIR / f"{name}.toml").read_text(encoding="utf-8")
            real = tomllib.loads(text)
            with mock.patch.dict(sys.modules, {"tomllib": None}):
                fb = codex_worker._parse_profile(text)
            for k in ("model", "model_reasoning_effort", "developer_instructions"):
                self.assertEqual(fb[k], real[k], k)

    def test_default_profile_is_medium(self):
        out = self.run_cli("--dry-run").stdout
        self.assertIn("Profile: worker-medium", out)
        self.assertIn("Effort: medium", out)

    def test_low_profile_dry_run_shows_model_effort_rules(self):
        low = codex_worker.load_profile("worker-low")
        out = self.run_cli("--dry-run", "--profile", "worker-low").stdout
        self.assertIn("Profile: worker-low", out)
        self.assertIn(f"Model: {low['model']}", out)
        self.assertIn("Effort: low", out)
        self.assertIn("Developer instructions:", out)
        self.assertIn("Never invent facts.", out)

    def test_flags_override_profile(self):
        out = self.run_cli("--dry-run", "--model", "m-x", "--effort", "high").stdout
        self.assertIn("Model: m-x", out)
        self.assertIn("Effort: high", out)

    def test_missing_profile_exits_2(self):
        with mock.patch.object(codex_worker, "PROFILE_DIR", Path(self._tmp.name) / "nope"):
            with self.assertRaises(codex_worker.WorkerError):
                codex_worker.load_profile("worker-low")
        r = self.run_cli("--profile", "worker-high")  # not a known profile: argparse error
        self.assertEqual(r.returncode, 2)

    def test_argv_carries_profile_instructions(self):
        for trim in codex_worker.CONFIG_TRIMS:
            r = self.run_cli("--config-trim", trim, "--profile", "worker-low")
            self.assertEqual(r.returncode, 0, r.stderr)
            args = json.loads((self.out / "fake-args.json").read_text(encoding="utf-8"))["args"]
            self.assertTrue(all("\n" not in a for a in args))
            self.assertEqual(self._di(args), codex_worker.load_profile("worker-low")["instructions"])

    def test_instructions_encoding_round_trips(self):
        nasty = 'line1\nsay "hi" \\ backç\ttab\n'
        argv = codex_worker.codex_argv("codex", "/w", "/l", "none", "m", "low", nasty)
        self.assertEqual(self._di(argv), nasty)

    def test_prompt_drops_rules_duplicated_by_profile(self):
        prompt = codex_worker.build_prompt("/w", "feat/x", "Spec.", None)
        self.assertNotIn("personal data", prompt)
        self.assertNotIn("vault workflow", prompt)
        self.assertIn("STATUS: SUCCESS|PARTIAL|FAILED", prompt)
        self.assertIn("wrapper commits your changes", codex_worker.build_prompt("/w", "b", "S", None, commit=True))

    def test_report_and_usage_log_have_profile(self):
        rep = json.loads(self.run_cli("--json", "--profile", "worker-low").stdout)
        self.assertEqual(rep["profile"], "worker-low")
        self.assertIn("Profile: worker-medium", self.run_cli().stdout)
        e = json.loads((self.data / ".graph" / "usage.log").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(e["profile"], "worker-low")
        for k in ("budget", "class", "max_turns"):
            self.assertNotIn(k, e)
            self.assertNotIn(k, rep)

    def test_run_passes_flags_and_prompt_to_codex(self):
        r = self.run_cli("--model", "m-x", "--effort", "high", "--config-trim", "ignore-user-config")
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
