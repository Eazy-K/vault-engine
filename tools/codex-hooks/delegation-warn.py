#!/usr/bin/env python3
"""Nudge Codex to consider delegating after many inline tool calls.

Warns at most once per turn, at THRESHOLD inline calls, and not at all once the
turn has spawned a worker (later calls are then verification). Orchestration
coordination (spawning and managing agents, plans) is not inline work; the
code-mode `exec` wrapper is not counted either.

Current PostToolUse payloads do not include agent_id, so subagent calls are
detected from the rollout's first record (session_meta with a subagent source
or parent_thread_id) and skipped; whether Codex fires global hooks for
subagents at all is unverified live. A completed turn is logged by the Stop
event (this script is also registered under Stop); if Stop never fires (e.g. an
interrupt) it is logged when the next turn's first tool call arrives.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Iterator
from datetime import datetime

THRESHOLD = 8
# Code mode's outer `exec` wrapper would double count its inner tools.exec_command/
# apply_patch calls (which arrive as "Bash"/"apply_patch"), so it is not counted.
WRAPPER_TOOLS = {"exec"}
# Orchestration coordination, not inline work. List ONLY names verified in real
# Codex payloads (verified live on Codex CLI 0.160.0, 2026-10-14, where they arrive
# flattened as collaborationspawn_agent, collaborationwait_agent,
# collaborationsend_message, collaborationinterrupt_agent); anything unlisted
# counts as inline work. Namespaced/flattened forms are normalized by _base_name.
SPAWN_TOOLS = {"Agent", "spawn_agent"}
COORDINATION_TOOLS = SPAWN_TOOLS | {"wait_agent", "send_message", "interrupt_agent"}
MESSAGE = ("[delegation-warn] 8 inline tool calls in this turn. If the remaining work is "
           "substantial or splits into independent parts, consider worker-low/worker-medium; "
           "small or tightly coupled work can stay inline.")


def _base_name(tool: str) -> str:
    """Strip a `collaboration` prefix or any `ns.`/`ns:`/`ns/` namespace."""
    name = tool
    for sep in (".", ":", "/"):
        name = name.rsplit(sep, 1)[-1]
    if name.startswith("collaboration") and name != "collaboration":
        name = name[len("collaboration"):].lstrip("_")
    return name


def _classify(tool: str) -> str:
    """"wrapper", "spawn", "coordination" or "inline"."""
    if tool in WRAPPER_TOOLS:
        return "wrapper"
    name = _base_name(tool)
    if name in SPAWN_TOOLS:
        return "spawn"
    if name in COORDINATION_TOOLS:
        return "coordination"
    return "inline"


def _is_subagent_rollout(raw: object) -> bool:
    """True when the rollout's first record (session_meta) marks a spawned subagent.

    Fails open: a missing or unreadable transcript is treated as the main session.
    """
    if not isinstance(raw, str) or not raw:
        return False
    try:
        with open(raw, encoding="utf-8") as stream:
            first = json.loads(stream.readline(1_048_576))
    except (OSError, UnicodeError, ValueError):
        return False
    meta = first.get("payload") if isinstance(first, dict) else None
    if not isinstance(first, dict) or first.get("type") != "session_meta" or not isinstance(meta, dict):
        return False
    source = meta.get("source")
    return bool(meta.get("parent_thread_id")
                or (isinstance(source, dict) and source.get("subagent")))


def _state_path(payload: dict) -> Path | None:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    base = Path(os.environ.get("CODEX_DELEGATION_WARN_STATE_DIR") or tempfile.gettempdir())
    return base / f"codex-delegation-warn-{digest}.json"


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Lock a per-turn state file across hook processes on Windows and POSIX."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _fresh_state(turn_id: object = None) -> dict:
    return {"turn_id": turn_id, "count": 0, "warned": False,
            "coordination": 0, "spawned": 0, "tools": {}}


def _load_state(path: Path) -> dict:
    """Load turn state; old files (count/warned only) get the new fields defaulted."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return _fresh_state()
    if not isinstance(value, dict):
        return _fresh_state()

    def _int(key: str) -> int:
        number = value.get(key)
        return number if type(number) is int and number >= 0 else 0

    warned = value.get("warned")
    tools = value.get("tools")
    state = _fresh_state(value.get("turn_id"))
    state.update(count=_int("count"), coordination=_int("coordination"), spawned=_int("spawned"),
                 warned=warned if type(warned) is bool else False)
    if isinstance(tools, dict):
        state["tools"] = {k: v for k, v in tools.items()
                          if isinstance(k, str) and type(v) is int and v >= 0}
    return state


def _log_prompt(session_id: str, state: dict) -> None:
    """Append a completed Codex turn to the local usage log, if configured."""
    try:
        raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
        if not raw:
            return
        data_dir = Path(raw).expanduser()
        if not data_dir.is_dir():
            return
        graph_dir = data_dir / ".graph"
        graph_dir.mkdir(exist_ok=True)
        event = {"ts": datetime.now().isoformat(timespec="seconds"), "agent": "codex",
                 "session": session_id, "event": "orchestrator_prompt",
                 "inline_calls": int(state.get("count") or 0),
                 "warned": bool(state.get("warned")), "threshold": THRESHOLD,
                 "coordination_calls": int(state.get("coordination") or 0),
                 "spawned": int(state.get("spawned") or 0),
                 "tools": dict(state.get("tools") or {})}
        with (graph_dir / "usage.log").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def _save_state(path: Path, state: dict) -> None:
    temp_path = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        temp_path.write_text(json.dumps(state), encoding="utf-8")
        temp_path.replace(path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (OSError, UnicodeError, ValueError):
        return
    if not isinstance(payload, dict):
        return

    # Codex payloads currently carry no agent_id; subagents are detected via the rollout session_meta.
    if payload.get("agent_id") or _is_subagent_rollout(payload.get("transcript_path")):
        return
    is_stop = payload.get("hook_event_name") == "Stop"
    tool = payload.get("tool_name")
    kind = "inline"
    if not is_stop:
        if not isinstance(tool, str):
            return
        kind = _classify(tool)
        if kind == "wrapper":
            return

    path = _state_path(payload)
    if path is None:
        return
    turn_id = payload.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        return
    session_id = payload["session_id"]
    try:
        with _locked(path):
            state = _load_state(path)
            if is_stop:
                # Codex requires JSON on stdout for Stop; "{}" is a no-op.
                if state.get("turn_id") == turn_id:
                    _log_prompt(session_id, state)
                    _save_state(path, _fresh_state())
                print("{}")
                return
            if state.get("turn_id") != turn_id:
                if state.get("turn_id") is not None:
                    _log_prompt(session_id, state)
                state = _fresh_state(turn_id)
            state["tools"][tool] = state["tools"].get(tool, 0) + 1
            if kind == "spawn":
                state["spawned"] += 1
            elif kind == "coordination":
                state["coordination"] += 1
            else:
                state["count"] += 1
            should_warn = (kind == "inline" and state["count"] >= THRESHOLD
                           and not state["warned"] and state["spawned"] == 0)
            if should_warn:
                state["warned"] = True
            _save_state(path, state)
    except OSError:
        # A missing temp location or lock failure must not disrupt the tool.
        return

    if should_warn:
        print(json.dumps({
            "systemMessage": MESSAGE,
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": MESSAGE,
            },
        }))


if __name__ == "__main__":
    main()
