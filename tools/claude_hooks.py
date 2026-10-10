"""Install the engine's Claude Code hooks into a Claude Code settings.json:
- agent-guard: a PreToolUse hook that stops the orchestrator from starting
  expensive subagents.
- context-warn: a UserPromptSubmit hook that warns the model when the
  session's context usage crosses a threshold, and reminds it on every user
  message to delegate multi-step work to a worker subagent.
- delegation-warn: a PostToolUse hook that additionally warns once a user
  message has driven several tool calls on the orchestrator's own thread,
  instead of delegating them.
- reinforce-check: a Stop hook that asks the agent to run `reinforce` once a
  task looks finished (a commit, push or PR in the turn) and `context` gave a
  task id that was never reinforced.
- statusLine: a command that shows context usage in the status line (only
  set if settings has no statusLine yet -- an existing one is left alone).

Optional extension module: graph.py imports this if present (see EXTENSIONS
in graph.py) and calls register(sub) with its argparse subparsers object.
Stdlib only, like graph.py itself.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import shlex
import sys
from pathlib import Path

import claude_dirs
import graph as g



def default_settings() -> Path:
    """settings.json in the active Claude config dir ($CLAUDE_CONFIG_DIR or ~/.claude)."""
    return g.claude_config_dir() / "settings.json"


GUARD_SCRIPT = g.ENGINE / "tools" / "claude-hooks" / "agent-guard.py"
GUARD_MATCHER = "Agent"
GUARD_MARKER = "agent-guard.py"  # substring identifying our hook's command, for idempotent merges

CONTEXT_WARN_SCRIPT = g.ENGINE / "tools" / "claude-hooks" / "context-warn.py"
CONTEXT_WARN_MARKER = "context-warn.py"

DELEGATION_WARN_SCRIPT = g.ENGINE / "tools" / "claude-hooks" / "delegation-warn.py"
DELEGATION_WARN_MATCHER = "Bash|Read|Edit|Write"
DELEGATION_WARN_MARKER = "delegation-warn.py"

REINFORCE_CHECK_SCRIPT = g.ENGINE / "tools" / "claude-hooks" / "reinforce-check.py"
REINFORCE_CHECK_MARKER = "reinforce-check.py"

STATUSLINE_SCRIPT = g.ENGINE / "tools" / "claude-hooks" / "statusline.py"
STATUSLINE_MARKER = "statusline.py"

_SAFE_ARG = re.compile(r"[A-Za-z0-9_./:~+-]+")


def _shell_arg(path: str) -> str:
    """One argument that bash, PowerShell and cmd all parse the same way. Claude Code
    may run hook commands in any of them on Windows, and POSIX quoting breaks there:
    PowerShell reads `'a.exe' 'b.py'` as two string literals, a parse error, so the
    hook silently never runs. Windows paths get forward slashes (every shell and
    Windows itself accept them) and stay unquoted; one with spaces or other special
    characters falls back to its 8.3 short name, which has none."""
    if sys.platform != "win32":
        return shlex.quote(path)
    arg = path.replace("\\", "/")
    if _SAFE_ARG.fullmatch(arg):
        return arg
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf)):
            short = buf.value.replace("\\", "/")
            if _SAFE_ARG.fullmatch(short):
                return short
    except (AttributeError, OSError):
        pass
    return f'"{arg}"'  # no short name: bash and cmd still parse this, PowerShell does not


def _command(script: Path) -> str:
    return f"{_shell_arg(sys.executable)} {_shell_arg(str(script))}"


def _guard_command() -> str:
    return _command(GUARD_SCRIPT)


def _context_warn_command() -> str:
    return _command(CONTEXT_WARN_SCRIPT)


def _delegation_warn_command() -> str:
    return _command(DELEGATION_WARN_SCRIPT)


def _reinforce_check_command() -> str:
    return _command(REINFORCE_CHECK_SCRIPT)


def _statusline_command() -> str:
    return _command(STATUSLINE_SCRIPT)


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


def _find_context_warn_hook(settings: dict) -> dict | None:
    """Same idea as _find_guard_hook, but for the UserPromptSubmit hook list."""
    entries = settings.get("hooks", {}).get("UserPromptSubmit", [])
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []) if isinstance(entry.get("hooks"), list) else []:
            if isinstance(hook, dict) and CONTEXT_WARN_MARKER in str(hook.get("command", "")):
                return hook
    return None


def _find_delegation_warn_hook(settings: dict) -> dict | None:
    """Same idea as _find_guard_hook, but for the PostToolUse hook list."""
    entries = settings.get("hooks", {}).get("PostToolUse", [])
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []) if isinstance(entry.get("hooks"), list) else []:
            if isinstance(hook, dict) and DELEGATION_WARN_MARKER in str(hook.get("command", "")):
                return hook
    return None


def _find_reinforce_check_hook(settings: dict) -> dict | None:
    """Same idea as _find_guard_hook, but for the Stop hook list."""
    entries = settings.get("hooks", {}).get("Stop", [])
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []) if isinstance(entry.get("hooks"), list) else []:
            if isinstance(hook, dict) and REINFORCE_CHECK_MARKER in str(hook.get("command", "")):
                return hook
    return None


def _merge_guard(settings: dict) -> bool:
    """Merge in the agent-guard PreToolUse hook. Returns True if it changed anything."""
    command = _guard_command()
    existing = _find_guard_hook(settings)
    if existing is not None:
        if existing.get("command") == command and existing.get("type") == "command":
            return False
        existing["type"] = "command"
        existing["command"] = command
        return True

    hooks = settings.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])
    pre.append({"matcher": GUARD_MATCHER, "hooks": [{"type": "command", "command": command}]})
    return True


def _merge_context_warn(settings: dict) -> bool:
    """Merge in the context-warn UserPromptSubmit hook. Returns True if changed."""
    command = _context_warn_command()
    existing = _find_context_warn_hook(settings)
    if existing is not None:
        if existing.get("command") == command and existing.get("type") == "command":
            return False
        existing["type"] = "command"
        existing["command"] = command
        return True

    hooks = settings.setdefault("hooks", {})
    entries = hooks.setdefault("UserPromptSubmit", [])
    entries.append({"hooks": [{"type": "command", "command": command}]})
    return True


def _merge_delegation_warn(settings: dict) -> bool:
    """Merge in the delegation-warn PostToolUse hook. Returns True if changed."""
    command = _delegation_warn_command()
    existing = _find_delegation_warn_hook(settings)
    if existing is not None:
        if existing.get("command") == command and existing.get("type") == "command":
            return False
        existing["type"] = "command"
        existing["command"] = command
        return True

    hooks = settings.setdefault("hooks", {})
    post = hooks.setdefault("PostToolUse", [])
    post.append({"matcher": DELEGATION_WARN_MATCHER,
                 "hooks": [{"type": "command", "command": command}]})
    return True


def _merge_reinforce_check(settings: dict) -> bool:
    """Merge in the reinforce-check Stop hook. Returns True if changed."""
    command = _reinforce_check_command()
    existing = _find_reinforce_check_hook(settings)
    if existing is not None:
        if existing.get("command") == command and existing.get("type") == "command":
            return False
        existing["type"] = "command"
        existing["command"] = command
        return True

    hooks = settings.setdefault("hooks", {})
    entries = hooks.setdefault("Stop", [])
    entries.append({"hooks": [{"type": "command", "command": command}]})
    return True


def _merge_statusline(settings: dict) -> bool:
    """Set statusLine to our command if none is configured yet, or refresh it if it
    is ours (a stale path or quoting). Returns True if changed; a statusLine that is
    not ours is left untouched."""
    current = settings.get("statusLine")
    command = _statusline_command()
    if current:
        if not (isinstance(current, dict)
                and STATUSLINE_MARKER in str(current.get("command", ""))):
            return False
        if current.get("command") == command and current.get("type") == "command":
            return False
    settings["statusLine"] = {"type": "command", "command": command}
    return True


def merge(settings: dict) -> tuple[dict, bool]:
    """Merge in the agent-guard, context-warn, delegation-warn and reinforce-check hooks, and (if none
    is configured) the statusLine command, without touching any other existing hook
    or an existing statusLine. Returns (new_settings, changed)."""
    changed = _merge_guard(settings)
    changed = _merge_context_warn(settings) or changed
    changed = _merge_delegation_warn(settings) or changed
    changed = _merge_reinforce_check(settings) or changed
    changed = _merge_statusline(settings) or changed
    return settings, changed


def status(settings_path: Path | None = None) -> tuple[str, str]:
    """("OK"|"WARN", message) for `doctor`: whether the agent-guard hook is installed."""
    settings_path = settings_path or default_settings()
    settings = _load(settings_path)
    hook = _find_guard_hook(settings)
    if hook is not None and hook.get("command") == _guard_command():
        return "OK", f"agent-guard hook installed ({settings_path})"
    return "WARN", (f"agent-guard hook not installed in {settings_path}: run "
                     f"`{g.update_all_command()}` (or `graph.py claude-hooks --install`) to stop expensive subagents")


def context_warn_status(settings_path: Path | None = None) -> tuple[str, str]:
    """("OK"|"WARN", message) for `doctor`: whether the context-warn hook is installed."""
    settings_path = settings_path or default_settings()
    settings = _load(settings_path)
    hook = _find_context_warn_hook(settings)
    if hook is not None and hook.get("command") == _context_warn_command():
        return "OK", f"context-warn hook installed ({settings_path})"
    return "WARN", (f"context-warn hook not installed in {settings_path}: run "
                     f"`{g.update_all_command()}` (or `graph.py claude-hooks --install`) to warn before context runs out")


def delegation_warn_status(settings_path: Path | None = None) -> tuple[str, str]:
    """("OK"|"WARN", message) for `doctor`: whether the delegation-warn hook is
    installed."""
    settings_path = settings_path or default_settings()
    settings = _load(settings_path)
    hook = _find_delegation_warn_hook(settings)
    if hook is not None and hook.get("command") == _delegation_warn_command():
        return "OK", f"delegation-warn hook installed ({settings_path})"
    return "WARN", (f"delegation-warn hook not installed in {settings_path}: run "
                     f"`{g.update_all_command()}` (or `graph.py claude-hooks --install`) to nudge delegation of "
                     "multi-step work")


def reinforce_check_status(settings_path: Path | None = None) -> tuple[str, str]:
    """("OK"|"WARN", message) for `doctor`: whether the reinforce-check Stop hook is
    installed."""
    settings_path = settings_path or default_settings()
    settings = _load(settings_path)
    hook = _find_reinforce_check_hook(settings)
    if hook is not None and hook.get("command") == _reinforce_check_command():
        return "OK", f"reinforce-check hook installed ({settings_path})"
    return "WARN", (f"reinforce-check hook not installed in {settings_path}: run "
                     f"`{g.update_all_command()}` (or `graph.py claude-hooks --install`) to remind "
                     "the agent to run reinforce when a task is finished")


def cmd_claude_hooks(args: argparse.Namespace) -> None:
    if args.settings:
        _apply_to(Path(args.settings).expanduser(), args)
        return
    for claude_dir in claude_dirs.targets(prompt=args.install):
        _apply_to(claude_dir / "settings.json", args)


def _apply_to(settings_path: Path, args: argparse.Namespace) -> None:
    settings = _load(settings_path)
    current = settings.get("statusLine")
    had_statusline = bool(current) and not (
        isinstance(current, dict) and STATUSLINE_MARKER in str(current.get("command", "")))
    merged, changed = merge(settings)

    if not args.install:
        if not changed:
            print(f"up to date: {settings_path}")
        else:
            print(f"would update: {settings_path}")
            print(f"  matcher: {GUARD_MATCHER}  command: {_guard_command()}")
            print(f"  matcher: UserPromptSubmit  command: {_context_warn_command()}")
            print(f"  matcher: {DELEGATION_WARN_MATCHER}  "
                  f"command: {_delegation_warn_command()}")
            print(f"  matcher: Stop  command: {_reinforce_check_command()}")
            if not had_statusline:
                print(f"  statusLine: {_statusline_command()}")
            print("(dry run: pass --install to write it)")
        if had_statusline:
            print(f"note: statusLine already set in {settings_path}, leaving it alone")
        return

    if not changed:
        print(f"up to date: {settings_path}")
        if had_statusline:
            print(f"note: statusLine already set in {settings_path}, leaving it alone")
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
    print(f"  matcher: {GUARD_MATCHER}  command: {_guard_command()}")
    print(f"  matcher: UserPromptSubmit  command: {_context_warn_command()}")
    print(f"  matcher: {DELEGATION_WARN_MATCHER}  command: {_delegation_warn_command()}")
    print(f"  matcher: Stop  command: {_reinforce_check_command()}")
    if not had_statusline:
        print(f"  statusLine: {_statusline_command()}")
    else:
        print(f"note: statusLine already set in {settings_path}, leaving it alone")


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("claude-hooks", help="install the agent-guard, context-warn, "
                                             "delegation-warn, reinforce-check hooks and a statusLine "
                                             "into Claude Code's settings.json")
    p.add_argument("--install", action="store_true",
                    help="write the merged settings (default: dry run, print the diff)")
    p.add_argument("--settings", help="settings.json path (default: settings.json in every "
                                       "registered Claude config dir, see `claude-dirs`; "
                                       "~/.claude or $CLAUDE_CONFIG_DIR)")
    p.set_defaults(func=cmd_claude_hooks)
