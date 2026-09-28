#!/usr/bin/env python3
"""PreToolUse hook for the `Agent` tool (Claude Code's subagent launcher, the tool
formerly named `Task`; existing `Task(...)` references still resolve to it): denies
spawning a subagent unless its `subagent_type` is one of the engine's cheap workers
(a `worker-*` agent, e.g. worker-low/worker-medium) and, when the call also overrides
`model`, that override is one of the models configured as allowed for workers (see
below) -- never a pricier or unknown model.

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

Allowed worker models: PreToolUse input has no `model` field for the *orchestrator*
itself (per https://code.claude.com/docs/en/hooks, "Only SessionStart hooks can
receive a `model` field, and Claude Code doesn't always include it"), so this hook
cannot compare the subagent's requested model against the orchestrator's own model
directly. Instead it reads the orchestrator model from the same config `graph.py
models` manages (<data>/vault.config.json + <data>/.graph/machine.json, "models" ->
"orchestrator" -> "model") and allows any model strictly cheaper than it (see
tools/model_config.py for the ranking). `<data>` comes from the VAULT_DATA
environment variable, falling back to VAULT_HOME. Any problem at all reading that
config -- unset env, missing/invalid files, an import failure -- falls back to
today's default, ("sonnet", "haiku"), so a bug in the config never blocks a tool
call either.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT_ALLOWED_MODELS = ("sonnet", "haiku")


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


def _allowed_models() -> tuple[str, ...]:
    """Models a worker subagent may be started with (or overridden to): every
    model strictly cheaper than the configured orchestrator model (`graph.py
    models`). Falls back to DEFAULT_ALLOWED_MODELS if VAULT_DATA/VAULT_HOME is
    unset, the config can't be read, or anything else about this goes wrong."""
    data_raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
    if not data_raw:
        return DEFAULT_ALLOWED_MODELS
    model_config = _load_model_config()
    if model_config is None:
        return DEFAULT_ALLOWED_MODELS
    try:
        data_dir = Path(data_raw).expanduser()
        effective, _ = model_config.load_layered(
            data_dir / "vault.config.json", data_dir / ".graph" / "machine.json")
        orchestrator_model = effective["orchestrator"]["model"]
        allowed = tuple(model_config.allowed_worker_models(orchestrator_model))
        return allowed if allowed else DEFAULT_ALLOWED_MODELS
    except Exception:
        return DEFAULT_ALLOWED_MODELS


ALLOWED_MODELS = _allowed_models()

DENY_HINT = ("only worker-* subagents may be started (e.g. worker-low or worker-medium), "
             "and only with model {models} (or no model override). "
             "Use worker-low/worker-medium with model {first} instead.").format(
    models=" or ".join(ALLOWED_MODELS) or "sonnet or haiku",
    first=ALLOWED_MODELS[0] if ALLOWED_MODELS else "sonnet")


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

    if not isinstance(subagent_type, str) or not subagent_type.startswith("worker-"):
        _deny(f"agent-guard: subagent_type={subagent_type!r} is not a cheap worker; "
              f"{DENY_HINT}")
        return

    if model is not None and model not in ALLOWED_MODELS:
        _deny(f"agent-guard: model={model!r} is not allowed for subagents; {DENY_HINT}")
        return

    sys.exit(0)


if __name__ == "__main__":
    main()
