#!/usr/bin/env python3
"""PostToolUse + Stop hook for Claude Code: nudges the orchestrator to delegate once it has
made several tool calls in a row on the main thread for the same user message,
instead of doing multi-step work itself.

Matcher: `*` (see the installer's DELEGATION_WARN_MATCHER). Only implementation
calls count (`inline_calls`): Edit/Write/MultiEdit/NotebookEdit and Bash commands that
are not read-only/verification (heuristic `is_verification_command`). Other tools
(Read, Grep, Glob, mcp__*, ...) are tallied as `read_calls`; `Agent`/`Task` are
delegation and neither. NOTE: the meaning of `inline_calls` changed in this version
(it used to be every non-delegation call; old total = inline_calls + read_calls for new
data); `engine_version` on the event separates old and new data. Only
fires for the *orchestrator* -- a hook call from inside a subagent carries an
`agent_id` field (see https://code.claude.com/docs/en/hooks: "agent_id ... is
present only when the hook fires inside a subagent"), and this hook returns
immediately when that field is present.

PreToolUse's decision-control output does not document `additionalContext` (only
`permissionDecision`, `permissionDecisionReason`, `systemMessage`, `updatedInput`);
PostToolUse's does, so this reminder is delivered as a PostToolUse hook.

A/B switch: with VAULT_DELEGATION_AB=1|on|true the session id picks an arm
(`ab_arm` in the event): "warn" behaves as usual, "silent" counts and logs but never
warns. Events also carry engine_version, cwd and (if VAULT_LAB is set) lab.

State: a small per-session file under the system temp dir records the current
prompt_id and how many matched tool calls have been made for it, and whether we
already warned for this prompt_id. A new prompt_id (new user message) resets the
counter and the "already warned" flag, so the warning can fire again on the next
message but never spams every call within the same message.

Concurrency: parallel tool calls fire parallel hook processes, so the load-modify-save
of the state file runs under an exclusive lock (`locked`: msvcrt.locking / fcntl.flock on
`<state>.lock`, short non-blocking retry loop, fail-open after ~2 s). Chosen over
append-only per-call records because the state is tiny, the Codex copy already uses a
lock, and one file keeps Stop trivially consistent.

Classifier: operators split only outside quotes (`grep -E "A|B"` is one segment);
`git stash list/show`, more read-only git/gh subcommands and read-only PowerShell
cmdlets (Get-ChildItem, Get-Content, ...) are verification. `_GRAPH_READ` is unchanged:
`graph.py context` stays read-only, while `reinforce`, `init`, `migrate` count.
Agent/Task calls are tallied (`coordination_calls`, `spawned`) so a prompt that only
delegates still logs an `orchestrator_prompt` event with `inline_calls: 0`.

Measurement: one JSON line {"event": "orchestrator_prompt", "inline_calls": N,
"warned": bool, ...} per finished prompt is appended to `<data>/.graph/usage.log`
(same file and ts/agent/session fields as graph.py's log_usage; `graph.py stats`
summarises it). Only written if VAULT_DATA/VAULT_HOME points at an existing
directory. The line is flushed when the main agent's `Stop` event fires (the hook is
also registered under Stop; per https://code.claude.com/docs/en/hooks Stop runs when
the main agent has finished responding), so the last prompt of a session is logged.
SessionEnd was not chosen: it shares a 1.5 s budget and carries no per-prompt state.
Stop does not fire on a user interrupt or API error; then the fallback still
applies and the pending prompt is flushed when the next prompt_id arrives.

On any parse error, missing/unwritable state, or unexpected input shape, this
prints nothing and exits 0; a bug here can never block a tool call.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime

WARN_AFTER = 3  # warn once the count exceeds this (i.e. on the 4th+ call)

# Tools that are delegation itself; they never count as inline work.
DELEGATION_TOOLS = {"Agent", "Task"}
# Implementation tools. `Bash` counts too unless the command is read-only/verification.
# Everything else (Read, Grep, Glob, LS, Web*, Todo*, Skill, mcp__*, ...) is a read call.
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# --- Bash/PowerShell command classifier: shared with read-guard.py in
# tools/shell_classify.py (the Codex hook keeps its own copy; tests assert they agree).
def _load_classifier():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                        "shell_classify.py")
    spec = importlib.util.spec_from_file_location("shell_classify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_classify = _load_classifier()
is_verification_command = _classify.is_verification_command



def is_counted(payload: dict) -> bool:
    """True when this tool call is implementation work (see module docstring)."""
    tool = payload.get("tool_name")
    if tool in WRITE_TOOLS:
        return True
    if tool == "Bash":
        tool_input = payload.get("tool_input")
        cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
        return not is_verification_command(cmd)
    return False

WARNING_MESSAGE = (
    "[delegation-warn] 4+ inline tool calls in this request. Implementation, "
    "code exploration and test writing belong to worker-low/worker-medium "
    "(related steps in one worker); keep only a truly trivial one-line or "
    "single-step change inline. Planning, coordination and verification may "
    "continue."
)


def ab_arm(session_id: str) -> str:
    """"warn" / "silent" from the session id when VAULT_DELEGATION_AB is on, else "off"."""
    if str(os.environ.get("VAULT_DELEGATION_AB") or "").strip().lower() not in ("1", "on", "true"):
        return "off"
    last = hashlib.sha1(str(session_id).encode()).hexdigest()[-1]
    return "warn" if int(last, 16) % 2 == 0 else "silent"


def engine_version() -> str:
    """`__version__` of the graph.py next to this hook (regex, no import)."""
    try:
        graph = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "graph.py")
        with open(graph, encoding="utf-8") as f:
            m = re.search(r"^__version__\s*=\s*[\"']([^\"']+)[\"']", f.read(), re.M)
        return m.group(1) if m else "unknown"
    except Exception:
        return "unknown"


def measurement_fields(session_id: str, cwd) -> dict:
    fields = {"ab_arm": ab_arm(session_id), "engine_version": engine_version(),
              "cwd": str(cwd) if cwd else os.getcwd()}
    if os.environ.get("VAULT_LAB"):
        fields["lab"] = os.environ["VAULT_LAB"]
    return fields


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


@contextmanager
def locked(path: str):
    """Exclusive cross-process lock around a load-modify-save of one state file.

    msvcrt.locking (Windows) / fcntl.flock (POSIX) on `<state>.lock`, taken
    non-blocking and retried every 20 ms for at most ~2 s so the 5 s hook budget is
    never at risk. If the lock cannot be taken (timeout, no lock file) the hook runs
    unlocked: a lost update is better than a blocked tool call.
    """
    handle = None
    got = False
    try:
        os.makedirs(state_dir(), exist_ok=True)
        handle = open(path + ".lock", "a+b")
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            attempt = lambda: (handle.seek(0), msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1))
        else:
            import fcntl
            attempt = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        deadline = time.monotonic() + 2.0
        while True:
            try:
                attempt()
                got = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
    except OSError:
        pass
    try:
        yield
    finally:
        if handle is not None:
            try:
                if got:
                    if os.name == "nt":
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()


def save_state(path: str, state: dict) -> None:
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def log_prompt(session_id: str, state: dict, cwd=None) -> None:
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
                 "read_calls": int(state.get("reads") or 0),
                 "coordination_calls": int(state.get("coordination") or 0),
                 "spawned": int(state.get("spawned") or 0),
                 "warned": bool(state.get("warned")), "threshold": WARN_AFTER}
        event.update(measurement_fields(session_id, cwd))
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
    sid = str(session_id)
    warn = False
    with locked(sp):
        state = load_state(sp)

        if payload.get("hook_event_name") == "Stop":
            # Turn finished: flush the pending prompt (also a delegation-only one, which
            # logs inline_calls 0), then forget it so the next prompt_id (or a late tool
            # call) does not log it a second time.
            if state.get("prompt_id") == prompt_id:
                log_prompt(sid, state, payload.get("cwd"))
                save_state(sp, {})
            return

        if state.get("prompt_id") != prompt_id:
            if state.get("prompt_id"):
                log_prompt(sid, state, payload.get("cwd"))
            state = {"prompt_id": prompt_id, "count": 0, "reads": 0, "warned": False,
                     "coordination": 0, "spawned": 0}

        if payload.get("tool_name") in DELEGATION_TOOLS:
            # Delegation: neither inline nor read, but the prompt must still exist so a
            # delegation-only prompt produces an orchestrator_prompt event at Stop.
            state["coordination"] = int(state.get("coordination") or 0) + 1
            state["spawned"] = int(state.get("spawned") or 0) + 1
        elif not is_counted(payload):
            state["reads"] = int(state.get("reads") or 0) + 1
        else:
            state["count"] = int(state.get("count") or 0) + 1
            if (state["count"] > WARN_AFTER and not state.get("warned")
                    and ab_arm(sid) != "silent"):
                state["warned"] = True
                warn = True
        save_state(sp, state)

    if warn:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": WARNING_MESSAGE,
                "systemMessage": WARNING_MESSAGE,
            },
        }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
