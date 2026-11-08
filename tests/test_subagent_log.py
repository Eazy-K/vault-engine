"""Tests for tools/claude-hooks/subagent-log.py (SubagentStop measurement hook)."""
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
SCRIPT = REPO_ROOT / "tools" / "claude-hooks" / "subagent-log.py"


def ASSISTANT(text):
    return {"type": "assistant", "message": {"role": "assistant",
                                             "content": [{"type": "text", "text": text}]}}


def ASSISTANT_BLOCKS(texts):
    blocks = [{"type": "text", "text": t} for t in texts]
    blocks.insert(1, {"type": "tool_use", "name": "x", "input": {"text": "ignored"}})
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


class TestSubagentLog(unittest.TestCase):
    AGENT = "claude"
    PRINTS_JSON = False

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.data = self.tmp / "data"
        self.data.mkdir()

    def _run(self, stdin, data=True):
        env = dict(os.environ)
        env.pop("VAULT_HOME", None)
        env.pop("VAULT_DATA", None)
        if data:
            env["VAULT_DATA"] = str(self.data)
        return subprocess.run([sys.executable, str(SCRIPT)], input=stdin,
                              capture_output=True, text=True, env=env)

    def _events(self):
        log = self.data / ".graph" / "usage.log"
        return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] \
            if log.exists() else []

    def test_agent_type_falls_back_to_meta_file(self):
        sub = self.tmp / "proj" / "sess" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-abc.meta.json").write_text(
            json.dumps({"agentType": "worker-low", "description": "SECRET"}), encoding="utf-8")
        (sub / "agent-abc.jsonl").write_text("TRANSCRIPT", encoding="utf-8")
        for extra in ({"agent_transcript_path": str(sub / "agent-abc.jsonl")},
                      {"transcript_path": str(self.tmp / "proj" / "sess.jsonl")}):
            log = self.data / ".graph" / "usage.log"
            if log.exists():
                log.unlink()
            payload = {"session_id": "s1", "agent_id": "abc", **extra}
            self.assertEqual(self._run(json.dumps(payload)).returncode, 0)
            (event,) = self._events()
            self.assertEqual(event["agent_type"], "worker-low")
            self.assertNotIn("SECRET", json.dumps(event))
            self.assertNotIn("TRANSCRIPT", json.dumps(event))

    def test_missing_meta_file_is_harmless(self):
        payload = {"session_id": "s1", "agent_id": "zzz",
                   "agent_transcript_path": str(self.tmp / "nope" / "agent-zzz.jsonl")}
        self.assertEqual(self._run(json.dumps(payload)).returncode, 0)
        (event,) = self._events()
        self.assertNotIn("agent_type", event)
        if self.AGENT == "claude":
            self.assertTrue(event.get("internal"))

    def test_meta_exists_without_agent_type_not_internal(self):
        if self.AGENT != "claude":
            self.skipTest("claude only")
        sub = self.tmp / "proj" / "sess" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-abc.meta.json").write_text("{}", encoding="utf-8")
        payload = {"session_id": "s1", "agent_id": "abc",
                   "transcript_path": str(self.tmp / "proj" / "sess.jsonl")}
        self.assertEqual(self._run(json.dumps(payload)).returncode, 0)
        (event,) = self._events()
        self.assertNotIn("agent_type", event)
        self.assertNotIn("internal", event)

    def test_meta_with_agent_type_not_internal(self):
        if self.AGENT != "claude":
            self.skipTest("claude only")
        sub = self.tmp / "proj" / "sess" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-abc.meta.json").write_text('{"agentType": "worker-low"}', encoding="utf-8")
        payload = {"session_id": "s1", "agent_id": "abc",
                   "transcript_path": str(self.tmp / "proj" / "sess.jsonl")}
        self._run(json.dumps(payload))
        (event,) = self._events()
        self.assertEqual(event["agent_type"], "worker-low")
        self.assertNotIn("internal", event)

    def test_logs_documented_fields_only(self):
        payload = {"session_id": "s1", "hook_event_name": "SubagentStop",
                   "stop_hook_active": False, "agent_id": "def456", "agent_type": "Explore",
                   "agent_transcript_path": "/x/agent.jsonl",
                   "last_assistant_message": "Analysis complete."}
        out = self._run(json.dumps(payload))
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "{}" if self.PRINTS_JSON else "")
        (event,) = self._events()
        self.assertEqual(event["event"], "subagent_stop")
        self.assertEqual(event["agent"], self.AGENT)
        self.assertEqual(event["session"], "s1")
        self.assertEqual(event["agent_type"], "Explore")
        self.assertEqual(event["agent_id"], "def456")
        self.assertIs(event["stop_hook_active"], False)
        self.assertEqual(event["last_message_chars"], len("Analysis complete."))
        self.assertNotIn("Analysis", json.dumps(event))  # message text is never logged
        self.assertNotIn("agent_transcript_path", event)
        for key in ("budget_class", "turns", "tokens", "budget_hit"):  # Explore has no class
            self.assertNotIn(key, event)

    def test_missing_optional_fields_are_omitted(self):
        self._run(json.dumps({"session_id": "s1"}))
        (event,) = self._events()
        for key in ("agent_type", "agent_id", "stop_hook_active"):
            self.assertNotIn(key, event)

    def _transcript(self, lines):
        path = self.tmp / "agent.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)

    def test_message_chars_from_payload(self):
        self._run(json.dumps({"session_id": "s1", "last_assistant_message": "hello"}))
        (event,) = self._events()
        self.assertEqual(event["last_message_chars"], 5)
        self.assertEqual(event["last_message_source"], "payload")

    def test_message_chars_from_transcript_last_assistant(self):
        path = self._transcript([
            json.dumps(ASSISTANT("first answer")),
            json.dumps({"type": "user", "message": {"role": "user", "content": "q" * 50}}),
            "{not json",
            json.dumps(ASSISTANT_BLOCKS(["abc", "de"])),
            json.dumps({"type": "user", "message": {"role": "user", "content": "tail"}}),
        ])
        self._run(json.dumps({"session_id": "s1", "agent_transcript_path": path}))
        (event,) = self._events()
        self.assertEqual(event["last_message_chars"], 5)
        self.assertEqual(event["last_message_source"], "transcript")

    def test_transcript_path_fallback_and_str_content(self):
        path = self._transcript([json.dumps(
            {"type": "assistant", "message": {"role": "assistant", "content": "plain"}})])
        self._run(json.dumps({"session_id": "s1", "last_assistant_message": "",
                              "transcript_path": path}))
        (event,) = self._events()
        self.assertEqual(event["last_message_chars"], 5)
        self.assertEqual(event["last_message_source"], "transcript")

    def _handback(self, message, text=None):
        content = [{"type": "text", "text": text}] if text else []
        content.append({"type": "tool_use", "name": "SubagentHandback",
                        "input": {"message": message}})
        return {"type": "assistant", "message": {"role": "assistant", "content": content}}

    def _tool_only(self):
        return {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}

    def test_handback_report_length_counted(self):
        msg = "SUCCESS. " + "x" * 91
        path = self._transcript([
            json.dumps(ASSISTANT("earlier")),
            json.dumps(self._handback(msg)),
            json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "content": "delivered"}]}}),
        ])
        self._run(json.dumps({"session_id": "s1", "last_assistant_message": "",
                              "agent_transcript_path": path}))
        (event,) = self._events()
        self.assertEqual(event["last_message_chars"], len(msg))
        self.assertEqual(event["last_message_source"], "transcript")
        self.assertEqual(event["last_message_kind"], "handback")
        self.assertNotIn("SUCCESS", json.dumps(event))

    def test_walks_back_past_tool_only_record(self):
        path = self._transcript([
            json.dumps(ASSISTANT("earlier text")),
            json.dumps(self._tool_only()),
        ])
        self._run(json.dumps({"session_id": "s1", "agent_transcript_path": path}))
        (event,) = self._events()
        self.assertEqual(event["last_message_chars"], len("earlier text"))
        self.assertEqual(event["last_message_source"], "transcript")
        self.assertEqual(event["last_message_kind"], "text")

    def test_no_text_anywhere_is_source_none(self):
        path = self._transcript([json.dumps(self._tool_only()),
                                 json.dumps({"type": "user", "message": {"content": "q"}})])
        self._run(json.dumps({"session_id": "s1", "agent_transcript_path": path}))
        (event,) = self._events()
        self.assertEqual(event["last_message_chars"], 0)
        self.assertEqual(event["last_message_source"], "none")
        self.assertEqual(event["last_message_kind"], "none")

    def test_message_chars_none_when_nothing_available(self):
        self._run(json.dumps({"session_id": "s1",
                              "agent_transcript_path": str(self.tmp / "missing.jsonl")}))
        (event,) = self._events()
        self.assertEqual(event["last_message_chars"], 0)
        self.assertEqual(event["last_message_source"], "none")

    def test_no_data_dir_writes_nothing(self):
        out = self._run(json.dumps({"session_id": "s1"}), data=False)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(self._events(), [])

    def test_garbage_input_fails_open(self):
        for stdin in ("", "not json {{", "[1, 2]"):
            out = self._run(stdin)
            self.assertEqual(out.returncode, 0)
            self.assertEqual(out.stderr.strip(), "")
        self.assertEqual(self._events(), [])


