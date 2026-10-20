#!/usr/bin/env python3
"""PreToolUse hook for the `Agent` tool (Claude Code's subagent launcher, the tool
formerly named `Task`; existing `Task(...)` references still resolve to it): denies
spawning a subagent unless it is policy-allowed. Allowed (roles come from
tools/model_config.py): the worker roles (`worker-low`, `worker-medium`; a `model`
override must equal the configured model) and the read-only built-in agents
`Explore`, `Plan`, `claude-code-guide` (matched case-insensitively). A read-only
agent with a configured model (Explore -> haiku by default) gets that model
injected via `updatedInput` when the call has none; a different `model` is denied.
Read-only agents configured with no model inherit (nothing injected). `fork`,
`general-purpose`, `claude`, `default` and everything else are denied.

Reads the hook's JSON on stdin (see
https://code.claude.com/docs/en/hooks#pretooluse-input and the Agent tool's
`subagent_type`/`model` fields) and, to deny, writes
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
                         "permissionDecision": "deny",
                         "permissionDecisionReason": "..."}}
to stdout and exits 0 (Claude Code only reads hook JSON on exit 0; on exit 2
it would ignore stdout and the reason would not reach the model). Any other
tool, an allowed Agent call, a parse error, or `VAULT_AGENT_GUARD=off` all fall
through to a silent `exit(0)` (no decision -- normal permission flow applies),
so a bug here can never block a tool call.

Worker model: each worker runs with the model in its own configuration. The
configured model comes from the same config `graph.py models` manages
(<data>/vault.config.json + <data>/.graph/machine.json, "models" -> <role> ->
"model"; see tools/model_config.py). `<data>` comes from the VAULT_DATA
environment variable, falling back to VAULT_HOME. Any problem reading that
config -- unset env, missing/invalid files, an import failure -- falls back to the
built-in default model of the role, so a bug in the config never blocks a tool
call either.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

WORKER_ROLES = ("worker-low", "worker-medium")
FALLBACK_MODELS = {"worker-low": "sonnet", "worker-medium": "sonnet"}


def _load_model_config():
    """Best-effort import of tools/model_config.py, next to this script's parent
    folder (tools/claude-hooks/agent-guard.py -> tools/model_config.py). None on
    any failure -- this hook must never block a tool call over an import bug."""
    try:
        import importlib.util
        path = Path(__file__).resolve().parent.parent / "model_config.py"
        spec = importlib.util.spec_from_file_location("model_config", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def _effective(model_config) -> dict:
    """Layered role config (`graph.py models`); built-in defaults if VAULT_DATA /
    VAULT_HOME is unset. Raises on read problems -- callers fail open/fall back."""
    data_raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
    if not data_raw:
        return {role: dict(v) for role, v in model_config.DEFAULT_MODELS.items()}
    data_dir = Path(data_raw).expanduser()
    effective, _ = model_config.load_layered(
        data_dir / "vault.config.json", data_dir / ".graph" / "machine.json")
    return effective


def _policy():
    """(workers, readonly, effective): workers = worker role names, readonly =
    {lowercase agent name: role}, effective = role -> {model, effort}. Falls back
    to the built-in policy if model_config can't be loaded or read."""
    model_config = _load_model_config()
    if model_config is None:
        effective = {r: {"model": m} for r, m in FALLBACK_MODELS.items()}
        return WORKER_ROLES, {}, effective
    workers = tuple(getattr(model_config, "WORKER_ROLES", WORKER_ROLES))
    names = getattr(model_config, "READONLY_AGENT_NAMES", {})
    readonly = {name.lower(): role for role, name in names.items()}
    try:
        effective = _effective(model_config)
    except Exception:
        effective = {r: dict(v) for r, v in model_config.DEFAULT_MODELS.items()}
    return workers, readonly, effective


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }))
    sys.exit(0)


def main() -> None:
    if os.environ.get("VAULT_AGENT_GUARD") == "off":
        sys.exit(0)

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            sys.exit(0)

        if data.get("tool_name") != "Agent":
            sys.exit(0)

        tool_input = data.get("tool_input")
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        subagent_type = tool_input.get("subagent_type")
        model = tool_input.get("model")
    except (ValueError, OSError):
        sys.exit(0)

    workers, readonly, effective = _policy()
    if isinstance(subagent_type, str) and subagent_type in workers:
        role = subagent_type
    elif isinstance(subagent_type, str):
        role = readonly.get(subagent_type.strip().lower())
    else:
        role = None
    if role is None:
        allowed = list(workers) + sorted(readonly)
        _deny(f"agent-guard: subagent_type={subagent_type!r} is not allowed; only "
              + ", ".join(allowed) + " may be started.")
        return

    configured = (effective.get(role) or {}).get("model")
    if role in workers:
        if configured is not None and model is not None and (
                not isinstance(model, str) or model.strip().lower() != configured.lower()):
            _deny(f"agent-guard: model={model!r} does not match {subagent_type}'s "
                  f"configured model {configured!r}; omit the model override or pass "
                  f"{configured!r}.")
        sys.exit(0)

    if configured is None:
        sys.exit(0)
    if model is None:
        updated = dict(tool_input)
        updated["model"] = configured
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": f"agent-guard: {subagent_type} runs on {configured}.",
            "updatedInput": updated}}))
        sys.exit(0)
    if not isinstance(model, str) or model.strip().lower() != configured.lower():
        _deny(f"agent-guard: model={model!r} does not match {subagent_type}'s "
              f"configured model {configured!r}; omit the model override or pass "
              f"{configured!r}.")
    sys.exit(0)


if __name__ == "__main__":
    main()
