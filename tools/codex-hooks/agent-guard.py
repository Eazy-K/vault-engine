#!/usr/bin/env python3
"""PreToolUse guard for Codex sub-agent spawns (`spawn_agent` / `Agent`).

Denies a spawn unless `agent_type` is one of the engine's worker profiles
(tools/codex-agents/worker-*.toml) and any `model` / `reasoning_effort`
override equals that profile's own model and effort, so a spawn can never be
pricier than the profile. Mirrors tools/claude-hooks/agent-guard.py.

Honest status: the 0025 experiment on Codex CLI 0.159.2 did not demonstrate
that the CLI enforces a PreToolUse `deny` for sub-agent spawns. The hook is
installed for when the CLI honors it; until then it is advisory at best.

Fails open: malformed input, an unreadable profile directory or any other
error produces no output and exit 0, so it can never crash Codex or block a
tool call by accident. `VAULT_AGENT_GUARD=off` disables it.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent.parent / "codex-agents"


def _profiles() -> dict[str, tuple[str, str]]:
    """Map worker name -> (model, reasoning effort) from the shipped TOMLs."""
    found: dict[str, tuple[str, str]] = {}
    for path in sorted(AGENTS_DIR.glob("worker-*.toml")):
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r'^(name|model|model_reasoning_effort)\s*=\s*"([^"]*)"\s*$', line)
            if match and match.group(1) not in values:
                values[match.group(1)] = match.group(2)
        if {"name", "model", "model_reasoning_effort"} <= values.keys():
            found[values["name"]] = (values["model"], values["model_reasoning_effort"])
    return found


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def _is_spawn(name: object) -> bool:
    """`spawn_agent` / `Agent`, also namespaced (e.g. `collaboration.spawn_agent`).

    Codex 0.159 issues spawns as a top-level function call, not through the
    code-mode `exec` wrapper, so ordinary `exec` calls never match.
    """
    return isinstance(name, str) and (
        name == "Agent" or re.search(r"(^|[._:/])spawn_agent$", name) is not None)


def decide(payload: object, profiles: dict[str, tuple[str, str]]) -> str | None:
    """Return a deny reason, or None to let the call through."""
    if not isinstance(payload, dict) or not _is_spawn(payload.get("tool_name")):
        return None
    args = payload.get("tool_input")
    if not isinstance(args, dict) or not profiles:
        return None
    role = args.get("agent_type")
    profile = profiles.get(role) if isinstance(role, str) else None
    if profile is None:
        return ("agent-guard: only worker profiles may be started ("
                + ", ".join(sorted(profiles)) + ").")
    model, effort = profile
    if args.get("model") is not None and args["model"] != model:
        return f"agent-guard: {role} must use model {model}."
    if args.get("reasoning_effort") is not None and args["reasoning_effort"] != effort:
        return f"agent-guard: {role} must use reasoning_effort {effort}."
    return None


def _log(payload: object, reason: str | None) -> None:
    """Record one spawn evaluation in the vault usage log (no message text)."""
    try:
        if not isinstance(payload, dict) or not _is_spawn(payload.get("tool_name")):
            return
        raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
        if not raw:
            return
        data_dir = Path(raw).expanduser()
        if not data_dir.is_dir():
            return
        args = payload.get("tool_input")
        role = args.get("agent_type") if isinstance(args, dict) else None
        event = {"ts": datetime.now().isoformat(timespec="seconds"), "agent": "codex",
                 "event": "agent_guard", "decision": "deny" if reason else "allow",
                 "agent_type": role if isinstance(role, str) else None}
        if reason:
            event["reason"] = reason
        graph_dir = data_dir / ".graph"
        graph_dir.mkdir(exist_ok=True)
        with (graph_dir / "usage.log").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main() -> None:
    try:
        if os.environ.get("VAULT_AGENT_GUARD") == "off":
            return
        payload = json.load(sys.stdin)
        reason = decide(payload, _profiles())
        _log(payload, reason)
        if reason:
            _deny(reason)
    except Exception:
        return


if __name__ == "__main__":
    main()
