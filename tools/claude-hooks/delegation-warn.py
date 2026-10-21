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
