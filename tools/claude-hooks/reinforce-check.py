#!/usr/bin/env python3
"""Stop hook for Claude Code: nudges the agent to run `graph.py reinforce` when a
task looks finished.

`graph.py context` ends with a line `... reinforce --task <id>` (6 hex chars), and
agents often forget to run it. Stop fires at the end of EVERY assistant turn, so
this hook blocks only when the current turn (everything after the last real user
message) shows a finish signal: a Bash call running `git commit`, `git push` or
`gh pr create`. It blocks when some task id printed by a `context` call in the
transcript has no `reinforce ... --task <id>` Bash call yet, and only once per
id and session.

Once-per-id state: a small per-session file under the system temp dir
(`vault-engine-reinforce/<session_id>.json`, the same place delegation-warn keeps
its state) lists the ids already blocked on.

Skipped: when `stop_hook_active` is true (a previous block of this hook is being
handled; blocking again would loop) and when the payload carries an `agent_id`
(fired inside a subagent). SubagentStop is not hooked.

Log: one `reinforce_check` line per decision in `<data>/.graph/usage.log` (same
file and ts/agent/session fields as graph.py's log_usage; only written if
VAULT_DATA/VAULT_HOME points at an existing directory).

Fails open: on any error this prints nothing and exits 0.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime

GRAPH = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      os.pardir, "graph.py"))
TASK_HINT = re.compile(r"reinforce --task ([0-9a-f]{6})\b")
FINISH = re.compile(r"\bgit\s+(?:-\S+\s+)*(?:commit|push)\b|\bgh\s+pr\s+create\b")


def state_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "vault-engine-reinforce")


def state_path(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
    return os.path.join(state_dir(), f"{safe}.json")


def load_blocked(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        blocked = data.get("blocked") if isinstance(data, dict) else None
        return blocked if isinstance(blocked, list) else []
    except (OSError, ValueError):
        return []


def save_blocked(path: str, blocked: list) -> None:
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"blocked": blocked}, f)
    except OSError:
        pass


def log_decision(session_id: str, outcome: str, task: str | None = None) -> None:
    """Append one reinforce_check line to the vault usage log. Silent on any problem."""
    try:
        raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
        if not raw:
            return
        data_dir = os.path.expanduser(raw)
        if not os.path.isdir(data_dir):
            return
        graph_dir = os.path.join(data_dir, ".graph")
        os.makedirs(graph_dir, exist_ok=True)
        event = {"ts": datetime.now().isoformat(timespec="seconds"), "agent": "claude-code",
                 "session": session_id, "event": "reinforce_check", "outcome": outcome}
        if task:
            event["task"] = task
        with open(os.path.join(graph_dir, "usage.log"), "a", encoding="utf-8",
                  newline="\n") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _blocks(entry: dict) -> list:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text_of(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text_of(v.get("text") if isinstance(v, dict) else v) for v in value)
    return ""


def read_entries(path: str) -> list:
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and not obj.get("isSidechain"):
                entries.append(obj)
    return entries


def analyse(entries: list) -> tuple[list, set, bool]:
    """(context task ids in order, ids already reinforced, finish signal this turn)."""
    tasks: list = []
    reinforced: set = set()
    finish = False
    for entry in entries:
        kind = entry.get("type")
        blocks = _blocks(entry)
        if kind == "user":
            is_result = any(b.get("type") == "tool_result" for b in blocks)
            if not is_result:
                finish = False  # a real user message starts a new turn
            for b in blocks:
                if b.get("type") == "tool_result":
                    for tid in TASK_HINT.findall(_text_of(b.get("content"))):
                        if tid not in tasks:
                            tasks.append(tid)
        elif kind == "assistant":
            for b in blocks:
                if b.get("type") != "tool_use" or b.get("name") != "Bash":
                    continue
                cmd = str((b.get("input") or {}).get("command", ""))
                if "graph.py" in cmd and "reinforce" in cmd:
                    reinforced.update(re.findall(r"--task[ =]([0-9a-f]{6})\b", cmd))
                if FINISH.search(cmd):
                    finish = True
    return tasks, reinforced, finish


def decide(payload: dict) -> tuple[str, str | None]:
    """(outcome, task id); outcome is "blocked" or an allow reason."""
    if payload.get("stop_hook_active"):
        return "allowed-stop-hook-active", None
    if payload.get("agent_id"):
        return "allowed-subagent", None
    path = payload.get("transcript_path")
    session_id = str(payload.get("session_id") or "")
    if not path or not session_id:
        return "allowed-no-input", None
    tasks, reinforced, finish = analyse(read_entries(str(path)))
    if not tasks:
        return "allowed-no-context", None
    missing = [t for t in tasks if t not in reinforced]
    if not missing:
        return "allowed-reinforced", None
    if not finish:
        return "allowed-no-finish-signal", None
    sp = state_path(session_id)
    blocked = load_blocked(sp)
    fresh = [t for t in missing if t not in blocked]
    if not fresh:
        return "allowed-already-blocked", None
    save_blocked(sp, blocked + fresh)
    return "blocked", fresh[0]


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return
    except Exception:
        return
    session_id = str(payload.get("session_id") or "")
    try:
        outcome, task = decide(payload)
    except Exception:
        outcome, task = "allowed-error", None
    log_decision(session_id, outcome, task)
    if outcome == "blocked":
        reason = (
            f"This task looks finished but `reinforce` has not run for task {task}. Run "
            f'`python "{GRAPH}" reinforce --task {task} <ids of the notes that actually '
            f"helped>` (just `--task {task}` if none helped), then mention it in one line "
            "of your final report."
        )
        print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