def USAGE_ASSISTANT(input_tokens=0, creation=0, read=0):
    usage = {"input_tokens": input_tokens, "cache_creation_input_tokens": creation,
             "cache_read_input_tokens": read, "output_tokens": 5}
    return {"type": "assistant", "message": {"role": "assistant", "usage": usage,
                                             "content": [{"type": "text", "text": "step"}]}}


class TestSubagentBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.data = self.tmp / "data"
        self.data.mkdir()

    def _transcript(self, n_turns, last_usage=(1000, 0, 0)):
        records = [USAGE_ASSISTANT(10) for _ in range(n_turns - 1)] + [USAGE_ASSISTANT(*last_usage)]
        records.insert(1, {"type": "user", "message": {"role": "user", "content": "q"}})
        path = self.tmp / "agent.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                        encoding="utf-8", newline="\n")
        return str(path)

    def _run(self, agent_type="worker-medium", turns=5, last_usage=(1000, 0, 0), **extra):
        payload = {"session_id": "s1", "agent_type": agent_type, "agent_id": "a1",
                   "agent_transcript_path": self._transcript(turns, last_usage), **extra}
        env = dict(os.environ, VAULT_DATA=str(self.data))
        env.pop("VAULT_HOME", None)
        out = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0)
        log = self.data / ".graph" / "usage.log"
        events = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
        return out.stdout.strip(), events[-1]

    def test_under_threshold_logs_fields_silently(self):
        out, event = self._run(turns=5, last_usage=(1000, 200, 300))
        self.assertEqual(out, "")
        self.assertEqual(event["budget_class"], "M")
        self.assertEqual(event["turns"], 5)
        self.assertEqual(event["tokens"], 1500)
        self.assertEqual(event["budget_hit"], "none")

    def test_warn_at_80_percent(self):
        out, event = self._run(turns=48)  # 48/60 = 80%
        msg = json.loads(out)
        self.assertEqual(list(msg), ["systemMessage"])
        self.assertIn("worker-medium used 48/60 turns", msg["systemMessage"])
        self.assertIn("class M", msg["systemMessage"])
        self.assertEqual(event["budget_hit"], "warn")

    def test_block_over_max_turns(self):
        out, event = self._run(turns=61)
        decision = json.loads(out)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("blocked", decision["reason"])
        self.assertIn("61/60 turns", decision["reason"])
        self.assertIn("systemMessage", decision)
        self.assertEqual(event["budget_hit"], "block")

    def test_stop_hook_active_never_blocks(self):
        out, event = self._run(turns=61, stop_hook_active=True)
        self.assertEqual(out, "")
        self.assertEqual(event["budget_hit"], "block")

    def test_block_over_max_tokens(self):
        out, event = self._run(turns=3, last_usage=(100000, 100000, 60000))
        self.assertEqual(json.loads(out)["decision"], "block")
        self.assertEqual(event["tokens"], 260000)
        self.assertEqual(event["budget_hit"], "block")

    def test_config_override_from_vault_config(self):
        (self.data / "vault.config.json").write_text(json.dumps({"budgets": {
            "classes": {"M": {"max_turns": 3, "max_tokens": 250000}}}}),
            encoding="utf-8", newline="\n")
        out, event = self._run(turns=4)
        self.assertEqual(json.loads(out)["decision"], "block")
        self.assertEqual(event["budget_hit"], "block")

    def test_unknown_agent_type_has_no_budget_fields(self):
        out, event = self._run(agent_type="Explore", turns=500)
        self.assertEqual(out, "")
        for key in ("budget_class", "turns", "tokens", "budget_hit"):
            self.assertNotIn(key, event)


if __name__ == "__main__":
    unittest.main()
