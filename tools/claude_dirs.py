"""`graph.py claude-dirs`: the Claude Code config dirs on this computer.

Claude Code reads its config from $CLAUDE_CONFIG_DIR when set, else ~/.claude,
and a computer can have several (a personal one, a work one). The engine keeps
a registry of the ones it manages in this computer's gitignored
<data>/.graph/machine.json (key "claude_dirs"), never in the engine repo:

- ~/.claude is always implicitly included (when it exists).
- A dir is recorded when an engine command runs with CLAUDE_CONFIG_DIR pointing
  at it (observed, not guessed; not when it lies in the system temp folder), or
  when the user adds it (`--add`). Registered dirs that no longer exist are
  skipped (doctor says so once).
- Other `.claude*` dirs under the home folder that look like a Claude config
  dir (settings.json or projects/) are only *candidates*: they are never added
  on their own. `--ask` prints them as a question for the agent to put to the
  user, like `projects --ask`.

`claude-hooks --install`, `models` and `update --claude-hooks/--models/--agents`
act on every registered dir; an explicit --settings/--agents-dir targets just
that one. Hooks do not record CLAUDE_CONFIG_DIR: they run on every tool call
and reading the registry there would need the data dir lookup each time.

Optional extension module: graph.py imports this (see EXTENSIONS in graph.py)
and calls register(sub) with its argparse subparsers object. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

import graph as g

KEY = "claude_dirs"


# --- registry ---------------------------------------------------------------

def _data(data: Path | None = None) -> Path | None:
    """The data dir, or None when it is not configured (the registry then
    contributes nothing and is not written)."""
    if data is not None:
        return data
    try:
        return g.resolve_data_dir()
    except SystemExit:
        return None


def _machine_file(data: Path) -> Path:
    return data / ".graph" / "machine.json"


def _load_machine(data: Path) -> dict:
    try:
        loaded = g.load_state_json(_machine_file(data))
    except OSError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _saved(data: Path | None) -> list[Path]:
    if data is None:
        return []
    raw = _load_machine(data).get(KEY)
    if not isinstance(raw, list):
        return []
    return [Path(str(p)).expanduser() for p in raw if isinstance(p, str) and p.strip()]


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def _add_unique(dirs: list[Path], path: Path) -> None:
    if not any(_same(path, d) for d in dirs):
        dirs.append(path)


def implicit() -> Path:
    return Path.home() / ".claude"


def sources(data: Path | None = None) -> list[tuple[Path, str]]:
    """(dir, source) for every registered dir: "default" (~/.claude, when it
    exists), "environment" (CLAUDE_CONFIG_DIR of this process) and "registered"."""
    data = _data(data)
    result: list[tuple[Path, str]] = []
    seen: list[Path] = []
    if implicit().is_dir():
        seen.append(implicit())
        result.append((implicit(), "default"))
    for path in _saved(data):
        if not path.is_dir():
            continue  # deleted since it was registered: see missing()
        if not any(_same(path, d) for d in seen):
            seen.append(path)
            result.append((path, "registered"))
    if os.environ.get("CLAUDE_CONFIG_DIR", "").strip():
        active = g.claude_config_dir()
        if not any(_same(active, d) for d in seen):
            seen.append(active)
            result.append((active, "environment"))
    return result


def missing(data: Path | None = None) -> list[Path]:
    """Registered dirs that no longer exist (e.g. a deleted temporary
    CLAUDE_CONFIG_DIR). They are skipped everywhere; `claude-dirs --remove`
    drops them from the registry."""
    return [p for p in _saved(_data(data)) if not p.is_dir()]


def _in_temp(path: Path) -> bool:
    try:
        path.resolve().relative_to(Path(tempfile.gettempdir()).resolve())
        return True
    except (OSError, ValueError):
        return False


def registered(data: Path | None = None) -> list[Path]:
    return [path for path, _source in sources(data)]


def _write(data: Path, dirs: list[Path]) -> None:
    machine = _load_machine(data)
    machine[KEY] = [str(d) for d in dirs]
    path = _machine_file(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    g.atomic_write_text(path, json.dumps(machine, indent=2, ensure_ascii=False) + "\n")


def add(path: Path, data: Path | None = None) -> bool:
    """Register `path`. True if it was new. False if already known or there is
    no data dir to keep the registry in."""
    data = _data(data)
    if data is None or not data.is_dir():
        return False
    path = path.expanduser()
    if _same(path, implicit()):
        return False
    saved = _saved(data)
    if any(_same(path, d) for d in saved):
        return False
    saved.append(path)
    _write(data, saved)
    return True


def remove(path: Path, data: Path | None = None) -> bool:
    data = _data(data)
    if data is None:
        return False
    saved = _saved(data)
    kept = [d for d in saved if not _same(d, path.expanduser())]
    if len(kept) == len(saved):
        return False
    _write(data, kept)
    return True


def record_env() -> bool:
    """Called by every engine command: remember the CLAUDE_CONFIG_DIR it runs
    with. Never raises."""
    try:
        if not os.environ.get("CLAUDE_CONFIG_DIR", "").strip():
            return False
        active = g.claude_config_dir()
        if not active.is_dir() or _in_temp(active):
            return False  # a throwaway dir in the temp folder is used, never remembered
        return add(active)
    except (OSError, SystemExit):
        return False


# --- candidates -------------------------------------------------------------

def _looks_like_config(path: Path) -> bool:
    return (path / "settings.json").is_file() or (path / "projects").is_dir()


def _subdirs(path: Path) -> list[Path]:
    try:
        return sorted(p for p in path.iterdir() if p.is_dir())
    except OSError:
        return []


def candidates(data: Path | None = None) -> list[Path]:
    """Unconfirmed `.claude*` dirs under the home folder (one or two levels)
    that look like Claude config dirs and are not registered."""
    known = registered(data)
    home = Path.home()
    found: list[Path] = []
    try:
        top = sorted(p for p in home.iterdir() if p.name.startswith(".claude") and p.is_dir())
    except OSError:
        return []
    for entry in top:
        options = [entry]
        if not _same(entry, implicit()):
            options += _subdirs(entry)
        for option in options:
            if _looks_like_config(option) and not any(_same(option, d) for d in known):
                _add_unique(found, option)
    return found


# --- what the other commands use ---------------------------------------------

def _candidates_notice(cands: list[Path]) -> None:
    graph_py = Path(g.__file__).resolve()
    print("note: other Claude Code config dirs exist on this computer but are not registered: "
          + ", ".join(str(c) for c in cands))
    print(f'  to include one: python "{graph_py}" claude-dirs --add <path> '
          f'(or `claude-dirs --ask` to have the agent ask the user); only registered dirs '
          f'are changed')


def _prompt_choice(cands: list[Path]) -> list[Path]:
    print("Other Claude Code config dirs found. Install to which?")
    for i, cand in enumerate(cands, 1):
        print(f"  {i}) {cand}")
    print(f"  {len(cands) + 1}) all")
    try:
        raw = input("Choice (number, empty = none of these): ").strip()
    except EOFError:
        return []
    if raw.isdigit():
        number = int(raw)
        if number == len(cands) + 1:
            return list(cands)
        if 1 <= number <= len(cands):
            return [cands[number - 1]]
    return []


def targets(prompt: bool = False) -> list[Path]:
    """The config dirs a write command should act on: every registered dir (or
    the active one when none is known). Unconfirmed candidates are offered in a
    terminal when `prompt` (the choice is registered) and otherwise only
    mentioned; they are never changed without a yes."""
    dirs = registered()
    cands = candidates()
    if cands:
        if prompt and g.stdin_is_interactive():
            for chosen in _prompt_choice(cands):
                add(chosen)
                _add_unique(dirs, chosen)
        else:
            _candidates_notice(cands)
    return dirs or [g.claude_config_dir()]


# --- command ----------------------------------------------------------------

def _print_list() -> None:
    entries = sources()
    if entries:
        print("Registered Claude Code config dirs:")
        for path, source in entries:
            print(f"  {path}  ({source})")
    else:
        print("No Claude Code config dir registered (and no ~/.claude).")
    gone = missing()
    if gone:
        print("Registered but missing (skipped; drop with --remove <path>):")
        for path in gone:
            print(f"  {path}")
    cands = candidates()
    if cands:
        print("Unconfirmed candidates (not changed until added):")
        for cand in cands:
            print(f"  {cand}")


def _print_ask() -> None:
    """Report for the agent: `claude-dirs --ask` never registers anything."""
    cands = candidates()
    if not cands:
        print("Nothing to ask: no unconfirmed Claude Code config dirs found.")
        return
    graph_py = Path(g.__file__).resolve()
    print("Claude Code config dirs found on this computer that are not registered:")
    for cand in cands:
        print(f"  {cand}")
    print()
    print("Ask the user which of these are Claude Code config dirs they use (hooks, models "
          "and subagent files are then installed into each registered dir), and whether "
          "there is another one not in this list.")
    print(f'For each the user confirms: python "{graph_py}" claude-dirs --add <path>. '
          "Leave the rest alone.")


def cmd_claude_dirs(args: argparse.Namespace) -> None:
    if args.add:
        path = Path(args.add).expanduser()
        if not path.is_dir():
            raise SystemExit(f"claude-dirs: {path} is not a folder")
        if _same(path, implicit()):
            print(f"{path} is always included")
        elif add(path):
            print(f"added: {path}")
        elif _data() is None:
            raise SystemExit("claude-dirs: VAULT_DATA is not set, so there is nowhere to "
                             "keep the list (run setup first)")
        else:
            print(f"already registered: {path}")
        return
    if args.remove:
        path = Path(args.remove).expanduser()
        if _same(path, implicit()):
            raise SystemExit(f"claude-dirs: {path} is always included and cannot be removed")
        print(f"removed: {path}" if remove(path) else f"not registered: {path}")
        return
    if args.ask:
        _print_ask()
        return
    _print_list()


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("claude-dirs", help="list/add/remove the Claude Code config dirs the "
                                           "engine manages on this computer")
    p.add_argument("--list", action="store_true",
                   help="show registered dirs and unconfirmed candidates (default)")
    p.add_argument("--add", metavar="PATH", help="register a Claude Code config dir")
    p.add_argument("--remove", metavar="PATH", help="unregister a dir")
    p.add_argument("--ask", action="store_true",
                   help="print the unconfirmed candidates as a question for the agent to ask "
                        "the user")
    p.set_defaults(func=cmd_claude_dirs)
