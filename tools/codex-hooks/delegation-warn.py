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

Counter (ADR 0012): only implementation calls count toward `inline_calls` --
apply_patch/write tools and shell calls whose command is not read-only/verification
(`is_verification_command`, heuristic). Other tools are tallied as `read_calls`. The
meaning of `inline_calls` changed in this version; `engine_version` on the event
separates old and new data.

Classifier: operators split only outside quotes; `git stash list/show`, more read-only
git/gh subcommands and read-only PowerShell cmdlets (Get-ChildItem, Get-Content, ...;
Codex on Windows runs reads that way) are verification. `_GRAPH_READ` is unchanged:
`graph.py context` stays read-only, while `reinforce`, `init`, `migrate` count.
Spawn calls create the turn state, so a delegation-only turn logs `inline_calls: 0`
with `spawned`/`coordination_calls` at Stop. Parallel hook processes serialize on an
exclusive state-file lock (see `_locked`).

A/B switch: with VAULT_DELEGATION_AB=1|on|true the session id picks an arm
(`ab_arm` in the event): "warn" behaves as usual, "silent" counts and logs but never
warns. Events also carry engine_version, cwd and (if VAULT_LAB is set) lab.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Iterator
from datetime import datetime

THRESHOLD = 8
# Code mode's outer `exec` wrapper would double count its inner tools.exec_command/
# apply_patch calls (which arrive as "Bash"/"apply_patch"), so it is not counted.
WRAPPER_TOOLS = {"exec"}
# Orchestration coordination, not inline work. List ONLY names verified in real
# Codex payloads (verified live on Codex CLI 0.160.0, 2026-10-14, where they arrive
# flattened as collaborationspawn_agent, collaborationwait_agent,
# collaborationsend_message, collaborationinterrupt_agent, collaborationfollowup_task); anything unlisted
# counts as inline work. Namespaced/flattened forms are normalized by _base_name.
SPAWN_TOOLS = {"Agent", "spawn_agent"}
COORDINATION_TOOLS = SPAWN_TOOLS | {"wait_agent", "send_message", "interrupt_agent",
                                        "followup_task"}
# Write/edit tools and shell tools (names after _base_name). Shell calls count only when
# the command is not read-only/verification; every other tool is a read call.
WRITE_TOOLS = {"apply_patch", "edit", "write", "write_file", "edit_file", "multiedit",
               "notebookedit", "Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"shell", "exec_command", "local_shell", "container.exec", "Bash", "bash",
               "shell_command", "unified_exec"}

# --- Bash/PowerShell command classifier (heuristic; duplicated in the Claude and Codex
# hooks, tests assert both copies agree). Conservative: anything not clearly
# read-only/verification is treated as implementation work, so the counter errs towards
# counting, never towards silently ignoring a write. Operators (`| || && ;` newline) split
# segments only OUTSIDE quotes (so `grep -E "WARN|FAIL" f` is one segment); unbalanced
# quotes make the whole command count as implementation.
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELL_WRAP_RE = re.compile(
    r"^\s*(?:bash|sh|zsh|pwsh|powershell)(?:\.exe)?\s+(?:-[A-Za-z]+\s+)*"
    r"-(?:l?c|command)\s+(.+)$", re.I | re.S)
_READ_PROGRAMS = {"cat", "head", "tail", "sed", "grep", "rg", "find", "ls", "wc", "echo",
                  "printf", "diff", "tree", "type", "which", "pwd"}
# Read-only PowerShell cmdlets/aliases (matched case-insensitively). Set-Content, Out-File,
# New-Item, Remove-Item, Move-Item, Copy-Item, Add-Content, Rename-Item are deliberately
# absent, and `>` redirection is rejected in _segment_is_read_only.
_PS_READ = {"get-childitem", "gci", "dir", "get-content", "gc", "get-item", "get-location",
            "get-date", "select-string", "sls", "test-path", "resolve-path", "get-command",
            "measure-object", "select-object", "where-object", "sort-object", "format-table",
            "format-list", "out-string", "write-output", "write-host", "get-filehash",
            "compare-object", "convertfrom-json", "convertto-json"}
_GIT_READ = {"status", "diff", "log", "show", "branch", "worktree", "remote", "fetch",
             "rev-parse", "ls-files", "blame", "describe", "shortlog", "rev-list", "ls-tree",
             "cat-file", "name-rev", "merge-base", "tag", "config", "stash"}
_GIT_BRANCH_WRITE = ("-d", "-D", "-m", "-M", "-c", "-C", "-f", "-u", "-t", "--delete",
                     "--move", "--copy", "--force", "--set-upstream-to", "--unset-upstream",
                     "--track", "--edit-description")
_GIT_CONFIG_READ = ("--get", "--get-all", "--get-regexp", "--list", "-l")
_GIT_CONFIG_WRITE = ("--add", "--unset", "--unset-all", "--replace-all", "--edit", "-e",
                     "--rename-section", "--remove-section")
