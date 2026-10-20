#!/usr/bin/env python3
"""PreToolUse guard for Codex sub-agent spawns (`spawn_agent` / `Agent`).

Denies a spawn unless `agent_type` is a configured worker role (worker roles come
from tools/model_config.py, `worker-low` / `worker-medium`, layered with the vault
config like the Claude guard) AND its profile tools/codex-agents/<role>.toml
exists; any other profile file in that folder is ignored. Codex's built-in
`default` agent is denied explicitly. There are no read-only built-in roles here:
Explore / Plan / claude-code-guide and haiku are Claude Code only. Any `model` / `reasoning_effort`
override equals that profile's own configured model and effort.
Mirrors tools/claude-hooks/agent-guard.py.

Live status: on Codex CLI 0.160.0 (Windows, 2026-10-14) the spawn arrives as
`collaborationspawn_agent` and a PreToolUse deny was enforced live (no child
started) once the matcher matched it; the old matcher never matched that name.
Older CLIs did not dispatch PreToolUse for spawns at all (openai/codex#49736).

Fails open: malformed input, an unreadable profile directory or any other
error produces no output and exit 0, so it can never crash Codex or block a
tool call by accident. `VAULT_AGENT_GUARD=off` disables it.

Note: matching the spawn tool names (matcher/_is_spawn) does not prove that
Codex dispatches PreToolUse for spawns at all (openai/codex#49736).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent.parent / "codex-agents"
WORKER_ROLES = ("worker-low", "worker-medium")


def _worker_roles() -> tuple[str, ...]:
    """Worker roles from model_config (effective layered config); the built-in
    pair if it can't be loaded or read."""
    try:
        import importlib.util
        path = Path(__file__).resolve().parent.parent / "model_config.py"
        spec = importlib.util.spec_from_file_location("model_config", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
        if raw:
            data_dir = Path(raw).expanduser()
            effective, _ = module.load_layered(
                data_dir / "vault.config.json", data_dir / ".graph" / "machine.json")
        else:
            effective = module.DEFAULT_MODELS
        roles = tuple(r for r in module.WORKER_ROLES if r in effective)
        return roles or WORKER_ROLES
    except Exception:
        return WORKER_ROLES


def _profiles() -> dict[str, tuple[str, str]]:
    """Map role -> (model, reasoning effort) for the configured worker roles."""
    found: dict[str, tuple[str, str]] = {}
    for role in _worker_roles():
        path = AGENTS_DIR / f"{role}.toml"
        if not path.is_file():
            continue
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r'^(name|model|model_reasoning_effort)\s*=\s*"([^"]*)"\s*$', line)
            if match and match.group(1) not in values:
                values[match.group(1)] = match.group(2)
        if {"name", "model", "model_reasoning_effort"} <= values.keys():
            found[role] = (values["model"], values["model_reasoning_effort"])
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
    """`spawn_agent` / `Agent`, also namespaced (`collaboration.spawn_agent`) or flattened
    (`collaborationspawn_agent`, Codex multi-agent V2).

    Codex 0.159 issues spawns as a top-level function call, not through the
    code-mode `exec` wrapper, so ordinary `exec` calls never match.
    """
    return isinstance(name, str) and (
        name in ("Agent", "spawn_agent", "collaborationspawn_agent")
        or re.search(r"[._:/]spawn_agent$", name) is not None)


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
        allowed = ", ".join(sorted(profiles))
        if role == "default":
            return ("agent-guard: Codex's built-in default agent is not allowed; only "
                    f"{allowed} may be started.")
        return f"agent-guard: only {allowed} may be started."
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
