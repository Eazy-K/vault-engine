#!/usr/bin/env python3
"""UserPromptSubmit hook for Claude Code: warns the MODEL (and the user) when
the session's context usage crosses a threshold.

Claude Code hooks are never handed the context window size directly, so this
estimates it from the *last assistant message* in the transcript's `usage`
field (input_tokens + cache_creation_input_tokens + cache_read_input_tokens
-- the same formula the docs use for context_window.used_percentage).

Env:
  VAULT_CONTEXT_WARN - threshold in tokens, default 150000.

State: a per-session temp file records the token level at which we last
warned, so we only warn again after context has grown by >= GROWTH_STEP
tokens since the last warning (avoids repeating the warning every turn).

On any parse error, missing transcript, or usage under the threshold this
prints nothing and exits 0; a bug here can never block a prompt.
"""
import json
import os
import sys
import tempfile

GROWTH_STEP = 25000
DEFAULT_THRESHOLD = 150000


def last_assistant_usage(transcript_path):
    """Scan transcript JSONL backwards for the last assistant message usage."""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "assistant":
            usage = (obj.get("message") or {}).get("usage")
            if isinstance(usage, dict):
                return usage
    return None


def context_tokens(usage: dict) -> int:
    return (
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
    )


def state_path(session_id: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"vault-context-warn-{session_id}.json")


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {"last_warned_at": 0}
    except (OSError, ValueError):
        return {"last_warned_at": 0}


def save_state(path: str, state: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def warning_message(tokens: int, threshold: int) -> str:
    return (
        f"[context-warn] Estimated context usage is ~{tokens} tokens "
        f"(threshold {threshold}). Write the current state and progress to "
        f"the vault's status note now, then suggest the user start a new "
        f"session or run /compact soon to avoid losing context."
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return
    except Exception:
        return  # no/garbage input, nothing to do

    session_id = payload.get("session_id") or "unknown"
    transcript_path = payload.get("transcript_path")
    try:
        threshold = int(os.environ.get("VAULT_CONTEXT_WARN", str(DEFAULT_THRESHOLD)))
    except ValueError:
        threshold = DEFAULT_THRESHOLD

    usage = last_assistant_usage(transcript_path) if transcript_path else None
    if usage is None:
        return  # no prior assistant turn yet (e.g. first prompt of session)

    tokens = context_tokens(usage)
    if tokens < threshold:
        return  # under threshold, nothing to say

    sp = state_path(str(session_id))
    state = load_state(sp)
    last_warned_at = state.get("last_warned_at", 0)
    if not isinstance(last_warned_at, (int, float)):
        last_warned_at = 0

    if last_warned_at != 0 and tokens - last_warned_at < GROWTH_STEP:
        return  # already warned recently, context hasn't grown enough since

    msg = warning_message(tokens, threshold)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": msg,
            "systemMessage": msg,
        },
    }))

    state["last_warned_at"] = tokens
    save_state(sp, state)


if __name__ == "__main__":
    main()
