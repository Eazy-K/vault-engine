#!/usr/bin/env python3
"""Warn when this vault's Codex request context exceeds 150K tokens."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

THRESHOLD = 150_000
MAX_THRESHOLD_OVERRIDE = 1_000_000
GROWTH_STEP = 10_000


def _threshold() -> int:
    """Read a one-process test override; invalid values keep the production default."""
    raw = os.environ.get("CODEX_CONTEXT_WARN_THRESHOLD")
    if raw is None:
        return THRESHOLD
    value = raw.strip()
    if not value or not value.isascii() or not value.isdigit() or len(value) > 7:
        return THRESHOLD
    parsed = int(value)
    if not 1 <= parsed <= MAX_THRESHOLD_OVERRIDE:
        return THRESHOLD
    return parsed


def _rollout_path(payload: dict) -> Path | None:
    raw = payload.get("transcript_path")
    if isinstance(raw, str) and raw:
        path = Path(raw)
        if path.is_file():
            return path

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    root = Path.home() / ".codex" / "sessions"
    try:
        return next(root.rglob(f"rollout-*-{session_id}.jsonl"), None)
    except (OSError, ValueError):
        return None


def _latest_context(path: Path) -> tuple[int, int | None] | None:
    latest = None
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if '"token_count"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                usage = info.get("last_token_usage")
                if not isinstance(usage, dict):
                    continue
                tokens = usage.get("input_tokens")
                if type(tokens) is not int or tokens < 0:
                    continue
                window = info.get("model_context_window")
                window = window if type(window) is int and window > 0 else None
                latest = tokens, window
    except (OSError, UnicodeError):
        return None
    return latest


def _state_path(payload: dict, rollout: Path) -> Path:
    key = payload.get("session_id") or str(rollout)
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:24]
    base = Path(os.environ.get("CODEX_CONTEXT_WARN_STATE_DIR") or tempfile.gettempdir())
    return base / f"codex-context-warn-{digest}.json"


def _last_warning(path: Path) -> int:
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("last_warned", 0)
        return value if type(value) is int and value >= 0 else 0
    except (OSError, ValueError, AttributeError):
        return 0


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    rollout = _rollout_path(payload)
    if rollout is None:
        return
    context = _latest_context(rollout)
    if context is None:
        return
    tokens, window = context
    state = _state_path(payload, rollout)
    threshold = _threshold()
    if tokens < threshold:
        try:
            state.unlink(missing_ok=True)
        except OSError:
            pass
        return
    last_warned = _last_warning(state)
    if tokens - last_warned < GROWTH_STEP:
        return
    detail = f"{tokens:,} token"
    if window:
        detail += f" / {window:,} ({tokens / window:.0%})"
    message = f"[context-warn] Codex bağlamı yaklaşık {detail}. Uygun iş sınırında /compact veya yeni oturum düşün."
    print(json.dumps({
        "systemMessage": message,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": message,
        },
    }, ensure_ascii=False))
    try:
        state.write_text(json.dumps({"last_warned": tokens}), encoding="utf-8")
    except OSError:
        pass


if __name__ == "__main__":
    main()
