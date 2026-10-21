"""Tests for tools/claude-hooks/delegation-warn.py (PostToolUse hook that nudges
the orchestrator to delegate after several tool calls in one user message).

Runs the script as a real subprocess, feeding it JSON on stdin, with the state
directory redirected into a temp dir via TMPDIR/TEMP/TMP env overrides so the
real system temp dir is never touched. Standard library only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import sys as _sys
from pathlib import Path as _P
_sys.path.insert(0, str(_P(__file__).resolve().parent))
import _isolation  # noqa: E402,F401  (scrubs VAULT_DATA/VAULT_HOME)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "claude-hooks" / "delegation-warn.py"



# (command, is_verification) -- shared by both hooks' classifier tests.
COMMAND_TABLE = [
    ("git status", True),
    ("git diff --stat", True),
    ("cd C:/x && git diff --stat", True),
    ("python -m unittest tests.test_x", True),
    ("python tests/run_shard.py --jobs 3", True),
    ("python tools/graph.py leakcheck", True),
    ("FOO=1 pytest -q | cat", True),
    ("gh pr checks 12", True),
    ("git log --oneline; ls -la", True),
    ("python setup.py build", False),
    ("pip install foo", False),
    ("git commit -m x", False),
    ("git push origin main", False),
    ("gh pr create --title x", False),
    ("python tools/graph.py reinforce", False),
    ("cat a.txt && rm b.txt", False),
    ("echo hi > out.txt", False),
    ("sed -i s/a/b/ f.txt", False),
    ("", False),
    # quote-aware splitting: operators inside quotes do not split
    ('grep -iE "WARN|FAIL" file', True),
    ("grep -iE 'WARN|FAIL' file", True),
    ('grep -n "a" f | head -5', True),
    ('echo "a && b; c"', True),
    ('grep "unbalanced f', False),
    ('echo "ok" && rm "x y"', False),
    ('cat f | grep "a|b" | tee out', False),
    # git: stash list/show only, extra read-only subcommands
    ("git stash list", True),
    ("git stash show -p", True),
    ("git stash", False),
    ("git stash push -m x", False),
    ("git stash pop", False),
    ("git stash drop", False),
    ("git stash apply", False),
    ("git describe --tags", True),
    ("git shortlog -sn", True),
    ("git rev-list --count HEAD", True),
    ("git ls-tree HEAD", True),
    ("git ls-files", True),
    ("git cat-file -p HEAD", True),
    ("git name-rev HEAD", True),
    ("git merge-base a b", True),
    ("git tag -l", True),
    ("git tag --list 'v*'", True),
    ("git tag", False),
    ("git tag v1.0", False),
    ("git config --get user.name", True),
    ("git config --list", True),
    ("git config user.name x", False),
    ("git config --unset user.name", False),
    ("git remote", True),
    ("git remote -v", True),
    ("git remote show origin", True),
    ("git remote add o u", False),
    ("git worktree list", True),
    ("git worktree add ../x", False),
    ("git branch", True),
    ("git branch -a", True),
    ("git branch -r", True),
    ("git branch --show-current", True),
    ("git branch --list 'feat/*'", True),
    ("git branch foo", False),
    ("git branch -D foo", False),
    # gh
    ("gh run list", True),
    ("gh run view 12", True),
    ("gh api repos/o/r/pulls", True),
    ("gh api -X POST repos/o/r/issues", False),
    ("gh api repos/o/r/issues -f title=x", False),
    ("gh api repos/o/r/issues -F a=b", False),
    ("gh api repos/o/r --method=DELETE", False),
    # PowerShell cmdlets (case-insensitive) and pipelines
    ("Get-ChildItem -Force", True),
    ("get-childitem -Recurse", True),
    ("GCI", True),
    ("Get-Content README.md", True),
    ("Get-Content a.txt | Select-String foo | Select-Object -First 3", True),
    ("Test-Path x; Get-Location; Get-Date", True),
    ("Get-Content a | ConvertFrom-Json | ConvertTo-Json", True),
    ("Set-Content -Path hello.txt -Value hello", False),
    ("Out-File x.txt", False),
    ("New-Item x.txt", False),
    ("Remove-Item x", False),
    ("Copy-Item a b", False),
    ("Get-Content a | Set-Content b", False),
    ("Get-Content a > b", False),
    ('pwsh -Command "Get-ChildItem -Force | Select-Object -First 5"', True),
    ('powershell -c "Get-Content a | Where-Object Length"', True),
    ('powershell -NoProfile -Command "Get-Content a | Set-Content b"', False),
    ('bash -lc "grep -E \\"a|b\\" f"', True),
]


def load_hook():
    import importlib.util
    spec = importlib.util.spec_from_file_location("claude_delegation_warn", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDelegationWarn(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.session_id = f"test-{id(self)}"
        self.env = dict(os.environ)
        self.env["TMPDIR"] = str(self.tmp)
        self.env["TEMP"] = str(self.tmp)
        self.env["TMP"] = str(self.tmp)

    def _run(self, prompt_id="p1", tool_name="Bash", agent_id=None,
              env_extra=None, event=None, command=None) -> subprocess.CompletedProcess:
        payload = {"session_id": self.session_id, "prompt_id": prompt_id,
                   "tool_name": tool_name,
                   "tool_input": {} if command is None else {"command": command}}
        if event:
            payload["hook_event_name"] = event
        if agent_id is not None:
            payload["agent_id"] = agent_id
        env = dict(self.env)
        if env_extra:
            env.update(env_extra)
        return subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                               capture_output=True, text=True, env=env)

    def test_first_three_calls_are_silent(self):
        for _ in range(3):
            out = self._run()
            self.assertEqual(out.returncode, 0)
            self.assertEqual(out.stdout.strip(), "")

    def test_warns_once_after_three_calls(self):
        for _ in range(3):
            self._run()
        fourth = self._run()
        self.assertEqual(fourth.returncode, 0)
        payload = json.loads(fourth.stdout)
        msg = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("worker-low", msg)
        self.assertIn("worker", msg)

        # further calls in the same prompt stay silent (warn once per prompt)
        fifth = self._run()
        self.assertEqual(fifth.stdout.strip(), "")
        sixth = self._run()
        self.assertEqual(sixth.stdout.strip(), "")

    def test_new_prompt_id_resets_counter(self):
        for _ in range(4):
            self._run(prompt_id="p1")
        # new prompt_id: counter resets, so the next 3 calls are silent again
        for _ in range(3):
            out = self._run(prompt_id="p2")
            self.assertEqual(out.stdout.strip(), "")
        fourth = self._run(prompt_id="p2")
        self.assertNotEqual(fourth.stdout.strip(), "")

    def test_subagent_calls_are_always_skipped(self):
        for _ in range(6):
            out = self._run(agent_id="sub-1")
            self.assertEqual(out.returncode, 0)
            self.assertEqual(out.stdout.strip(), "")

    def test_garbage_stdin_exits_0_no_traceback(self):
        out = subprocess.run([sys.executable, str(SCRIPT)], input="not json {{{",
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")
        self.assertEqual(out.stderr.strip(), "")

    def test_empty_stdin_exits_0_no_traceback(self):
        out = subprocess.run([sys.executable, str(SCRIPT)], input="",
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")
        self.assertEqual(out.stderr.strip(), "")

    def test_non_object_json_exits_0_no_traceback(self):
        out = subprocess.run([sys.executable, str(SCRIPT)], input="[1, 2, 3]",
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")
        self.assertEqual(out.stderr.strip(), "")

    def test_missing_session_or_prompt_id_is_silent(self):
        out = subprocess.run([sys.executable, str(SCRIPT)],
                              input=json.dumps({"tool_name": "Bash"}),
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")

    def test_unwritable_state_dir_exits_0_no_traceback(self):
        # Point the state dir at a path that can't be created (a file in place
        # of a directory), so os.makedirs fails inside save_state.
        blocker = self.tmp / "vault-engine-delegation"
        blocker.write_text("not a directory", encoding="utf-8")
        out = self._run()
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stderr.strip(), "")

    def _data_env(self):
        data = self.tmp / "data"
        data.mkdir(exist_ok=True)
        return data, {"VAULT_DATA": str(data)}

    def _log(self, data):
        log = data / ".graph" / "usage.log"
        if not log.exists():
            return []
        return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]

    def test_previous_prompt_is_logged_on_prompt_change(self):
        data, env = self._data_env()
        for _ in range(4):
            self._run(prompt_id="p1", env_extra=env)
        self.assertEqual(self._log(data), [])  # current prompt is not flushed
        self._run(prompt_id="p2", env_extra=env)
        events = self._log(data)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "orchestrator_prompt")
        self.assertEqual(events[0]["inline_calls"], 4)
        self.assertTrue(events[0]["warned"])
        self.assertEqual(events[0]["session"], self.session_id)
        self.assertIn("ts", events[0])

    def _session_for(self, arm):
        import hashlib
        for i in range(100):
            sid = f"ab-{i}"
            even = int(hashlib.sha1(sid.encode()).hexdigest()[-1], 16) % 2 == 0
            if even == (arm == "warn"):
                return sid
        raise AssertionError("no session id found")

    def _ab_run(self, arm, extra=None, calls=4, flush=True):
        data, env = self._data_env()
        env = dict(env)
        env.update(extra or {})
        if arm != "off":
            env["VAULT_DELEGATION_AB"] = "TRUE"
            self.session_id = self._session_for(arm)
        outs = [self._run(env_extra=env) for _ in range(calls)]
        self._run(event="Stop", env_extra=env)
        return outs, self._log(data)

    def test_ab_silent_arm_never_warns_but_logs(self):
        outs, events = self._ab_run("silent", calls=6)
        self.assertTrue(all(o.stdout.strip() == "" for o in outs))
        (event,) = events
        self.assertEqual((event["ab_arm"], event["inline_calls"], event["warned"]),
                         ("silent", 6, False))

    def test_ab_warn_arm_warns(self):
        outs, events = self._ab_run("warn")
        self.assertIn("delegation-warn", outs[3].stdout)
        self.assertEqual((events[0]["ab_arm"], events[0]["warned"]), ("warn", True))

    def test_ab_off_arm_unchanged(self):
        outs, events = self._ab_run("off")
        self.assertIn("delegation-warn", outs[3].stdout)
        self.assertEqual(events[0]["ab_arm"], "off")

    def test_event_has_engine_version_cwd_and_optional_lab(self):
        text = (REPO_ROOT / "tools" / "graph.py").read_text(encoding="utf-8")
        import re
        version = re.search(r'^__version__ = "([^"]+)"', text, re.M).group(1)
        _, events = self._ab_run("off")
        self.assertEqual(events[0]["engine_version"], version)
        self.assertTrue(events[0]["cwd"])
        self.assertNotIn("lab", events[0])
        self.session_id += "-lab"
        _, events = self._ab_run("off", extra={"VAULT_LAB": "lab-1"})
        self.assertEqual(events[-1]["lab"], "lab-1")

    def test_no_log_without_data_dir(self):
        missing = str(self.tmp / "nope")
        for pid in ("p1", "p2"):
            out = self._run(prompt_id=pid, env_extra={"VAULT_DATA": missing})
            self.assertEqual(out.stderr.strip(), "")
        self.assertFalse((self.tmp / "nope").exists())
        env = {k: v for k, v in self.env.items() if k not in ("VAULT_DATA", "VAULT_HOME")}
        self.env = env
        self._run(prompt_id="p1")
        self.assertEqual(self._run(prompt_id="p2").stderr.strip(), "")

    def test_subagent_calls_are_not_logged(self):
        data, env = self._data_env()
        for pid in ("p1", "p2", "p3"):
            self._run(prompt_id=pid, agent_id="sub-1", env_extra=env)
        self.assertEqual(self._log(data), [])


    def test_read_tools_never_warn_and_are_tallied_as_read_calls(self):
        data, env = self._data_env()
        for _ in range(6):
            self.assertEqual(self._run(tool_name="Read", env_extra=env).stdout.strip(), "")
        self._run(tool_name="Stop", event="Stop", env_extra=env)
        (event,) = self._log(data)
        self.assertEqual((event["inline_calls"], event["read_calls"], event["warned"]),
                         (0, 6, False))

    def test_four_edits_warn_on_the_fourth(self):
        outs = [self._run(tool_name="Edit") for _ in range(4)]
        self.assertTrue(all(o.stdout.strip() == "" for o in outs[:3]))
        self.assertIn("delegation-warn", outs[3].stdout)

    def _counted(self, tool, command=None):
        """True if one call of this tool/command is counted as inline (via the event)."""
        data, env = self._data_env()
        self.session_id = f"cnt-{abs(hash((tool, command)))}-{id(self)}"
        self._run(tool_name=tool, command=command, env_extra=env)
        self._run(event="Stop", env_extra=env)
        event = self._log(data)[-1]
        return event["inline_calls"] == 1 and event["read_calls"] == 0

    def test_bash_classification_counts_only_implementation(self):
        for cmd in ("git status", "python -m unittest tests.test_x",
                    "cd C:/x && git diff --stat"):
            self.assertFalse(self._counted("Bash", cmd), cmd)
        for cmd in ("python setup.py build", "git commit -m x", "cat a.txt && rm b.txt"):
            self.assertTrue(self._counted("Bash", cmd), cmd)
        self.assertFalse(self._counted("mcp__foo__bar"))
        self.assertTrue(self._counted("Write"))

    def test_delegation_tools_never_count(self):
        for _ in range(6):  # delegating never counts
            self.assertEqual(self._run(prompt_id="b", tool_name="Agent").stdout.strip(), "")
            self.assertEqual(self._run(prompt_id="b", tool_name="Task").stdout.strip(), "")

    def test_is_verification_command_table(self):
        mod = load_hook()
        for cmd, expected in COMMAND_TABLE:
            self.assertEqual(mod.is_verification_command(cmd), expected, cmd)

    def test_stop_flushes_last_prompt_once(self):
        data, env = self._data_env()
        for _ in range(4):
            self._run(prompt_id="p1", env_extra=env)
        stop = self._run(prompt_id="p1", env_extra=env, event="Stop")
        self.assertEqual((stop.returncode, stop.stdout.strip()), (0, ""))
        events = self._log(data)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "orchestrator_prompt")
        self.assertEqual(events[0]["inline_calls"], 4)
        self.assertTrue(events[0]["warned"])
        self._run(prompt_id="p1", env_extra=env, event="Stop")  # no double log
        self._run(prompt_id="p2", env_extra=env)  # nothing pending to flush
        self.assertEqual(len(self._log(data)), 1)

    def test_delegation_only_prompt_logs_zero_inline_at_stop(self):
        data, env = self._data_env()
        self._run(tool_name="Agent", env_extra=env)
        self._run(tool_name="Task", env_extra=env)
        self._run(event="Stop", env_extra=env)
        (event,) = self._log(data)
        self.assertEqual((event["event"], event["inline_calls"], event["read_calls"]),
                         ("orchestrator_prompt", 0, 0))
        self.assertEqual((event["spawned"], event["coordination_calls"]), (2, 2))
        self.assertFalse(event["warned"])

    def test_delegation_after_inline_calls_keeps_counts(self):
        data, env = self._data_env()
        self._run(tool_name="Edit", env_extra=env)
        self._run(tool_name="Agent", env_extra=env)
        self._run(event="Stop", env_extra=env)
        (event,) = self._log(data)
        self.assertEqual((event["inline_calls"], event["spawned"]), (1, 1))

    def test_parallel_hook_processes_do_not_lose_updates(self):
        data, env = self._data_env()
        full = dict(self.env)
        full.update(env)
        calls = [("Read", None)] * 4 + [("Bash", "python setup.py build")] * 2 +                 [("Agent", None)] * 2
        procs = []
        for tool, cmd in calls:
            payload = {"session_id": self.session_id, "prompt_id": "par", "tool_name": tool,
                       "tool_input": {} if cmd is None else {"command": cmd}}
            proc = subprocess.Popen([sys.executable, str(SCRIPT)], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, env=full)
            proc.stdin.write(json.dumps(payload))
            proc.stdin.close()
            procs.append(proc)
        for proc in procs:
            proc.wait(timeout=30)
            proc.stdout.close()
            proc.stderr.close()
        self._run(prompt_id="par", event="Stop", env_extra=env)
        (event,) = self._log(data)
        self.assertEqual((event["read_calls"], event["inline_calls"], event["spawned"]),
                         (4, 2, 2))

    def test_stop_without_pending_prompt_logs_nothing(self):
        data, env = self._data_env()
        self._run(prompt_id="p1", env_extra=env, event="Stop")
        self.assertEqual(self._log(data), [])


if __name__ == "__main__":
    unittest.main()
