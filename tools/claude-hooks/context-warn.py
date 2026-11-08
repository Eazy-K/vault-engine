#!/usr/bin/env python3
"""UserPromptSubmit hook for Claude Code: warns the MODEL (and the user) when
the session's context has grown a lot beyond where the session started.

Claude Code hooks are never handed the context window size directly, so this
estimates it from the `usage` field of assistant messages in the transcript
(input_tokens + cache_creation_input_tokens + cache_read_input_tokens -- the
same formula the docs use for context_window.used_percentage).

Baseline plus delta: the size of the session's FIRST assistant call is the
baseline (system prompt, tools, memory files are already in it). It is
recorded once in the per-session state file. The hook then warns at
baseline + WARN_DELTA (default 50000) and suggests a handoff at
baseline + HANDOFF_DELTA (default 80000).

Config: `budgets.context` in `$VAULT_DATA/vault.config.json` (fallback
`$VAULT_HOME`) may set {"warn_delta": int, "handoff_delta": int}; invalid or
missing values fall back to the defaults.

Env:
  VAULT_CONTEXT_WARN - if set to a valid int, an ABSOLUTE warn threshold in
  tokens. It takes priority over baseline + warn_delta; the handoff threshold
  is then that value + (handoff_delta - warn_delta).

State: a per-session temp file holds the baseline, the token level of the last
warning and its level ("warn" | "handoff"). After a warning, the hook warns
again only after GROWTH_STEP more tokens, except that crossing from the warn
level into the handoff level always emits once.

When a message fires, one JSON line (event "context_warn") is appended to
`$VAULT_DATA/.graph/usage.log` if that directory exists.

Nothing is printed while under threshold. On any parse error or garbage input
this prints nothing and exits 0; a bug here can never block a prompt.
"""
import json
import os
import sys
import tempfile
from datetime import datetime

GROWTH_STEP = 25000
WARN_DELTA = 50000
HANDOFF_DELTA = 80000


def _iter_assistant_usages(transcript_path, reverse=False):
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    for line in (reversed(lines) if reverse else lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "assistant":
            message = obj.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict):
                yield usage


def last_assistant_usage(transcript_path):
    """Scan transcript JSONL backwards for the last assistant message usage."""
    return next(_iter_assistant_usages(transcript_path, reverse=True), None)


def first_assistant_usage(transcript_path):
    """Scan transcript JSONL forwards for the first assistant message usage."""
    return next(_iter_assistant_usages(transcript_path), None)


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
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(state, f)
    except OSError:
        pass


def _data_dir() -> str | None:
    raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
    return os.path.expanduser(raw) if raw else None


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def load_deltas() -> tuple[int, int]:
    """(warn_delta, handoff_delta) from vault.config.json, else the defaults."""
    warn, handoff = WARN_DELTA, HANDOFF_DELTA
    try:
        data_dir = _data_dir()
        if data_dir:
            with open(os.path.join(data_dir, "vault.config.json"), "r", encoding="utf-8") as f:
                cfg = json.load(f)
            ctx = cfg["budgets"]["context"]
            w, h = ctx.get("warn_delta"), ctx.get("handoff_delta")
            if _is_int(w) and _is_int(h) and h > w:
                warn, handoff = w, h
    except Exception:
        pass
    return warn, handoff


def warning_message(tokens: int, baseline: int) -> str:
    return (
        f"[context-warn] Context is about {tokens} tokens, {tokens - baseline} "
        f"above this session's first call (baseline {baseline}). Delegate "
        f"remaining work to subagents and keep tool output short."
    )


def handoff_message(tokens: int, baseline: int) -> str:
    return (
        f"[context-warn] Context is about {tokens} tokens, {tokens - baseline} "
        f"above baseline. Write a handoff note (standards/handoff in the vault) "
        f"and continue in a new session."
    )


def log_event(session_id: str, level: str, tokens: int, baseline: int, threshold: int) -> None:
    try:
        data_dir = _data_dir()
        if not data_dir:
            return
        graph_dir = os.path.join(data_dir, ".graph")
        if not os.path.isdir(graph_dir):
            return
        event = {"ts": datetime.now().isoformat(timespec="seconds"), "agent": "claude",
                 "session": session_id, "event": "context_warn", "level": level,
                 "tokens": tokens, "baseline": baseline, "threshold": threshold}
        with open(os.path.join(graph_dir, "usage.log"), "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass


def _context_usage_warning(payload: dict) -> str | None:
    """The warning line for this prompt, or None if none applies."""
    session_id = str(payload.get("session_id") or "unknown")
    transcript_path = payload.get("transcript_path")
    if not transcript_path:
        return None

    sp = state_path(session_id)
    state = load_state(sp)
    baseline = state.get("baseline")
    if not _is_int(baseline):
        first = first_assistant_usage(transcript_path)
        if first is None:
            return None  # nothing to measure yet
        baseline = context_tokens(first)
        state["baseline"] = baseline
        save_state(sp, state)

    last = last_assistant_usage(transcript_path)
    if last is None:
        return None
    tokens = context_tokens(last)

    warn_delta, handoff_delta = load_deltas()
    try:
        warn_at = int(os.environ["VAULT_CONTEXT_WARN"])
        handoff_at = warn_at + (handoff_delta - warn_delta)
    except (KeyError, ValueError):
        warn_at = baseline + warn_delta
        handoff_at = baseline + handoff_delta

    if tokens >= handoff_at:
        level, threshold = "handoff", handoff_at
    elif tokens >= warn_at:
        level, threshold = "warn", warn_at
    else:
        return None

    last_warned_at = state.get("last_warned_at", 0)
    if not isinstance(last_warned_at, (int, float)):
        last_warned_at = 0
    escalating = level == "handoff" and state.get("last_level") != "handoff"
    if last_warned_at != 0 and not escalating and tokens - last_warned_at < GROWTH_STEP:
        return None  # already warned recently, context hasn't grown enough since

    state["last_warned_at"] = tokens
    state["last_level"] = level
    save_state(sp, state)
    log_event(session_id, level, tokens, baseline, threshold)
    return (handoff_message if level == "handoff" else warning_message)(tokens, baseline)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return
    except Exception:
        return  # no/garbage input, nothing to do

    try:
        context_warning = _context_usage_warning(payload)
    except Exception:
        context_warning = None
    if not context_warning:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context_warning,
            "systemMessage": context_warning,
        },
    }))


if __name__ == "__main__":
    main()
