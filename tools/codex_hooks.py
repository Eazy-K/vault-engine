"""Install Codex hooks and worker profiles into the user's Codex home.

The adapter owns only the Codex-specific hooks.json and worker TOMLs. It never
edits config.toml, and customized worker files are kept. Optional path arguments
exist so tests can use temporary directories without touching a real Codex home.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import graph as g

CODEX_HOME = Path.home() / ".codex"
DEFAULT_HOOKS = CODEX_HOME / "hooks.json"
DEFAULT_CONFIG = CODEX_HOME / "config.toml"
DEFAULT_AGENTS = CODEX_HOME / "agents"
HOOKS_DIR = g.ENGINE / "tools" / "codex-hooks"
AGENTS_DIR = g.ENGINE / "tools" / "codex-agents"
HOOKS = {
    "UserPromptSubmit": ("context-warn.py", None),
    "PreToolUse": ("agent-guard.py", "^(Agent|spawn_agent)$"),
    "PostToolUse": ("delegation-warn.py", "^(Bash|Read|Edit|Write|apply_patch)$"),
}
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
_SAFE_ARG = re.compile(r"[A-Za-z0-9_./:~+-]+")


def _command(script: Path) -> str:
    executable, target = str(Path(sys.executable).resolve()), str(script.resolve())
    if sys.platform == "win32":
        # Windows shells accept the CommandLineToArgvW quoting produced here.
        return subprocess.list2cmdline([executable, target])
    return f"{shlex.quote(executable)} {shlex.quote(target)}"


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _upsert_hook(settings: dict, event: str, filename: str, matcher: str | None) -> bool:
    """Update our existing command in place, or append it while preserving others."""
    command = _command(HOOKS_DIR / filename)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks.json 'hooks' member must be an object")
    entries = hooks.setdefault(event, [])
    if not isinstance(entries, list):
        raise ValueError(f"hooks.json {event!r} member must be an array")
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            continue
        for handler in entry["hooks"]:
            if isinstance(handler, dict) and filename in str(handler.get("command", "")):
                changed = handler.get("type") != "command" or handler.get("command") != command
                handler["type"] = "command"
                handler["command"] = command
                if matcher is not None and entry.get("matcher") != matcher:
                    entry["matcher"] = matcher
                    changed = True
                return changed
    entry = {"hooks": [{"type": "command", "command": command}]}
    if matcher is not None:
        entry["matcher"] = matcher
    entries.append(entry)
    return True


def merge(settings: dict) -> tuple[dict, bool]:
    """Return settings with Codex hooks installed, preserving unrelated entries."""
    changed = False
    for event, (filename, matcher) in HOOKS.items():
        changed = _upsert_hook(settings, event, filename, matcher) or changed
    return settings, changed


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.date.today().strftime("%Y%m%d")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{suffix}")
    shutil.copy2(path, candidate)
    return candidate


def _install_agents(agents_dir: Path) -> tuple[list[Path], list[Path]]:
    installed, skipped = [], []
    agents_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted(AGENTS_DIR.glob("worker-*.toml")):
        target = agents_dir / source.name
        content = source.read_bytes()
        if target.exists():
            current = target.read_bytes()
            if current.replace(b"\r\n", b"\n") == content.replace(b"\r\n", b"\n"):
                continue
            # Do not replace locally edited profiles: the user may have customized them.
            skipped.append(target)
            continue
        target.write_bytes(content)
        installed.append(target)
    return installed, skipped


def cmd_codex_hooks(args: argparse.Namespace) -> None:
    hooks_path = Path(args.hooks).expanduser() if args.hooks else DEFAULT_HOOKS
    agents_dir = Path(args.agents_dir).expanduser() if args.agents_dir else DEFAULT_AGENTS
    settings = _load_json(hooks_path)
    if settings is None:
        sys.exit(f"codex-hooks: refusing to replace invalid JSON in {hooks_path}")
    merged, hooks_changed = merge(settings)
    agent_sources = sorted(AGENTS_DIR.glob("worker-*.toml"))
    missing_agents = [p for p in agent_sources if not (agents_dir / p.name).exists()]
    custom_agents = [p for p in agent_sources if (agents_dir / p.name).exists()
                     and (agents_dir / p.name).read_bytes().replace(b"\r\n", b"\n")
                     != p.read_bytes().replace(b"\r\n", b"\n")]

    if not args.install:
        if not hooks_changed and not missing_agents and not custom_agents:
            print(f"up to date: {hooks_path}; worker profiles: {agents_dir}")
        else:
            print(f"would update: {hooks_path}" if hooks_changed else f"hooks up to date: {hooks_path}")
            if missing_agents:
                print("  install workers: " + ", ".join(p.name for p in missing_agents))
            if custom_agents:
                print("  keep customized workers: " + ", ".join(p.name for p in custom_agents))
            print("(dry run: pass --install to write missing or managed files)")
        return

    if hooks_changed:
        backup = _backup(hooks_path)
        if backup:
            print(f"  backup: {backup}")
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        hooks_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8", newline="\n")
        print(f"  installed hooks: {hooks_path}")
    else:
        print(f"  hooks up to date: {hooks_path}")
    installed, skipped = _install_agents(agents_dir)
    for path in installed:
        print(f"  installed worker: {path}")
    for path in skipped:
        print(f"  kept customized worker: {path}")


def hooks_status(hooks_path: Path | None = None) -> tuple[str, str]:
    hooks_path = hooks_path or (Path.home() / ".codex" / "hooks.json")
    settings = _load_json(hooks_path)
    if settings is None:
        return "WARN", f"Codex hooks.json is not valid JSON ({hooks_path})"
    expected = merge({})[0]["hooks"]
    actual = settings.get("hooks", {})
    for event, (filename, _matcher) in HOOKS.items():
        expected_command = expected[event][0]["hooks"][0]["command"]
        entries = actual.get(event, []) if isinstance(actual, dict) else []
        found = any(filename in str(handler.get("command", ""))
                    and handler.get("command") == expected_command
                    and (expected[event][0].get("matcher") is None
                         or entry.get("matcher") == expected[event][0]["matcher"])
                    for entry in entries if isinstance(entry, dict)
                    for handler in entry.get("hooks", []) if isinstance(handler, dict))
        if not found:
            return "WARN", (f"Codex {filename} hook missing or stale in {hooks_path}; "
                             "run `graph.py codex-hooks --install`")
    return "OK", f"Codex hooks installed ({hooks_path})"


def _root_toml_values(path: Path) -> dict[str, str]:
    """Read string-valued root TOML settings without a third-party dependency."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return values
    for line in lines:
        if line.lstrip().startswith("["):
            break
        match = re.match(r"^\s*(model|model_reasoning_effort)\s*=\s*(['\"])(.*?)\2\s*(?:#.*)?$", line)
        if match:
            values[match.group(1)] = match.group(3)
    return values