_GH_READ = {"pr": {"view", "checks", "status", "list", "diff"}, "run": {"view", "list"}}
_GH_API_WRITE_PREFIXES = ("-X", "--method", "-f", "-F", "--field", "--raw-field", "--input")
# `context` stays read-only; `reinforce`, `init`, `migrate` (and anything unlisted) count.
_GRAPH_READ = {"context", "stats", "show", "search", "tasks", "lint", "leakcheck", "doctor",
               "models", "--version", "--help"}
_PYTHONS = {"python", "python3", "py"}
_REDIRECT_OK_RE = re.compile(r"\d?>\s*&\d|\d?>\s*/dev/null|\d?>\s*NUL\b", re.I)


def _split_segments(cmd: str):
    """Split on `&&`, `||`, `|`, `;` and newlines outside quotes; None if quotes are unbalanced.

    Single quotes are literal; backslash escapes the next char outside single quotes.
    """
    segments, buf, quote, i, n = [], [], None, 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if quote == "'":
            buf.append(ch)
            if ch == "'":
                quote = None
        elif quote == '"':
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                i += 1
                buf.append(cmd[i])
            elif ch == '"':
                quote = None
        elif ch == "\\" and i + 1 < n:
            buf.append(ch)
            i += 1
            buf.append(cmd[i])
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch in "|&;\n":
            if ch in "|&" and cmd[i + 1:i + 2] == ch:
                i += 1  # `||` / `&&`
            elif ch == "&":
                buf.append(ch)  # lone `&` (e.g. `2>&1`) is not a separator
                i += 1
                continue
            segments.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if quote is not None:
        return None
    segments.append("".join(buf))
    return segments


def _tokens(segment: str) -> list:
    """Whitespace split outside quotes; surrounding quotes are dropped, backslashes kept."""
    out, buf, quote, started = [], [], None, False
    for ch in segment:
        if quote:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        elif ch in "'\"":
            quote, started = ch, True
        elif ch.isspace():
            if started or buf:
                out.append("".join(buf))
            buf, started = [], False
        else:
            buf.append(ch)
    if started or buf:
        out.append("".join(buf))
    return out


def _program(token: str) -> str:
    base = token.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base[:-4] if base.endswith(".exe") else base


def _git_is_read_only(args: list) -> bool:
    args = list(args)
    while args and args[0] in ("-C", "-c"):
        args = args[2:]  # `git -C <dir>` / `git -c k=v` take a value
    rest = [a for a in args if not a.startswith("-")]
    if not rest or rest[0] not in _GIT_READ:
        return False
    sub, flags = rest[0], [a for a in args if a.startswith("-")]
    if sub == "branch":
        if any(f in _GIT_BRANCH_WRITE for f in flags):
            return False
        # `git branch <name>` creates a branch; patterns are fine with --list.
        return len(rest) == 1 or any(f in ("--list", "-l") for f in flags)
    if sub == "tag":
        return any(f in ("-l", "--list") for f in flags)
    if sub == "config":
        return (any(f in _GIT_CONFIG_READ for f in flags)
                and not any(f in _GIT_CONFIG_WRITE for f in flags))
    if sub == "worktree":
        return rest[1:2] == ["list"]
    if sub == "remote":
        return rest[1:2] in ([], ["show"], ["get-url"])
    if sub == "stash":
        return rest[1:2] in (["list"], ["show"])
    return True


def _segment_is_read_only(segment: str) -> bool:
    segment = _REDIRECT_OK_RE.sub(" ", segment)
    if ">" in segment or "`" in segment or "$(" in segment:
        return False  # file redirection or command substitution: not provably read-only
    tokens = _tokens(segment)
    while tokens and _ENV_RE.match(tokens[0]):
        tokens.pop(0)
    if not tokens:
        return True  # empty segment (e.g. trailing separator)
    prog, args = _program(tokens[0]), tokens[1:]
    if prog == "cd":
        return True  # moving around changes no files
    if prog in _PS_READ:
        return True
    if prog in _READ_PROGRAMS:
        if prog == "sed" and any(a.startswith("-i") or a.startswith("--in-place") for a in args):
            return False
        if prog == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok", "-fprint")
                                  for a in args):
            return False
        return True
    if prog == "git":
        return _git_is_read_only(args)
    if prog == "gh":
        rest = [a for a in args if not a.startswith("-")]
        if rest[:1] == ["api"]:
            return not any(a.startswith(_GH_API_WRITE_PREFIXES) for a in args)
        return len(rest) >= 2 and rest[1] in _GH_READ.get(rest[0], ())
    if prog == "pytest":
        return True
    if prog in _PYTHONS:
        rest = list(args)
        while rest and rest[0].startswith("-") and rest[0] not in ("-m", "-c"):
            rest.pop(0)  # interpreter flags such as -u / -I / -B
        if not rest or rest[0] == "-c":
            return False
        if rest[0] == "-m":
            return len(rest) > 1 and rest[1] in ("unittest", "pytest")
        script = _program(rest[0])
        if script == "run_shard.py":
            return True
        if script == "graph.py":
            sub = [a for a in rest[1:] if not a.startswith("-")]
            if sub:
                return sub[0] in _GRAPH_READ
            return any(a in ("--version", "--help") for a in rest[1:])
        return False
    return False


