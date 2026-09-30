"""Install Codex hooks and worker profiles into the user's Codex home.

The adapter owns only the Codex-specific hooks.json and worker TOMLs. The Codex
home is $CODEX_HOME (default ~/.codex), as in user_config. It never edits
config.toml, and customized worker files are kept. Optional path arguments
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
import tempfile
from pathlib import Path

import graph as g
from user_config import codex_home

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
    if getattr(args, "probe_deny", False):
        probe_deny()
        return
    hooks_path = Path(args.hooks).expanduser() if args.hooks else codex_home() / "hooks.json"
    agents_dir = Path(args.agents_dir).expanduser() if args.agents_dir else codex_home() / "agents"
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
    hooks_path = hooks_path or (codex_home() / "hooks.json")
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
    config_path = config_path or (codex_home() / "config.toml")
    values = _root_toml_values(config_path)
    model, effort = values.get("model"), values.get("model_reasoning_effort")
    if not model or not effort or effort not in EFFORTS:
        return "WARN", (f"Codex model/reasoning effort is missing or invalid in {config_path}; "
                         "review the top-level model and model_reasoning_effort settings")
    return "OK", (f"Codex model and reasoning effort are explicitly configured "
                   f"({model}, {effort}); preferred-model drift is not checked")


def workers_status(agents_dir: Path | None = None) -> tuple[str, str]:
    agents_dir = agents_dir or (codex_home() / "agents")
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


# --- PreToolUse deny probe ---------------------------------------------------------------

TESTED_KEY = "codex_deny_tested_version"  # in <data>/.graph/machine.json (gitignored)
PROBE_TIMEOUT = 240
PROBE_PROMPT = "create probe.txt with content x"
PROBE_HOOK = (
    "import json\n"
    "from pathlib import Path\n"
    'Path(__file__).with_name("fired.log").write_text("fired", encoding="utf-8")\n'
    'print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", '
    '"permissionDecision": "deny", "permissionDecisionReason": "vault-engine deny probe"}}))\n'
)
RETEST_HINT = ("Codex CLI changed; PreToolUse deny enforcement is unverified on this version "
               "- run codex-hooks --probe-deny")


def codex_version(executable: str = "codex") -> str | None:
    """Output of `codex --version`, or None when it cannot be run."""
    try:
        out = subprocess.run([executable, "--version"], capture_output=True, text=True,
                             encoding="utf-8", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or "").strip()
    return text if out.returncode == 0 and text else None


def _machine_file(data: Path | None = None) -> Path | None:
    try:
        data = data or g.resolve_data_dir()
    except SystemExit:
        return None
    return data / ".graph" / "machine.json"


def _load_machine(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def tested_version(data: Path | None = None) -> str | None:
    path = _machine_file(data)
    value = _load_machine(path).get(TESTED_KEY) if path else None
    return value if isinstance(value, str) and value else None


def record_tested_version(version: str, data: Path | None = None) -> bool:
    path = _machine_file(data)
    if path is None or not path.parent.parent.is_dir():
        return False
    machine = _load_machine(path)
    machine[TESTED_KEY] = version
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(machine, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    return True


def deny_retest_status(executable: str = "codex", data: Path | None = None) -> tuple[str, str] | None:
    """INFO when the installed Codex CLI differs from the version the deny probe last
    ran on (or no probe was recorded); None when it matches or codex cannot run."""
    version = codex_version(executable)
    if version is None or tested_version(data) == version:
        return None
    return "INFO", RETEST_HINT


def probe_deny(executable: str | None = None, data: Path | None = None,
               timeout: int = PROBE_TIMEOUT) -> str:
    """Run one `codex exec` in a throwaway git dir whose project .codex/hooks.json denies
    file writes; report whether the hook fired and whether the write was blocked.
    Never touches the real Codex config. Returns "enforced", "not-enforced",
    "inconclusive" or "skipped"."""
    executable = executable or shutil.which("codex")
    if not executable:
        print("codex-hooks --probe-deny: codex not found, skipped")
        return "skipped"
    version = codex_version(executable)
    with tempfile.TemporaryDirectory(prefix="vault-engine-probe-") as tmp:
        work = Path(tmp)
        (work / ".codex").mkdir()
        hook = work / "probe-hook.py"
        hook.write_text(PROBE_HOOK, encoding="utf-8", newline="\n")
        hooks = {"hooks": {"PreToolUse": [{
            "matcher": "^(Write|Edit|apply_patch|Bash|shell|exec_command)$",
            "hooks": [{"type": "command", "command": _command(hook)}]}]}}
        (work / ".codex" / "hooks.json").write_text(json.dumps(hooks, indent=2) + "\n",
                                                    encoding="utf-8", newline="\n")
        try:
            subprocess.run(["git", "init", "-q"], cwd=work, capture_output=True, timeout=60)
            subprocess.run([executable, "exec", "--sandbox", "workspace-write", PROBE_PROMPT],
                           cwd=work, capture_output=True, text=True, encoding="utf-8",
                           timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"codex-hooks --probe-deny: timed out after {timeout}s; nothing recorded")
            return "inconclusive"
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"codex-hooks --probe-deny: could not run ({exc}); nothing recorded")
            return "inconclusive"
        fired = (work / "fired.log").exists()
        blocked = not (work / "probe.txt").exists()
    if fired and blocked:
        result, text = "enforced", "hook fired and the write was blocked"
    elif fired:
        result, text = "not-enforced", "hook fired but Codex still wrote the file (deny NOT enforced)"
    else:
        result, text = "inconclusive", ("hook did not fire (project hooks may need trust, or "
                                         "this CLI uses other tool names)")
    print(f"codex-hooks --probe-deny: {text} [{version or 'unknown version'}]")
    if version and record_tested_version(version, data):
        print(f"  recorded {TESTED_KEY} in machine.json")
    return result


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("codex-hooks", help="install Codex context/delegation hooks and worker profiles")
    parser.add_argument("--probe-deny", action="store_true",
                        help="run one throwaway `codex exec` to test whether a PreToolUse deny "
                             "is enforced, and record the Codex version tested")
    parser.add_argument("--install", action="store_true",
                        help="write hooks.json and missing worker profiles (default: dry run)")
    parser.add_argument("--hooks", help="hooks.json path (default: <CODEX_HOME>/hooks.json)")
    parser.add_argument("--agents-dir", help="worker directory (default: <CODEX_HOME>/agents)")
    parser.set_defaults(func=cmd_codex_hooks)
