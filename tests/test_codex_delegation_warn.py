"""Repeated Codex PostToolUse nudges and usage-log coverage."""
from __future__ import annotations

import hashlib
import json
import os
import re
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
SCRIPT = REPO_ROOT / "tools" / "codex-hooks" / "delegation-warn.py"


class TestCodexDelegationWarn(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.session = f"session-{id(self)}"
        self.env = dict(os.environ)
        self.env["CODEX_DELEGATION_WARN_STATE_DIR"] = str(self.tmp / "state")
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.env["VAULT_DATA"] = str(self.data)

    def _run(self, turn_id="turn-1", tool_name="Bash", agent_id=None, event=None,
             tool_input=None):
        payload = {"session_id": self.session, "turn_id": turn_id,
                   "tool_name": tool_name, "tool_input": tool_input or {}}
        if event:
            payload["hook_event_name"] = event
        if agent_id:
            payload["agent_id"] = agent_id
        if getattr(self, "transcript", None):
            payload["transcript_path"] = str(self.transcript)
        return subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                              capture_output=True, text=True, env=self.env)

    def test_code_mode_inner_tool_names_count_but_outer_exec_does_not(self):
        outs = [self._run(turn_id="t2", tool_name="exec") for _ in range(6)]
        self.assertTrue(all(not o.stdout.strip() for o in outs))
        outs = [self._run(turn_id="t3", tool_name="exec_command") for _ in range(8)]
        self.assertTrue(outs[7].stdout.strip())

    def test_warns_once_at_eight(self):
        outputs = [self._run() for _ in range(14)]
        self.assertEqual([i + 1 for i, out in enumerate(outputs) if out.stdout.strip()], [8])
        msg = json.loads(outputs[7].stdout)["systemMessage"]
        self.assertIn("8 inline tool calls", msg)
        self.assertIn("truly trivial one-line or single-step", msg)

    def test_coordination_calls_never_count_or_warn(self):
        names = ["wait_agent", "send_message", "interrupt_agent", "collaborationwait_agent",
                 "collaborationsend_message", "collaborationinterrupt_agent", "collaboration.wait_agent",
                 "followup_task", "collaborationfollowup_task"]
        for _ in range(3):
            for name in names:
                self.assertEqual(self._run(tool_name=name).stdout.strip(), "")
        self._run(turn_id="turn-2")
        event = self._events()[0]
        self.assertEqual(event["inline_calls"], 0)
        self.assertEqual(event["coordination_calls"], 27)
        self.assertFalse(event["warned"])

    def test_unverified_names_are_read_calls(self):
        for name in ("send_input", "update_plan", "close_agent", "wait"):
            for _ in range(2):
                self._run(tool_name=name)
        self._run(turn_id="turn-2")
        event = self._events()[0]
        self.assertEqual((event["inline_calls"], event["read_calls"], event["coordination_calls"]),
                         (0, 8, 0))

    def test_no_warning_after_spawn_in_same_turn(self):
        self._run(tool_name="collaborationspawn_agent")
        outs = [self._run() for _ in range(12)]
        self.assertTrue(all(not o.stdout.strip() for o in outs))
        self._run(turn_id="turn-2")
        event = self._events()[0]
        self.assertEqual((event["inline_calls"], event["spawned"], event["warned"]), (12, 1, False))

    def test_spawn_after_warning_keeps_single_warning(self):
        outs = [self._run() for _ in range(8)]
        self.assertTrue(outs[7].stdout.strip())
        self._run(tool_name="spawn_agent")
        self.assertTrue(all(not self._run().stdout.strip() for _ in range(5)))

    def test_unknown_tools_are_read_calls_and_appear_in_tools(self):
        for _ in range(3):
            self._run(tool_name="mystery_tool")
        self._run(tool_name="wait_agent")
        self._run(turn_id="turn-2")
        event = self._events()[0]
        self.assertEqual((event["inline_calls"], event["read_calls"]), (0, 3))
        self.assertEqual(event["tools"], {"mystery_tool": 3, "wait_agent": 1})
        self.assertEqual(event["threshold"], 8)

    def test_old_state_file_loads(self):
        import hashlib
        digest = hashlib.sha256(self.session.encode("utf-8")).hexdigest()[:32]
        state_dir = self.tmp / "state"
        state_dir.mkdir()
        (state_dir / f"codex-delegation-warn-{digest}.json").write_text(
            json.dumps({"turn_id": "turn-1", "count": 6, "warned": False}), encoding="utf-8")
        self.assertEqual(self._run().stdout.strip(), "")
        out = self._run()
        self.assertIn("8 inline tool calls", json.loads(out.stdout)["systemMessage"])
        self._run(turn_id="turn-2")
        event = self._events()[0]
        self.assertEqual((event["inline_calls"], event["coordination_calls"], event["spawned"]), (8, 0, 0))

    def _events(self):
        return [json.loads(x) for x in
                (self.data / ".graph" / "usage.log").read_text(encoding="utf-8").splitlines()]

    def _session_for(self, arm):
        for i in range(100):
            sid = f"ab-{i}"
            even = int(hashlib.sha1(sid.encode()).hexdigest()[-1], 16) % 2 == 0
            if even == (arm == "warn"):
                return sid
        raise AssertionError("no session id found")

    def _ab_run(self, arm, extra=None, calls=8):
        if arm != "off":
            self.env["VAULT_DELEGATION_AB"] = "on"
            self.session = self._session_for(arm)
        self.env.update(extra or {})
        outs = [self._run() for _ in range(calls)]
        self._run(event="Stop")
        return outs, self._events()

    def test_ab_silent_arm_never_warns_but_logs(self):
        outs, events = self._ab_run("silent", calls=10)
        self.assertTrue(all(o.stdout.strip() == "" for o in outs))
        self.assertEqual((events[0]["ab_arm"], events[0]["inline_calls"], events[0]["warned"]),
                         ("silent", 10, False))

    def test_ab_warn_arm_warns(self):
        outs, events = self._ab_run("warn")
        self.assertIn("delegation-warn", outs[7].stdout)
        self.assertEqual((events[0]["ab_arm"], events[0]["warned"]), ("warn", True))

    def test_ab_off_arm_unchanged(self):
        outs, events = self._ab_run("off")
        self.assertIn("delegation-warn", outs[7].stdout)
        self.assertEqual(events[0]["ab_arm"], "off")

    def test_event_has_engine_version_cwd_and_optional_lab(self):
        text = (REPO_ROOT / "tools" / "graph.py").read_text(encoding="utf-8")
        version = re.search(r'^__version__ = "([^"]+)"', text, re.M).group(1)
        _, events = self._ab_run("off")
        self.assertEqual(events[0]["engine_version"], version)
        self.assertTrue(events[0]["cwd"])
        self.assertNotIn("lab", events[0])
        self.session += "-lab"
        _, events = self._ab_run("off", extra={"VAULT_LAB": "lab-1"})
        self.assertEqual(events[-1]["lab"], "lab-1")

    def test_new_turn_resets_count_and_logs_previous_turn_as_codex(self):
        for _ in range(8):
            self._run()
        self.assertFalse((self.data / ".graph" / "usage.log").exists())
        out = self._run(turn_id="turn-2")
        self.assertEqual(out.stdout.strip(), "")
        events = [json.loads(line) for line in
                  (self.data / ".graph" / "usage.log").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "orchestrator_prompt")
        self.assertEqual(events[0]["agent"], "codex")
        self.assertEqual(events[0]["inline_calls"], 8)
        self.assertTrue(events[0]["warned"])

    def _rollout(self, payload):
        self.transcript = self.tmp / "rollout.jsonl"
        self.transcript.write_text(json.dumps({"type": "session_meta", "payload": payload})
                                   + "\n", encoding="utf-8")

    def test_subagent_rollout_is_ignored_and_not_logged(self):
        for meta in ({"source": {"subagent": {"thread_spawn": {"parent_thread_id": "p"}}}},
                     {"parent_thread_id": "p"}):
            with self.subTest(meta=meta):
                self._rollout(meta)
                for turn in ("a", "b"):
                    for _ in range(9):
                        self.assertEqual(self._run(turn_id=turn).stdout.strip(), "")
                self.assertFalse((self.data / ".graph" / "usage.log").exists())

    def test_main_rollout_still_warns_and_missing_transcript_fails_open(self):
        self._rollout({"source": "cli"})
        self.assertTrue(any(self._run().stdout.strip() for _ in range(8)))
        self.transcript = self.tmp / "missing.jsonl"
        self.session += "-2"
        self.assertTrue(any(self._run().stdout.strip() for _ in range(8)))

    def test_subagent_and_delegation_tool_calls_are_ignored(self):
        for _ in range(5):
            self.assertEqual(self._run(agent_id="worker").stdout.strip(), "")
            self.assertEqual(self._run(tool_name="spawn_agent").stdout.strip(), "")
            self.assertEqual(self._run(tool_name="collaboration.spawn_agent").stdout.strip(), "")
        self.assertFalse((self.data / ".graph" / "usage.log").exists())

    def _counted(self, tool, tool_input):
        self.session = f"cnt-{abs(hash((tool, json.dumps(tool_input))))}-{id(self)}"
        self._run(tool_name=tool, tool_input=tool_input)
        self._run(event="Stop")
        event = self._events()[-1]
        self.assertEqual(event["inline_calls"] + event["read_calls"], 1)
        return event["inline_calls"] == 1

    def test_read_tools_do_not_count_and_are_tallied(self):
        outs = [self._run(turn_id="r1", tool_name=n, tool_input={"path": "x"})
                for n in ("read_file", "list_dir", "grep", "web_search") * 3]
        self.assertTrue(all(not o.stdout.strip() for o in outs))
        self._run(turn_id="r2")
        event = self._events()[0]
        self.assertEqual((event["inline_calls"], event["read_calls"], event["warned"]),
                         (0, 12, False))

    def test_write_and_shell_classification(self):
        self.assertTrue(self._counted("apply_patch", {"input": "*** Begin Patch"}))
        self.assertTrue(self._counted("exec_command", {"cmd": "git commit -m x"}))
        self.assertTrue(self._counted("shell", {"command": ["bash", "-lc", "rm a.txt"]}))
        self.assertTrue(self._counted("local_shell", {"command": ["python", "setup.py", "build"]}))
        self.assertFalse(self._counted("exec_command", {"cmd": "git status"}))
        self.assertFalse(self._counted("shell", {"command": ["bash", "-lc", "cd x && git diff --stat"]}))
        self.assertFalse(self._counted("container.exec", {"command": ["python", "-m", "unittest", "tests.x"]}))

    def test_eight_shell_writes_still_warn(self):
        outs = [self._run(turn_id="w1", tool_name="apply_patch") for _ in range(8)]
        self.assertTrue(outs[7].stdout.strip())
        self.assertTrue(all(not o.stdout.strip() for o in outs[:7]))

    def test_classifier_matches_claude_hook_on_command_table(self):
        import importlib.util
        from test_delegation_warn import COMMAND_TABLE, SCRIPT as CLAUDE_SCRIPT
        mods = []
        for path in (CLAUDE_SCRIPT, SCRIPT):
            spec = importlib.util.spec_from_file_location(f"hook_{len(mods)}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mods.append(mod)
        for cmd, expected in COMMAND_TABLE:
            self.assertEqual(mods[0].is_verification_command(cmd), expected, cmd)
            self.assertEqual(mods[1].is_verification_command(cmd), expected, cmd)
            self.assertEqual(mods[1].is_verification_command(cmd.split(" ")), expected, cmd)

    def test_other_tools_do_not_count(self):
        outs = [self._run(turn_id="t9", tool_name="Other") for _ in range(12)]
        self.assertTrue(all(not o.stdout.strip() for o in outs))

    def test_stop_flushes_last_turn_once_and_prints_json(self):
        for _ in range(8):
            self._run()
        stop = self._run(event="Stop")
        self.assertEqual(json.loads(stop.stdout), {})
        log = self.data / ".graph" / "usage.log"
        events = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(e["event"], e["agent"], e["inline_calls"]) for e in events],
                         [("orchestrator_prompt", "codex", 8)])
        self._run(event="Stop")
        self._run(turn_id="turn-2")
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