def model_status(config_path: Path | None = None) -> tuple[str, str]:
    """Check that Codex has explicit, syntactically valid model and effort values.

    The engine has no authoritative per-user preferred Codex model, so this check
    cannot decide whether a valid model is current or the user's chosen one.
    """
    config_path = config_path or (Path.home() / ".codex" / "config.toml")
    values = _root_toml_values(config_path)
    model, effort = values.get("model"), values.get("model_reasoning_effort")
    if not model or not effort or effort not in EFFORTS:
        return "WARN", (f"Codex model/reasoning effort is missing or invalid in {config_path}; "
                         "review the top-level model and model_reasoning_effort settings")
    return "OK", (f"Codex model and reasoning effort are explicitly configured "
                   f"({model}, {effort}); preferred-model drift is not checked")


def workers_status(agents_dir: Path | None = None) -> tuple[str, str]:
    agents_dir = agents_dir or (Path.home() / ".codex" / "agents")
    expected = sorted(AGENTS_DIR.glob("worker-*.toml"))
    if not expected:
        return "OK", "no Codex worker profiles are shipped"
    stale = []
    for source in expected:
        target = agents_dir / source.name
        if not target.exists():
            stale.append(f"{source.name} missing")
        else:
            current = target.read_bytes().replace(b"\r\n", b"\n")
            shipped = source.read_bytes().replace(b"\r\n", b"\n")
            if current != shipped:
                stale.append(f"{source.name} differs")
    if stale:
        return "WARN", (f"Codex worker profiles missing or customized in {agents_dir}: "
                         f"{', '.join(stale)}; review against {AGENTS_DIR}")
    return "OK", f"Codex worker profiles match engine templates ({agents_dir})"


def statuses(hooks_path: Path | None = None, config_path: Path | None = None,
             agents_dir: Path | None = None) -> list[tuple[str, str]]:
    return [hooks_status(hooks_path), model_status(config_path), workers_status(agents_dir)]


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("codex-hooks", help="install Codex context/delegation hooks and worker profiles")
    parser.add_argument("--install", action="store_true",
                        help="write hooks.json and missing worker profiles (default: dry run)")
    parser.add_argument("--hooks", help=f"hooks.json path (default: {DEFAULT_HOOKS})")
    parser.add_argument("--agents-dir", help=f"worker directory (default: {DEFAULT_AGENTS})")
    parser.set_defaults(func=cmd_codex_hooks)
