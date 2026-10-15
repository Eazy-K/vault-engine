#!/usr/bin/env python3
"""PreToolUse hook for the `Agent` tool (Claude Code's subagent launcher, the tool
formerly named `Task`; existing `Task(...)` references still resolve to it): denies
spawning a subagent unless its `subagent_type` is exactly `worker-low` or
`worker-medium` and, when the call also overrides `model`, that override equals the
model configured for that worker role.

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


def _configured_model(role: str) -> str:
    """The model configured for worker `role` (`graph.py models`). Falls back to
    the role's built-in default if VAULT_DATA/VAULT_HOME is unset, the config
    can't be read, or anything else about this goes wrong."""
    model_config = _load_model_config()
    if model_config is None:
        return FALLBACK_MODELS[role]
    try:
        data_raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
        if not data_raw:
            return model_config.configured_model(role)
        data_dir = Path(data_raw).expanduser()
        effective, _ = model_config.load_layered(
            data_dir / "vault.config.json", data_dir / ".graph" / "machine.json")
        return model_config.configured_model(role, effective)
    except Exception:
        return FALLBACK_MODELS[role]


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

    if subagent_type not in WORKER_ROLES:
        _deny(f"agent-guard: subagent_type={subagent_type!r} is not allowed; only "
              "worker-low or worker-medium may be started.")
        return

    if model is not None:
        configured = _configured_model(subagent_type)
        if not isinstance(model, str) or model.strip().lower() != configured.lower():
            _deny(f"agent-guard: model={model!r} does not match {subagent_type}'s "
                  f"configured model {configured!r}; omit the model override or pass "
                  f"{configured!r}.")
            return

    sys.exit(0)


if __name__ == "__main__":
    main()
