#!/usr/bin/env python3
"""PreToolUse hook for the `Agent` tool (Claude Code's subagent launcher, the tool
formerly named `Task`; existing `Task(...)` references still resolve to it): denies
spawning a subagent unless its `subagent_type` is one of the engine's cheap workers
(a `worker-*` agent, e.g. worker-low/worker-medium) and, when the call also overrides
`model`, that override is `sonnet` or `haiku` -- never a pricier or unknown model.

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
"""
from __future__ import annotations

import json
import os
import sys

ALLOWED_MODELS = ("sonnet", "haiku")

DENY_HINT = ("only worker-* subagents may be started (e.g. worker-low or worker-medium), "
             "and only with model sonnet or haiku (or no model override). "
             "Use worker-low/worker-medium with model sonnet instead.")


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
