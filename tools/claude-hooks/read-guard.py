#!/usr/bin/env python3
"""PreToolUse hook (matcher `Read|Grep|Glob|Bash`) that gives the orchestrator a read
budget per user message: the first N file-browsing calls pass, the N+1th and later
ones are denied with a reason telling it to bundle the remaining reads into one
`worker-low` (or read-only `Explore`) subagent. The standard already says reading and
searching belong to a worker; delegation-warn.py only counts writes and never blocks.

What counts: the `Read`, `Grep` and `Glob` tools, and `Bash` commands that only browse
files (cat, head, tail, sed, grep, rg, find, ls, Get-Content, Select-String, ...; the
classifier is tools/shell_classify.py, shared with delegation-warn.py). Verification
(git, gh, pytest, `graph.py ...`) and anything that writes never count.

Budget: 3 by default, `VAULT_READ_BUDGET=<n>` overrides it (0 denies every read).
`VAULT_READ_GUARD=off` disables the hook. A call from inside a subagent (stdin JSON
has `agent_id`) is never counted or denied.

State: one small JSON file per session, `<data>/.graph/read-guard/<session>.json`
(`<data>` = VAULT_DATA, else VAULT_HOME; the system temp dir if neither points at an
existing folder), holding the current `prompt_id` and its read count. A new
`prompt_id` (new user message) resets the count. A short lock file serialises the
load-modify-save, because parallel tool calls run parallel hook processes.

Deny output (same shape as agent-guard.py), printed with exit 0:
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                         "permissionDecisionReason": "..."}}
Every error path (bad stdin, missing ids, unwritable state, import failure) is a silent
exit 0: a bug here can never block a tool call.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time

DEFAULT_BUDGET = 3
READ_TOOLS = {"Read", "Grep", "Glob"}


def _load_classifier():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                        "shell_classify.py")
    spec = importlib.util.spec_from_file_location("shell_classify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def budget() -> int:
    try:
        return max(0, int(os.environ.get("VAULT_READ_BUDGET", "").strip()))
    except ValueError:
        return DEFAULT_BUDGET


def is_read(payload: dict) -> bool:
    tool = payload.get("tool_name")
    if tool in READ_TOOLS:
        return True
    if tool == "Bash":
        tool_input = payload.get("tool_input")
        cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
        return bool(_load_classifier().is_browse_command(cmd))
    return False


def state_dir() -> str:
    raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
    if raw:
        data = os.path.expanduser(raw)
        if os.path.isdir(data):
            return os.path.join(data, ".graph", "read-guard")
    return os.path.join(tempfile.gettempdir(), "vault-engine-read-guard")


def state_path(session_id: str) -> str:
    digest = hashlib.sha1(session_id.encode("utf-8", "replace")).hexdigest()[:24]
    return os.path.join(state_dir(), f"{digest}.json")


def _acquire(lock: str) -> bool:
    """Exclusive create of `lock`, retried for ~2 s; a lock older than 5 s is stale."""
    deadline = time.monotonic() + 2.0
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > 5:
                    os.remove(lock)
                    continue
            except OSError:
                pass
        except OSError:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def bump(session_id: str, prompt_id: str) -> int:
    """Count this read for the prompt and return the new total."""
    path = state_path(session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock = path + ".lock"
    got = _acquire(lock)
    try:
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict) or state.get("prompt_id") != prompt_id:
            state = {"prompt_id": prompt_id, "reads": 0}
        state["reads"] = int(state.get("reads") or 0) + 1
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
        return state["reads"]
    finally:
        if got:
            try:
                os.remove(lock)
            except OSError:
                pass


def deny_reason(limit: int) -> str:
    return (f"read-guard: the orchestrator's read budget for this request ({limit}) is "
            "used up. Do not read more files yourself: bundle this and all remaining "
            "reads/searches into ONE worker-low subagent (or a read-only Explore one) "
            "with a precise question, and use its summary. For a legitimate one-off "
            "read, the user can raise VAULT_READ_BUDGET or set VAULT_READ_GUARD=off.")


def main() -> None:
    if os.environ.get("VAULT_READ_GUARD", "").strip().lower() == "off":
        return
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    if not isinstance(payload, dict) or payload.get("agent_id"):
        return
    session_id, prompt_id = payload.get("session_id"), payload.get("prompt_id")
    if not session_id or not prompt_id or not is_read(payload):
        return
    limit = budget()
    if bump(str(session_id), str(prompt_id)) > limit:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": deny_reason(limit),
        }}))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
