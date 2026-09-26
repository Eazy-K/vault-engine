#!/usr/bin/env python3
"""statusLine command for Claude Code: prints a short context-usage indicator,
e.g. "ctx 42K/200K 21%".

Reads the JSON session object Claude Code sends on stdin (see
https://code.claude.com/docs/en/statusline) and reports
context_window.current_usage (input_tokens + cache_creation_input_tokens +
cache_read_input_tokens + output_tokens), context_window.context_window_size
and context_window.used_percentage. `current_usage`/`used_percentage` are
null before the first model call in a session and right after /compact, so
that case is reported as "no data yet" rather than as an error.

Any parse error or missing field falls back to a short "ctx ??" line --
never a traceback, never a non-zero exit -- so a bug here can never break
the status line.
"""
import json
import sys


def context_tokens(usage: dict) -> int:
    return (
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("output_tokens") or 0)
    )


def format_line(data: dict) -> str:
    cw = data.get("context_window") or {}
    usage = cw.get("current_usage")
    size = cw.get("context_window_size")
    pct = cw.get("used_percentage")

    if not isinstance(usage, dict):
        return "ctx --K (no data yet)"

    k = context_tokens(usage) / 1000.0
    size_str = f"/{size // 1000}K" if isinstance(size, (int, float)) and size else ""
    pct_str = f" {pct:.0f}%" if isinstance(pct, (int, float)) else ""
    return f"ctx {k:.0f}K{size_str}{pct_str}"


def main() -> None:
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        print("ctx ?? (bad input)")
        return

    try:
        print(format_line(data))
    except Exception:
        print("ctx ??")


if __name__ == "__main__":
    main()
