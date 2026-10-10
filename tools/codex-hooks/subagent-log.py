#!/usr/bin/env python3
"""SubagentStop hook for Codex: logs one `subagent_stop` event per finished subagent
to `<data>/.graph/usage.log` (same file and fields as the Claude hook; only written
if VAULT_DATA/VAULT_HOME points at an existing directory).

Documented Codex SubagentStop fields (https://learn.chatgpt.com/docs/hooks):
turn_id, agent_id, agent_type, agent_transcript_path, stop_hook_active,
last_assistant_message. Logged: agent_type, agent_id, stop_hook_active and the
length of last_assistant_message (not its text). No status/outcome field is
documented, so none is logged.

Codex requires JSON on stdout for SubagentStop, so this prints `{}` and exits 0.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime


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
        event = {"ts": datetime.now().isoformat(timespec="seconds"), "agent": "codex",
                 "session": str(payload.get("session_id") or ""), "event": "subagent_stop"}
        for key in ("agent_type", "agent_id"):
            if payload.get(key):
                event[key] = str(payload[key])
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
        payload = json.load(sys.stdin)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        log_event(payload)
    print("{}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
