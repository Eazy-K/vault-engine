"""Install the engine's Claude Code hooks (currently: the agent-guard PreToolUse
hook that stops the orchestrator from starting expensive subagents) into a
Claude Code settings.json.

Optional extension module: graph.py imports this if present (see EXTENSIONS
in graph.py) and calls register(sub) with its argparse subparsers object.
Stdlib only, like graph.py itself.
"""
from __future__ import annotations

import argparse
import datetime
import json
import shlex
import sys
from pathlib import Path

import graph as g

DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"
GUARD_SCRIPT = g.ENGINE / "tools" / "claude-hooks" / "agent-guard.py"
GUARD_MATCHER = "Agent"
GUARD_MARKER = "agent-guard.py"  # substring identifying our hook's command, for idempotent merges


def _guard_command() -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(GUARD_SCRIPT))}"


def _load(path: Path) -> dict:
    """Same shape as onboarding._load_existing_config: {} if missing or not a JSON object."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _find_guard_hook(settings: dict) -> dict | None:
    """The hook-handler dict (the {"type": "command", "command": ...} entry) already
    installed for our guard, if any -- wherever it lives among PreToolUse entries."""
    pre = settings.get("hooks", {}).get("PreToolUse", [])
    if not isinstance(pre, list):
        return None
    for entry in pre:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []) if isinstance(entry.get("hooks"), list) else []:
            if isinstance(hook, dict) and GUARD_MARKER in str(hook.get("command", "")):
                return hook
    return None


def merge(settings: dict) -> tuple[dict, bool]:
    """Merge in one PreToolUse hook for the Agent matcher, without touching any other
    existing hook. Returns (new_settings, changed)."""
    command = _guard_command()
    existing = _find_guard_hook(settings)
    if existing is not None:
        if existing.get("command") == command and existing.get("type") == "command":
            return settings, False
        existing["type"] = "command"
        existing["command"] = command
        return settings, True

    hooks = settings.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])
    pre.append({"matcher": GUARD_MATCHER, "hooks": [{"type": "command", "command": command}]})
    return settings, True


def status(settings_path: Path = DEFAULT_SETTINGS) -> tuple[str, str]:
    """("OK"|"WARN", message) for `doctor`."""
    settings = _load(settings_path)
    hook = _find_guard_hook(settings)
    if hook is not None and hook.get("command") == _guard_command():
        return "OK", f"agent-guard hook installed ({settings_path})"
    return "WARN", (f"agent-guard hook not installed in {settings_path}: run "
                     "`graph.py claude-hooks --install` to stop expensive subagents")


def cmd_claude_hooks(args: argparse.Namespace) -> None:
    settings_path = Path(args.settings).expanduser() if args.settings else DEFAULT_SETTINGS
    settings = _load(settings_path)
    merged, changed = merge(settings)

    if not args.install:
        if not changed:
            print(f"up to date: {settings_path}")
        else:
            print(f"would update: {settings_path}")
            print(f"  matcher: {GUARD_MATCHER}")
            print(f"  command: {_guard_command()}")
            print("(dry run: pass --install to write it)")
        return

    if not changed:
        print(f"up to date: {settings_path}")
        return

    if settings_path.exists():
        backup = settings_path.with_name(
            settings_path.name + "." + "bak-" + datetime.date.today().strftime("%Y%m%d"))
        n = 1
        while backup.exists():
            n += 1
            backup = settings_path.with_name(
                settings_path.name + f".bak-{datetime.date.today().strftime('%Y%m%d')}-{n}")
        backup.write_bytes(settings_path.read_bytes())
        print(f"  backup: {backup}")

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8", newline="\n")
    print(f"  installed: {settings_path}")
    print(f"  matcher: {GUARD_MATCHER}")
    print(f"  command: {_guard_command()}")


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("claude-hooks", help="install the agent-guard PreToolUse hook "
                                             "(blocks expensive subagents) into Claude "
                                             "Code's settings.json")
    p.add_argument("--install", action="store_true",
                    help="write the merged settings (default: dry run, print the diff)")
    p.add_argument("--settings", help=f"settings.json path (default: {DEFAULT_SETTINGS})")
    p.set_defaults(func=cmd_claude_hooks)
