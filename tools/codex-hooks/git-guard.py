#!/usr/bin/env python3
"""PreToolUse guard for Codex shell calls: denies a force `git push` and any
`--no-verify` / `git commit -n` bypass. Same rules as the Claude hook; the logic
lives in tools/claude-hooks/git-guard.py (`decide`) and is loaded from there.

Fails open (no output, exit 0) on any error; `VAULT_GIT_GUARD=off` disables it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

SHELL_TOOLS = ("Bash", "shell", "shell_command", "local_shell", "exec_command")
SHARED = Path(__file__).resolve().parent.parent / "claude-hooks" / "git-guard.py"


def _shared():
    spec = importlib.util.spec_from_file_location("vault_git_guard", SHARED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    try:
        if os.environ.get("VAULT_GIT_GUARD") == "off":
            return
        shared = _shared()
        command = shared.command_of(json.load(sys.stdin), SHELL_TOOLS)
        reason = shared.decide(command) if command else None
        if reason:
            print(shared.deny_json(reason))
    except Exception:
        return


if __name__ == "__main__":
    main()
