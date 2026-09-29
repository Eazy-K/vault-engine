#!/usr/bin/env python3
"""PostToolUse hook for Claude Code: nudges the orchestrator to delegate once it has
made several tool calls in a row on the main thread for the same user message,
instead of doing multi-step work itself.

Matcher: Bash|Read|Edit|Write (see the installer's DELEGATION_WARN_MATCHER). Only
fires for the *orchestrator* -- a hook call from inside a subagent carries an
`agent_id` field (see https://code.claude.com/docs/en/hooks: "agent_id ... is
present only when the hook fires inside a subagent"), and this hook returns
immediately when that field is present.

PreToolUse's decision-control output does not document `additionalContext` (only
`permissionDecision`, `permissionDecisionReason`, `systemMessage`, `updatedInput`);
PostToolUse's does, so this reminder is delivered as a PostToolUse hook.

State: a small per-session file under the system temp dir records the current
prompt_id and how many matched tool calls have been made for it, and whether we
already warned for this prompt_id. A new prompt_id (new user message) resets the
counter and the "already warned" flag, so the warning can fire again on the next
message but never spams every call within the same message.

Measurement: when a new prompt_id replaces the previous one, one JSON line
{"event": "orchestrator_prompt", "inline_calls": N, "warned": bool, ...} for the
finished prompt is appended to `<data>/.graph/usage.log` (same file and ts/agent/
session fields as graph.py's log_usage; `graph.py stats` summarises it). Only
written if VAULT_DATA/VAULT_HOME points at an existing directory. Limitation: the
last prompt of a session is never flushed (no later prompt triggers it, and this
hook has no session-end registration).

On any parse error, missing/unwritable state, or unexpected input shape, this
prints nothing and exits 0; a bug here can never block a tool call.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime

WARN_AFTER = 3  # warn once the count exceeds this (i.e. on the 4th+ call)

WARNING_MESSAGE = (
    "4+ tool calls in this request: delegate the rest to a worker "
    "(worker-low/worker-medium)."
)


def state_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "vault-engine-delegation")


def state_path(session_id: str) -> str:
    return os.path.join(state_dir(), f"{session_id}.json")


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def log_prompt(session_id: str, state: dict) -> None:
    """Append the finished prompt's inline-call count to the vault usage log.
    Silent on any problem (no data dir, unwritable log, ...)."""
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
                 "session": session_id, "event": "orchestrator_prompt",
                 "inline_calls": int(state.get("count") or 0),
                 "warned": bool(state.get("warned")), "threshold": WARN_AFTER}
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
        return  # no/garbage input, nothing to do

    if payload.get("agent_id"):
        return  # this hook fired inside a subagent, not the orchestrator

    session_id = payload.get("session_id")
    prompt_id = payload.get("prompt_id")
    if not session_id or not prompt_id:
        return  # not enough to key state on

    sp = state_path(str(session_id))
    state = load_state(sp)

    if state.get("prompt_id") != prompt_id:
        if state.get("prompt_id"):
            log_prompt(str(session_id), state)
        state = {"prompt_id": prompt_id, "count": 0, "warned": False}

    state["count"] = int(state.get("count") or 0) + 1
    already_warned = bool(state.get("warned"))

    if state["count"] > WARN_AFTER and not already_warned:
        state["warned"] = True
        save_state(sp, state)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": WARNING_MESSAGE,
                "systemMessage": WARNING_MESSAGE,
            },
        }))
        return

    save_state(sp, state)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
