#!/usr/bin/env python3
"""UserPromptSubmit hook for Claude Code: warns the MODEL (and the user) when
the session's context usage crosses a threshold, and reminds it on every user
message to delegate multi-step work to a worker subagent.

Claude Code hooks are never handed the context window size directly, so this
estimates it from the *last assistant message* in the transcript's `usage`
field (input_tokens + cache_creation_input_tokens + cache_read_input_tokens
-- the same formula the docs use for context_window.used_percentage).

Env:
  VAULT_CONTEXT_WARN - threshold in tokens, default 150000.

State: a per-session temp file records the token level at which we last
warned, so we only warn again after context has grown by >= GROWTH_STEP
tokens since the last warning (avoids repeating the warning every turn).

Delegation reminder: a short, constant one-line reminder is added to every
prompt's context, independent of the context-usage warning above (see
REMINDER_MESSAGE below); both can appear together in the same output.

On any parse error or garbage input this prints nothing and exits 0; a bug
here can never block a prompt. A missing transcript or usage under the
threshold only silences the context-usage part -- the reminder is still
printed for every well-formed prompt.
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


REMINDER_MESSAGE = (
    "Reminder: delegate multi-step work to worker-low/worker-medium; if the "
    "task has not started, run `context` first."
)


def _context_usage_warning(payload: dict) -> str | None:
    """The context-usage warning line for this prompt, or None if none applies
    (under threshold, no prior assistant turn, or too soon since the last one)."""
    session_id = payload.get("session_id") or "unknown"
    transcript_path = payload.get("transcript_path")
    try:
        threshold = int(os.environ.get("VAULT_CONTEXT_WARN", str(DEFAULT_THRESHOLD)))
    except ValueError:
        threshold = DEFAULT_THRESHOLD

    usage = last_assistant_usage(transcript_path) if transcript_path else None
    if usage is None:
        return None  # no prior assistant turn yet (e.g. first prompt of session)

    tokens = context_tokens(usage)
    if tokens < threshold:
        return None  # under threshold, nothing to say

    sp = state_path(str(session_id))
    state = load_state(sp)
    last_warned_at = state.get("last_warned_at", 0)
    if not isinstance(last_warned_at, (int, float)):
        last_warned_at = 0

    if last_warned_at != 0 and tokens - last_warned_at < GROWTH_STEP:
        return None  # already warned recently, context hasn't grown enough since

    state["last_warned_at"] = tokens
    save_state(sp, state)
    return warning_message(tokens, threshold)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return
    except Exception:
        return  # no/garbage input, nothing to do

    lines = [REMINDER_MESSAGE]
    try:
        context_warning = _context_usage_warning(payload)
    except Exception:
        context_warning = None
    if context_warning:
        lines.append(context_warning)

    additional_context = "\n".join(lines)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        },
    }
    if context_warning:
        output["hookSpecificOutput"]["systemMessage"] = context_warning
    print(json.dumps(output))


if __name__ == "__main__":
    main()
