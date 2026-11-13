#!/usr/bin/env python3
"""PreToolUse hook for Claude Code: mid-run budget gate for worker subagents.

Payload fields used (https://code.claude.com/docs/en/hooks): `agent_id` (set only for
tool calls made inside a subagent; empty in the main session, where the hook does
nothing), `agent_type` (the role, e.g. worker-low; only roles with a turn cap in
tools/budgets.py / `budgets.roles` of vault.config.json are gated), `tool_name`
(`SubagentHandback`, the report tool, is never denied), `session_id`, `transcript_path`
and optionally `agent_transcript_path`. The subagent transcript is the
`agent_transcript_path`, else `transcript_path` when it already lies under a `subagents`
folder, else `<transcript_path without extension>/subagents/agent-<agent_id>.jsonl`.

Rule: turns, tokens = budgets.transcript_usage(path) (turns = distinct `message.id` of
assistant records); ratio = turns / turn cap. Tokens are only measured (logged).
- ratio >= 1.0: deny the tool call (`permissionDecision: deny`) with a reason telling the
  worker to stop and hand back a short BLOCKED report via SubagentHandback. The deny
  repeats on every call; a `budget_block` event (ts, agent, session, agent_type,
  agent_id, max_turns, turns, tokens, tool_name) goes to `<data>/.graph/usage.log`
  once per agent (marker `<data>/.graph/budget-warn/<agent_id>.block`).
- 0.8 <= ratio < 1.0: allow with an `additionalContext` warning, once per agent (marker
  `<data>/.graph/budget-warn/<agent_id>`; without a data dir there is no state, so the
  warning repeats).
- otherwise, and on any problem (bad stdin, missing transcript, import failure): prints
  nothing and exits 0 (fails open).

The data dir is VAULT_DATA, else VAULT_HOME, if it points at an existing directory.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime


class _FallbackBudgets:
    """Used if tools/budgets.py can't be imported; same numbers as the module."""
    ROLES = {"worker-low": 20, "worker-medium": 60, "worker-verify": 25,
             "worker-design": 60, "worker-design-verify": 25}

    @classmethod
    def load_budgets(cls, data_dir):
        return {"roles": dict(cls.ROLES)}

    @staticmethod
    def transcript_usage(path):
        """Same rule as budgets.transcript_usage: turns = distinct message.id of
        assistant records (id-less record = one turn); tokens of the last usage."""
        ids, anonymous, tokens = set(), 0, None
        try:
            if not path or not os.path.isfile(str(path)):
                return 0, None
            with open(str(path), "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict) or obj.get("type") != "assistant":
                        continue
                    msg = obj.get("message")
                    mid = msg.get("id") if isinstance(msg, dict) else None
                    if isinstance(mid, str) and mid:
                        ids.add(mid)
                    else:
                        anonymous += 1
                    usage = msg.get("usage") if isinstance(msg, dict) else None
                    if isinstance(usage, dict):
                        tokens = sum(int(usage.get(k) or 0) for k in (
                            "input_tokens", "cache_creation_input_tokens",
                            "cache_read_input_tokens"))
        except Exception:
            pass
        return len(ids) + anonymous, tokens

    @classmethod
    def role_turn_cap(cls, budgets, role):
        return (budgets.get("roles") or {}).get(role)


def _budgets_module():
    """tools/budgets.py (this script's parent folder), or the inline fallback."""
    try:
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent / "budgets.py"
        spec = importlib.util.spec_from_file_location("budgets", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return _FallbackBudgets


def _data_dir():
    raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
    if not raw:
        return None
    path = os.path.expanduser(raw)
    return path if os.path.isdir(path) else None


def _subagent_transcript(payload: dict, agent_id: str) -> str | None:
    atp = payload.get("agent_transcript_path")
    if atp:
        return str(atp)
    tp = payload.get("transcript_path")
    if not tp:
        return None
    tp = str(tp)
    parts = re.split(r"[\\/]", tp)
    if "subagents" in parts[:-1]:
        return tp
    return os.path.join(os.path.splitext(tp)[0], "subagents", "agent-" + agent_id + ".jsonl")


def _marker(data_dir, agent_id: str, suffix: str = "") -> str | None:
    if not data_dir:
        return None
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", agent_id) or "agent"
    return os.path.join(data_dir, ".graph", "budget-warn", safe + suffix)


def _first_time(data_dir, agent_id: str, suffix: str = "") -> bool:
    """True the first time for this agent (creates the marker); True without state."""
    path = _marker(data_dir, agent_id, suffix)
    if path is None:
        return True
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        return True


def _log_block(data_dir, payload: dict, agent_type: str, agent_id: str, max_turns: int,
               turns: int, tokens) -> None:
    try:
        graph_dir = os.path.join(data_dir, ".graph")
        os.makedirs(graph_dir, exist_ok=True)
        event = {"ts": datetime.now().isoformat(timespec="seconds"), "agent": "claude",
                 "session": str(payload.get("session_id") or ""), "event": "budget_block",
                 "agent_type": agent_type, "agent_id": agent_id, "max_turns": max_turns,
                 "turns": turns}
        if tokens is not None:
            event["tokens"] = tokens
        event["tool_name"] = str(payload.get("tool_name") or "")
        with open(os.path.join(graph_dir, "usage.log"), "a", encoding="utf-8",
                  newline="\n") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def decide(payload: dict) -> dict | None:
    agent_id = str(payload.get("agent_id") or "")
    if not agent_id or payload.get("tool_name") == "SubagentHandback":
        return None
    agent_type = str(payload.get("agent_type") or "")
    if not agent_type:
        return None
    data_dir = _data_dir()
    mod = _budgets_module()
    max_turns = mod.role_turn_cap(mod.load_budgets(data_dir), agent_type)
    if not max_turns:
        return None
    path = _subagent_transcript(payload, agent_id)
    if not path or not os.path.isfile(path):
        return None
    turns, tokens = mod.transcript_usage(path)
    ratio = turns / max_turns
    usage = f"turns {turns}"
    if ratio >= 1.0:
        if data_dir and _first_time(data_dir, agent_id, ".block"):
            _log_block(data_dir, payload, agent_type, agent_id, max_turns, turns, tokens)
        reason = (f"budget-guard: {agent_type} turn cap {max_turns} exceeded ({usage}). Stop working "
                  "now and deliver a short blocked report via SubagentHandback: status "
                  "BLOCKED, what is done, what remains, where the WIP is (branch/commit/"
                  "files). Do not call other tools.")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}
    if ratio >= 0.8:
        if not _first_time(data_dir, agent_id):
            return None
        msg = (f"budget-guard: {agent_type} turn cap {max_turns} is at >=80% ({usage}). Wrap up soon: "
               "commit your WIP and deliver your report via SubagentHandback before the "
               "limit; past 100% tool calls are denied.")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "additionalContext": msg}}
    return None


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return
        out = decide(payload)
        if out:
            print(json.dumps(out, ensure_ascii=False))
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
