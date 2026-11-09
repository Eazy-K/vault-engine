"""Tests for tools/queue_runner.py (`graph.py queue-runner`); no real claude, gh or network."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_core import GRAPH_PATH, graph  # noqa: E402

sys.path.insert(0, str(GRAPH_PATH.parent))
import queue_runner as qr  # noqa: E402

qr.g = graph  # other test modules replace sys.modules["graph"]; bind to ours

TASK = """---
type: task
project: {project}
status: {status}
created: {created}
budget: {budget}
keywords: [queue]
---

# {title}

## Goal
Something.

## Acceptance
```
{acceptance}
```

## Log
<!-- agent fills -->
"""

GOOD_JSON = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                        "result": "all done", "total_cost_usd": 1.25, "num_turns": 12,
                        "duration_ms": 5000,
                        "usage": {"input_tokens": 100, "output_tokens": 50,
                                  "cache_read_input_tokens": 1000,
                                  "cache_creation_input_tokens": 10}})


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout


def args(**kw):
    base = dict(action="run", dry_run=False, print_schedule=False, time="02:00", timeout=60,
                claude="claude-bin")
    base.update(kw)
    return argparse.Namespace(**base)


class QueueBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.projects = root / "projects"
        (self.data / ".graph").mkdir(parents=True)
        (self.data / "inbox" / "vault-engine").mkdir(parents=True)
        (self.projects / "vault-engine" / ".git").mkdir(parents=True)
        (self.projects / "other" / ".git").mkdir(parents=True)
        self.paths = graph.Paths(engine=graph.ENGINE, data=self.data)
        self.write_machine()
        git(self.data, "init", "-q") if False else subprocess.run(
            ["git", "init", "-q", str(self.data)], check=True)
        for k, v in (("user.name", "t"), ("user.email", "tester")):
            git(self.data, "config", k, v)
        self.wt = root / "wt"
        self.wt.mkdir()

    def write_machine(self, queue="default"):
        if queue == "default":
            queue = {"enabled": True, "projects": ["vault-engine"],
                     "budgets_usd": 5}
        raw = {"project_roots": [str(self.projects)]}
        if queue is not None:
            raw["queue"] = queue
        (self.data / ".graph" / "machine.json").write_text(json.dumps(raw), encoding="utf-8")

    def task(self, name, status="ready", created="2026-11-01", project="vault-engine",
             budget="M", acceptance="python -c pass"):
        path = self.data / "inbox" / project / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TASK.format(project=project, status=status, created=created, budget=budget,
                                    title=name, acceptance=acceptance), encoding="utf-8")
        return path

    def commit_all(self):
        git(self.data, "add", "-A")
        git(self.data, "commit", "-qm", "init")

    def events(self):
        log = self.data / ".graph" / "usage.log"
        return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    def run_queue(self, claude_stdout=GOOD_JSON, claude_rc=0, accept_exit=0, pr="https://example.invalid/pr/1",
                  timed_out=False, **kw):
        calls = {}

        def fake_claude(argv, wt, timeout):
            calls["argv"], calls["cwd"] = argv, wt
            if timed_out:
                return None, True
            return subprocess.CompletedProcess(argv, claude_rc, claude_stdout, ""), False

        def fake_accept(command, wt):
            return {"command": command, "exit": accept_exit, "tail": "Ran 5 tests\nOK" if not accept_exit
                    else "FAILED (failures=1)"}

        def fake_worktree(repo, number, slug):
            calls["repo"], calls["wt_args"] = repo, (number, slug)
            return self.wt, f"feat/queue-{slug}"

        def fake_pr(wt, branch, title, body):
            calls["pr_body"] = body
            return (pr, "") if pr else (None, "gh pr create failed: boom")

        out = io.StringIO()
        with mock.patch.object(qr, "run_claude", fake_claude), \
                mock.patch.object(qr, "run_acceptance", fake_accept), \
                mock.patch.object(qr, "make_worktree", fake_worktree), \
                mock.patch.object(qr, "create_pr", fake_pr), \
                contextlib.redirect_stdout(out):
            event = qr.execute(args(**kw), self.paths)
        calls["stdout"] = out.getvalue()
        return event, calls


class GuardTests(QueueBase):
    def test_refuses_without_budgets(self):
        self.write_machine({"enabled": True, "projects": ["vault-engine"]})
        self.task("0001-a", status="ready")
        with self.assertRaises(qr.QueueError) as cm:
            qr.execute(args(), self.paths)
        self.assertIn("budgets_usd", str(cm.exception))
        self.assertEqual(self.events()[-1]["outcome"], "refused")

    def test_refuses_when_disabled_or_missing(self):
        for queue in (None, {"enabled": False, "projects": ["vault-engine"], "budgets_usd": 1}):
            self.write_machine(queue)
            with self.assertRaises(qr.QueueError):
                qr.load_queue_config(self.paths)

    def test_rejects_bad_budget_values(self):
        for bad in (0, -1, "5", True, [5], {}):
            self.write_machine({"enabled": True, "projects": ["vault-engine"], "budgets_usd": bad})
            with self.assertRaises(qr.QueueError, msg=str(bad)):
                qr.load_queue_config(self.paths)

    def test_rejects_old_dict_shapes_with_fix(self):
        self.write_machine({"enabled": True, "projects": ["vault-engine"],
                            "budgets_usd": {"S": 2, "M": 5, "L": 10}})
        with self.assertRaises(qr.QueueError) as cm:
            qr.load_queue_config(self.paths)
        self.assertIn('"budgets_usd": 5', str(cm.exception))
        self.write_machine({"enabled": True, "projects": ["vault-engine"], "budgets_usd": 5,
                            "max_turns": {"S": 40, "M": 80}})
        with self.assertRaises(qr.QueueError) as cm:
            qr.load_queue_config(self.paths)
        self.assertIn('"max_turns": 80', str(cm.exception))

    def test_max_turns_defaults_and_override(self):
        cfg = qr.load_queue_config(self.paths)
        self.assertEqual(cfg["max_turns"], 80)
        self.assertEqual(cfg["budgets_usd"], 5.0)
        self.write_machine({"enabled": True, "projects": ["vault-engine"], "budgets_usd": 1,
                            "max_turns": 7})
        self.assertEqual(qr.load_queue_config(self.paths)["max_turns"], 7)
        self.write_machine({"enabled": True, "projects": ["vault-engine"], "budgets_usd": 1,
                            "max_turns": "x"})
        with self.assertRaises(qr.QueueError):
            qr.load_queue_config(self.paths)

    def test_cli_refusal_exit_code(self):
        self.write_machine(None)
        cp = subprocess.run([sys.executable, "-I", str(GRAPH_PATH.parent / "queue_runner.py"), "run"],
                            env={**os.environ, "VAULT_DATA": str(self.data)}, capture_output=True, text=True)
        self.assertEqual(cp.returncode, 2)
        self.assertIn("queue-runner:", cp.stderr)


class PickTests(QueueBase):
    def test_empty_queue(self):
        self.task("0001-a", status="open")
        event, calls = self.run_queue()
        self.assertEqual(calls["stdout"].strip(), "queue empty")
        self.assertEqual(event["outcome"], "empty")
        self.assertEqual(self.events()[-1]["event"], "queue_run")

    def test_oldest_first_with_filters(self):
        self.task("0003-new", created="2026-11-03")
        self.task("0002-old", created="2026-11-02")
        self.task("0001-oldest-other-project", created="2026-10-01", project="other")
        self.task("0000-oldest-no-accept", created="2026-10-02", acceptance="human")
        self.task("0000b-oldest-open", created="2026-10-03", status="open")
        cfg = qr.load_queue_config(self.paths)
        picked = qr.pick_task(self.paths, cfg)
        self.assertEqual(picked["id"], "inbox/vault-engine/0002-old")

    def test_same_created_orders_by_id(self):
        self.task("0009-b")
        self.task("0008-a")
        self.assertTrue(qr.pick_task(self.paths, qr.load_queue_config(self.paths))["id"].endswith("0008-a"))

    def test_task_budget_field_is_not_a_filter(self):
        self.task("0001-m", budget="XL")
        self.assertIsNotNone(qr.pick_task(self.paths, qr.load_queue_config(self.paths)))

    def test_dry_run_changes_nothing(self):
        path = self.task("0001-a")
        before = path.read_text(encoding="utf-8")
        event, calls = self.run_queue(dry_run=True)
        self.assertEqual(event["outcome"], "dry-run")
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertIn("--max-budget-usd 5", calls["stdout"])
        self.assertEqual(self.events(), [])


class RunTests(QueueBase):
    def test_green_path(self):
        path = self.task("0057-queue-thing")
        self.commit_all()
        event, calls = self.run_queue()
        text = path.read_text(encoding="utf-8")
        self.assertEqual(event["outcome"], "pr")
        self.assertIn("status: review", text)
        self.assertIn("queue-runner: PR https://example.invalid/pr/1", text)
        self.assertEqual(calls["wt_args"], ("0057", "queue-thing"))
        self.assertEqual(calls["repo"], (self.projects / "vault-engine").resolve())
        self.assertIn("all done", calls["pr_body"])
        self.assertIn("Ran 5 tests", calls["pr_body"])
        # claim and result are two commits of only the task file; tree is clean
        self.assertEqual(git(self.data, "status", "--porcelain", "--", "inbox").strip(), "")
        self.assertIn("claim", git(self.data, "log", "--format=%s", "-2").splitlines()[1])
        ev = self.events()[-1]
        self.assertEqual((ev["event"], ev["outcome"], ev["turns"], ev["tokens"], ev["usd"], ev["pr"]),
                         ("queue_run", "pr", 12, 1160, 1.25, "https://example.invalid/pr/1"))
        self.assertIn("duration_s", ev)

    def test_argv_contents(self):
        self.task("0001-a")
        _, calls = self.run_queue()
        argv = calls["argv"]
        self.assertEqual(argv[0], "claude-bin")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "auto")
        self.assertEqual(argv[argv.index("--max-turns") + 1], "80")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "5")
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertIn("-p", argv)
        prompt = argv[argv.index("-p") + 1]
        self.assertIn("0001-a.md", prompt)
        self.assertIn("do not open a PR", prompt.replace("Do not push, do not open a PR", "do not open a PR"))
        self.assertEqual(calls["cwd"], self.wt)

    def test_red_acceptance_blocks(self):
        path = self.task("0001-a")
        event, _ = self.run_queue(accept_exit=1)
        text = path.read_text(encoding="utf-8")
        self.assertEqual(event["outcome"], "blocked")
        self.assertIn("status: blocked", text)
        self.assertIn("acceptance red (exit 1)", text)
        self.assertIn("FAILED (failures=1)", text)
        self.assertIn("feat/queue-a", text)
        self.assertEqual(self.events()[-1]["outcome"], "blocked")
        self.assertIn("acceptance red", self.events()[-1]["reason"])

    def test_budget_hit_blocks_even_if_acceptance_green(self):
        path = self.task("0001-a")
        out = json.dumps({"subtype": "error_max_budget_usd", "is_error": True, "total_cost_usd": 5.01,
                          "num_turns": 30})
        event, _ = self.run_queue(claude_stdout=out, claude_rc=1)
        self.assertEqual(event["outcome"], "blocked")
        self.assertIn("budget hit: error_max_budget_usd", event["reason"])
        self.assertEqual(event["usd"], 5.01)
        self.assertIn("status: blocked", path.read_text(encoding="utf-8"))

    def test_timeout_and_garbage_output_block(self):
        self.task("0001-a")
        event, _ = self.run_queue(timed_out=True)
        self.assertIn("timed out", event["reason"])
        self.task("0002-b")
        event, _ = self.run_queue(claude_stdout="not json", claude_rc=1)
        self.assertEqual(event["outcome"], "blocked")
        self.assertIn("claude error", event["reason"])

    def test_pr_failure_blocks(self):
        path = self.task("0001-a")
        event, _ = self.run_queue(pr=None)
        self.assertEqual(event["outcome"], "blocked")
        self.assertIn("gh pr create failed", path.read_text(encoding="utf-8"))

    def test_claim_conflict_aborts_without_running(self):
        path = self.task("0001-a")
        self.commit_all()
        remote = Path(self.tmp.name) / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        git(self.data, "remote", "add", "origin", str(remote))
        real_git = qr._git

        def fake_git(repo, *a, **kw):
            if a and a[0] == "push":
                return subprocess.CompletedProcess(a, 1, "", "rejected (non-fast-forward)")
            if a and a[0] == "pull":  # nothing to pull in this scenario
                return subprocess.CompletedProcess(a, 0, "", "")
            return real_git(repo, *a, **kw)

        ran = []
        with mock.patch.object(qr, "_git", fake_git), \
                mock.patch.object(qr, "run_claude", lambda *a: ran.append(a)), \
                mock.patch.object(qr, "make_worktree", lambda *a: ran.append(a)):
            with self.assertRaises(qr.QueueError) as cm:
                qr.execute(args(), self.paths)
        self.assertIn("claim push rejected", str(cm.exception))
        self.assertEqual(ran, [])
        self.assertIn("status: ready", path.read_text(encoding="utf-8"))
        self.assertEqual(git(self.data, "status", "--porcelain", "--", "inbox").strip(), "")

    def test_status_edit_touches_only_the_status_line(self):
        path = self.task("0001-a")
        before = path.read_text(encoding="utf-8")
        qr.set_status(path, "in-progress")
        after = path.read_text(encoding="utf-8")
        self.assertEqual(before.replace("status: ready", "status: in-progress"), after)


class StatsAndScheduleTests(QueueBase):
    def test_queue_run_stats(self):
        events = [
            {"event": "queue_run", "outcome": "pr", "task": "t1", "usd": 2.0, "duration_s": 100, "turns": 10},
            {"event": "queue_run", "outcome": "blocked", "task": "t2", "usd": 4.0, "duration_s": 300, "turns": 30},
            {"event": "queue_run", "outcome": "empty", "task": ""},
            {"event": "context"},
        ]
        s = graph.queue_run_stats(events)
        self.assertEqual(s["outcomes"], {"blocked": 1, "empty": 1, "pr": 1})
        self.assertEqual((s["runs"], s["ran"]), (3, 2))
        self.assertEqual((s["usd_total"], s["usd_mean"]), (6.0, 3.0))
        self.assertEqual((s["duration_mean_s"], s["turns_mean"]), (200.0, 20.0))
        self.assertEqual(graph.queue_run_stats([{"event": "context"}]), {})

    def test_stats_command_prints_section(self):
        graph.log_usage(self.paths, {"event": "queue_run", "outcome": "pr", "task": "t1", "usd": 2.0,
                                     "duration_s": 100, "turns": 10})
        with mock.patch.object(graph, "default_paths", lambda: self.paths), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            graph.cmd_stats(argparse.Namespace(tokens=False, retrieval=False, no_transcripts=True,
                                               json=False, projects_dir=None))
        self.assertIn("queue runs:", out.getvalue())
        self.assertIn("$2.00 mean", out.getvalue())

    def test_print_schedule(self):
        text = qr.schedule_text(self.paths, "03:30")
        self.assertIn("schtasks /Create /SC DAILY /ST 03:30", text)
        self.assertIn("30 3 * * *", text)
        self.assertIn("-I", text)
        self.assertIn("VAULT_DATA", text)
        with self.assertRaises(qr.QueueError):
            qr.schedule_text(self.paths, "25:00")

    def test_cli_help_works_isolated(self):
        cp = subprocess.run([sys.executable, "-I", str(GRAPH_PATH.parent / "queue_runner.py"), "--help"],
                            capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0)
        self.assertIn("--print-schedule", cp.stdout)


if __name__ == "__main__":
    unittest.main()