def is_verification_command(cmd) -> bool:
    """True when every segment of a shell command is read-only or verification.

    Splits on && ; || | and newlines outside quotes, ignores `cd` and env assignments.
    An empty or unparsable (e.g. unbalanced quotes) command is NOT verification (it
    counts as implementation).
    """
    if isinstance(cmd, (list, tuple)):
        cmd = " ".join(str(c) for c in cmd)
    if not isinstance(cmd, str) or not cmd.strip():
        return False
    try:
        m = _SHELL_WRAP_RE.match(cmd)
        if m:  # bash -lc "..." / powershell -Command "..."
            inner = m.group(1).strip()
            if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
                q = inner[0]
                inner = inner[1:-1]
                if q == '"':
                    inner = inner.replace('\\"', '"')
            return is_verification_command(inner)
        parts = _split_segments(cmd)
        if parts is None:
            return False
        segments = [s for s in parts if s.strip()]
        return bool(segments) and all(_segment_is_read_only(s) for s in segments)
    except Exception:
        return False


def _command_of(payload: dict):
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("command", "cmd"):
            if tool_input.get(key):
                return tool_input[key]
    return None


def _is_counted(tool: str, payload: dict) -> bool:
    name = _base_name(tool)
    if name in WRITE_TOOLS or tool in WRITE_TOOLS:
        return True
    if name in SHELL_TOOLS or tool in SHELL_TOOLS:
        return not is_verification_command(_command_of(payload))
    return False


MESSAGE = ("[delegation-warn] 8 inline tool calls in this turn. Implementation, code "
           "exploration and test writing belong to worker-low/worker-medium (related "
           "steps in one worker); keep only a truly trivial one-line or single-step "
           "change inline. Planning, coordination and verification may continue.")


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
    """Exclusive cross-process lock around a load-modify-save of one turn state file.

    msvcrt.locking (Windows) / fcntl.flock (POSIX) on `<state>.lock`, taken
    non-blocking and retried every 20 ms for at most ~2 s (msvcrt.LK_LOCK would block
    up to ~10 s, beyond the hook budget). If the lock cannot be taken the hook runs
    unlocked (fail-open): a lost update beats a blocked tool call. Chosen over
    append-only per-call records because the state is tiny and one file keeps Stop
    trivially consistent; the Claude copy uses the same approach.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        got = False
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()

            def attempt() -> None:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            def attempt() -> None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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
        try:
            yield
        finally:
            if got:
                try:
                    if os.name == "nt":
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass


def _fresh_state(turn_id: object = None) -> dict:
    return {"turn_id": turn_id, "count": 0, "warned": False,
            "coordination": 0, "spawned": 0, "reads": 0, "tools": {}}


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
                 reads=_int("reads"),
                 warned=warned if type(warned) is bool else False)
    if isinstance(tools, dict):
        state["tools"] = {k: v for k, v in tools.items()
                          if isinstance(k, str) and type(v) is int and v >= 0}
    return state


def _log_prompt(session_id: str, state: dict, cwd: object = None) -> None:
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
                 "read_calls": int(state.get("reads") or 0),
                 "warned": bool(state.get("warned")), "threshold": THRESHOLD,
                 "coordination_calls": int(state.get("coordination") or 0),
                 "spawned": int(state.get("spawned") or 0),
                 "tools": dict(state.get("tools") or {})}
        event.update(measurement_fields(session_id, cwd))
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
                    _log_prompt(session_id, state, payload.get("cwd"))
                    _save_state(path, _fresh_state())
                print("{}")
                return
            if state.get("turn_id") != turn_id:
                if state.get("turn_id") is not None:
                    _log_prompt(session_id, state, payload.get("cwd"))
                state = _fresh_state(turn_id)
            counted = False
            state["tools"][tool] = state["tools"].get(tool, 0) + 1
            if kind == "spawn":
                state["spawned"] += 1
            elif kind == "coordination":
                state["coordination"] += 1
            elif _is_counted(tool, payload):
                state["count"] += 1
                counted = True
            else:
                state["reads"] += 1
            should_warn = (counted and state["count"] >= THRESHOLD
                           and not state["warned"] and state["spawned"] == 0
                           and ab_arm(session_id) != "silent")
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
