#!/usr/bin/env python3
"""Remind the root Codex agent to delegate after four matching tool calls."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Iterator

THRESHOLD = 4
MATCHED_TOOLS = {"Bash", "Read", "Edit", "Write", "apply_patch"}
MESSAGE = (
    "[delegation-warn] Bu kullanıcı turunda 4 veya daha fazla Bash/Read/Edit/Write "
    "aracı çağrısı yapıldı. Kalan çok adımlı veya uzun işi uygun bir worker-* alt "
    "ajanına devretmeyi değerlendir."
)


def _state_path(payload: dict) -> Path | None:
    session_id = payload.get("session_id")
    turn_id = payload.get("turn_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(turn_id, str) or not turn_id:
        return None
    key = f"{session_id}\0{turn_id}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
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
        return {"count": 0, "warned": False}
    if not isinstance(value, dict):
        return {"count": 0, "warned": False}
    count = value.get("count")
    warned = value.get("warned")
    return {
        "count": count if type(count) is int and count >= 0 else 0,
        "warned": warned if type(warned) is bool else False,
    }


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

    # Subagent hooks share the parent session and turn IDs, so agent_id must
    # be checked before touching that shared counter.
    if payload.get("agent_id"):
        return
    if payload.get("tool_name") not in MATCHED_TOOLS:
        return

    path = _state_path(payload)
    if path is None:
        return
    try:
        with _locked(path):
            state = _load_state(path)
            state["count"] += 1
            should_warn = state["count"] >= THRESHOLD and not state["warned"]
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
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
