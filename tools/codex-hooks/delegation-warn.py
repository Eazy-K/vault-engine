#!/usr/bin/env python3
"""Remind Codex to delegate after repeated inline tool calls.

Current PostToolUse payloads do not include agent_id, so subagent calls are
detected from the rollout's first record (session_meta with a subagent source
or parent_thread_id) and skipped; whether Codex fires global hooks for
subagents at all is unverified live. A completed turn is logged when the
next turn's first matching tool call arrives; the final turn remains unlogged
until that happens because this hook does not receive turn-stop events.
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

THRESHOLD = 4
MATCHED_TOOLS = {"Bash", "Read", "Edit", "Write", "apply_patch"}
MESSAGE = "[delegation-warn] 4 tool calls in this turn; delegate the remaining work to worker-low/worker-medium."
REPEAT_MESSAGE = "[delegation-warn] More inline work has continued; stop and delegate the remaining work to worker-low/worker-medium."
REPEAT_EVERY = 2


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


def _load_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {"turn_id": None, "count": 0, "warned": False}
    if not isinstance(value, dict):
        return {"turn_id": None, "count": 0, "warned": False}
    count = value.get("count")
    warned = value.get("warned")
    return {
        "turn_id": value.get("turn_id"),
        "count": count if type(count) is int and count >= 0 else 0,
        "warned": warned if type(warned) is bool else False,
    }


def _log_prompt(session_id: str, state: dict) -> None:
    """Append a completed Codex turn to the local usage log, if configured.

    PostToolUse has no final-turn callback, so the final turn may never be
    flushed unless another turn begins in this session.
    """
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
                 "warned": bool(state.get("warned")), "threshold": THRESHOLD}
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
    if payload.get("tool_name") not in MATCHED_TOOLS:
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
            if state.get("turn_id") != turn_id:
                if state.get("turn_id") is not None:
                    _log_prompt(session_id, state)
                state = {"turn_id": turn_id, "count": 0, "warned": False}
            state["count"] += 1
            should_warn = (state["count"] >= THRESHOLD and
                           (state["count"] - THRESHOLD) % REPEAT_EVERY == 0)
            if state["count"] >= THRESHOLD:
                state["warned"] = True
            _save_state(path, state)
    except OSError:
        # A missing temp location or lock failure must not disrupt the tool.
        return

    if should_warn:
        message = MESSAGE if state["count"] == THRESHOLD else REPEAT_MESSAGE
        print(json.dumps({
            "systemMessage": message,
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": message,
            },
        }))


if __name__ == "__main__":
    main()
