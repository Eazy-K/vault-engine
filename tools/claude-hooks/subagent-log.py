#!/usr/bin/env python3
"""SubagentStop hook for Claude Code: logs one `subagent_stop` event per finished
subagent run to `<data>/.graph/usage.log` (same file and ts/agent/session fields as
graph.py's log_usage; only written if VAULT_DATA/VAULT_HOME points at an existing
directory).

Fields come from the documented SubagentStop payload
(https://code.claude.com/docs/en/hooks): `agent_type`, `agent_id`,
`stop_hook_active`, `last_assistant_message`, `agent_transcript_path`. Logged:
agent_type, agent_id, stop_hook_active and the *length* of last_assistant_message
(not its text). The payload documents no status/outcome/duration/token field, so
none is logged. A stop with no agent_type and no meta file is a Claude Code internal
agent; it is logged with `"internal": true`. When the payload lacks agent_type, `agentType` is read from the
subagent meta file (`agent-<id>.meta.json` next to the transcript); no transcript
content is ever read.

Never blocks: prints nothing and exits 0 on any problem.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime


def meta_agent_type(payload: dict) -> tuple[str, bool]:
    """(agentType, internal) from the subagent's `.meta.json` (reads only that one key).

    `internal` is True only when a meta path could be derived and no meta file exists
    there: Claude Code internal agents have no transcript/meta file. A meta file that
    exists but lacks agentType, or no derivable path, is not internal."""
    found, checked = False, False
    try:
        agent_id = str(payload.get("agent_id") or "")
        paths = []
        atp = payload.get("agent_transcript_path")
        if atp:
            paths.append(os.path.splitext(str(atp))[0] + ".meta.json")
        tp = payload.get("transcript_path")
        if tp and agent_id:
            base = os.path.splitext(str(tp))[0]
            paths.append(os.path.join(base, "subagents", "agent-" + agent_id + ".meta.json"))
        for path in paths:
            checked = True
            if os.path.isfile(path):
                found = True
                with open(path, encoding="utf-8") as f:
                    value = json.load(f).get("agentType")
                if isinstance(value, str) and value:
                    return value, False
    except Exception:
        return "", False
    return "", checked and not found


def log_event(payload: dict) -> None:
    if not payload:
        return
    try:
        raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
        if not raw:
            return
        data_dir = os.path.expanduser(raw)
        if not os.path.isdir(data_dir):
            return
        graph_dir = os.path.join(data_dir, ".graph")
        os.makedirs(graph_dir, exist_ok=True)
        event = {"ts": datetime.now().isoformat(timespec="seconds"), "agent": "claude",
                 "session": str(payload.get("session_id") or ""), "event": "subagent_stop"}
        for key in ("agent_type", "agent_id"):
            if payload.get(key):
                event[key] = str(payload[key])
        if "agent_type" not in event:
            fallback, internal = meta_agent_type(payload)
            if fallback:
                event["agent_type"] = fallback
            elif internal:
                event["internal"] = True
        if "stop_hook_active" in payload:
            event["stop_hook_active"] = bool(payload["stop_hook_active"])
        last = payload.get("last_assistant_message")
        if isinstance(last, str):
            event["last_message_chars"] = len(last)
        with open(os.path.join(graph_dir, "usage.log"), "a", encoding="utf-8",
                  newline="\n") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return
    except Exception:
        return
    log_event(payload)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
