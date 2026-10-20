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

_TAIL_BYTES = 2 * 1024 * 1024


def _text_len(content) -> int:
    if isinstance(content, str):
        return len(content)
    total = 0
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                total += len(block["text"])
    return total


def _assistant_chars(rec) -> int | None:
    """Text length of an assistant record, or None if `rec` is not one.

    Shapes: Claude `{"type":"assistant","message":{"content":...}}`."""
    if not isinstance(rec, dict):
        return None
    if rec.get("type") == "assistant":
        msg = rec.get("message")
        return _text_len(msg.get("content")) if isinstance(msg, dict) else 0
    return None


def transcript_last_chars(payload: dict) -> int | None:
    """Length of the text of the last assistant entry in the transcript JSONL (reads
    at most the last ~2 MB); None if no readable file or no assistant entry."""
    try:
        path = payload.get("agent_transcript_path") or payload.get("transcript_path")
        if not path or not os.path.isfile(str(path)):
            return None
        with open(str(path), "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - _TAIL_BYTES))
            data = f.read()
        lines = data.split(b"\n")
        if size > _TAIL_BYTES:
            lines = lines[1:]  # first line is probably cut mid-record
        for raw in reversed(lines):
            try:
                found = _assistant_chars(json.loads(raw.decode("utf-8", "replace")))
            except Exception:
                continue
            if found is not None:
                return found
    except Exception:
        pass
    return None


def last_message(payload: dict) -> tuple[int, str]:
    last = payload.get("last_assistant_message")
    if isinstance(last, str) and last:
        return len(last), "payload"
    chars = transcript_last_chars(payload)
    if chars is not None:
        return chars, "transcript"
    return 0, "none"



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
        event["last_message_chars"], event["last_message_source"] = last_message(payload)
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
