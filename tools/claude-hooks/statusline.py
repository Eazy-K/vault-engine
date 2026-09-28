#!/usr/bin/env python3
"""statusLine command for Claude Code: prints a short one-line status,
e.g. "Opus 5.5·med │ vault-engine (fix/windows-hook-quoting) │ ctx 80K/1000K 8% │ $1.42".

Reads the JSON session object Claude Code sends on stdin (see
https://code.claude.com/docs/en/statusline) and prints up to four
segments, joined by " │ ", omitting any segment whose data is missing:

1. Model name and reasoning effort (model.display_name). Effort is read
   from the stdin JSON's `effort.level` field when present (Claude Code
   >= the version that added it); otherwise it falls back to
   ~/.claude/settings.json's `modelSettings[<model.id>].effortLevel` or
   top-level `effortLevel`. If no effort can be determined, only the
   model name is shown.
2. Folder (branch): basename of workspace.current_dir (falling back to
   top-level cwd), plus the current git branch in parentheses when the
   directory is inside a git repository.
3. Context window usage: context_window.current_usage (input_tokens +
   cache_creation_input_tokens + cache_read_input_tokens +
   output_tokens), context_window.context_window_size and
   context_window.used_percentage. `current_usage`/`used_percentage` are
   null before the first model call in a session and right after
   /compact, so that case is reported as "no data yet" rather than as an
   error.
4. Session cost: cost.total_cost_usd formatted as "$1.42".

Any parse error or missing field is handled gracefully -- never a
traceback, never a non-zero exit -- so a bug here can never break the
status line.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

# Overridable so tests never read the real ~/.claude/settings.json.
SETTINGS_ENV_VAR = "CLAUDE_STATUSLINE_SETTINGS"

EFFORT_ABBREV = {
    "low": "low",
    "medium": "med",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}


def context_tokens(usage: dict) -> int:
    return (
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("output_tokens") or 0)
    )


def ctx_segment(data: dict) -> str:
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


def _settings_path(settings_path: str | None = None) -> str:
    if settings_path is not None:
        return settings_path
    env_path = os.environ.get(SETTINGS_ENV_VAR)
    if env_path:
        return env_path
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def effort_from_settings(model_id: str | None, settings_path: str | None = None) -> str | None:
    """Look up an effort level from a settings.json file.

    Checks modelSettings[<model_id>].effortLevel first, then the
    top-level effortLevel field. Returns None if the file is missing,
    unreadable, or has no matching field.
    """
    path = _settings_path(settings_path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except Exception:
        return None
    if not isinstance(settings, dict):
        return None

    if model_id:
        model_settings = settings.get("modelSettings")
        if isinstance(model_settings, dict):
            entry = model_settings.get(model_id)
            if isinstance(entry, dict):
                level = entry.get("effortLevel")
                if isinstance(level, str) and level:
                    return level

    level = settings.get("effortLevel")
    if isinstance(level, str) and level:
        return level
    return None


def model_segment(data: dict, settings_path: str | None = None) -> str | None:
    model = data.get("model")
    if not isinstance(model, dict):
        return None
    name = model.get("display_name")
    if not isinstance(name, str) or not name:
        return None

    effort = None
    effort_data = data.get("effort")
    if isinstance(effort_data, dict):
        level = effort_data.get("level")
        if isinstance(level, str) and level:
            effort = level
    if not effort:
        effort = effort_from_settings(model.get("id"), settings_path)

    if effort:
        abbrev = EFFORT_ABBREV.get(effort, effort)
        return f"{name}·{abbrev}"
    return name


def git_branch(directory: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", directory, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def folder_segment(data: dict) -> str | None:
    workspace = data.get("workspace")
    directory = None
    if isinstance(workspace, dict):
        directory = workspace.get("current_dir")
    if not isinstance(directory, str) or not directory:
        cwd = data.get("cwd")
        directory = cwd if isinstance(cwd, str) and cwd else None
    if not directory:
        return None

    name = os.path.basename(directory.rstrip("/\\")) or directory
    branch = git_branch(directory)
    if branch:
        return f"{name} ({branch})"
    return name


def cost_segment(data: dict) -> str | None:
    cost = data.get("cost")
    if not isinstance(cost, dict):
        return None
    total = cost.get("total_cost_usd")
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return None
    return f"${total:.2f}"


def format_line(data: dict, settings_path: str | None = None) -> str:
    segments = [
        model_segment(data, settings_path),
        folder_segment(data),
        ctx_segment(data),
        cost_segment(data),
    ]
    return " │ ".join(s for s in segments if s)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

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
