#!/usr/bin/env python3
"""SubagentStop hook for Claude Code: logs one `subagent_stop` event per finished
subagent run to `<data>/.graph/usage.log` (same file and ts/agent/session fields as
graph.py's log_usage; only written if VAULT_DATA/VAULT_HOME points at an existing
directory).

Fields come from the documented SubagentStop payload
(https://code.claude.com/docs/en/hooks): `agent_type`, `agent_id`,
`stop_hook_active`, `last_assistant_message`, `agent_transcript_path`. Logged:
agent_type, agent_id, stop_hook_active and the *length* of the final message
(not its text): `last_message_chars`, `last_message_source` (payload | transcript | none)
and `last_message_kind` (text | handback | none). Worker subagents return their report
through a `SubagentHandback` tool call (no text block, empty payload message), so the
transcript scan counts that call's `input.message` length (kind "handback") and walks
back past assistant records with neither text nor handback. The payload documents no status/outcome/duration/token field, so
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


def _text_len(content) -> tuple[int, int]:
    """(text chars, SubagentHandback report chars) of a message `content`."""
    if isinstance(content, str):
        return len(content), 0
    text = handback = 0
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if isinstance(block.get("text"), str):
                text += len(block["text"])
            elif block.get("type") == "tool_use" and block.get("name") == "SubagentHandback":
                inp = block.get("input")
                if isinstance(inp, dict):
                    handback += len(str(inp.get("message", "")))
    return text, handback


def _assistant_chars(rec) -> tuple[int, str] | None:
    """(chars, kind) of an assistant record, or None if `rec` is not one or has no
    text and no handback. kind is "handback" when the chars come from a
    SubagentHandback tool call (how worker subagents return their report), else "text".

    Shape: Claude `{"type":"assistant","message":{"content":...}}`."""
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    text, handback = _text_len(msg.get("content"))
    if handback:
        return text + handback, "handback"
    if text:
        return text, "text"
    return None


def transcript_last(payload: dict) -> tuple[int, str] | None:
    """(chars, kind) of the last assistant entry in the transcript JSONL that has text
    or a handback (reads at most the last ~2 MB); None if no readable file or none."""
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


def last_message(payload: dict) -> tuple[int, str, str]:
    """(chars, source, kind): source payload|transcript|none, kind text|handback|none."""
    last = payload.get("last_assistant_message")
    if isinstance(last, str) and last:
        return len(last), "payload", "text"
    found = transcript_last(payload)
    if found is not None:
        return found[0], "transcript", found[1]
    return 0, "none", "none"


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
        (event["last_message_chars"], event["last_message_source"],
         event["last_message_kind"]) = last_message(payload)
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
