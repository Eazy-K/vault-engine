"""`graph.py models`: one source of truth for which Claude Code model each role
(orchestrator, worker-low, worker-medium) uses, instead of settings.json,
tools/claude-agents/worker-*.md and agent-guard.py each hard-coding their own
(see tools/model_config.py for the ranking and the config layering rules).

Optional extension module: graph.py imports this if present (see EXTENSIONS in
graph.py) and calls register(sub) with its argparse subparsers object.
Stdlib only, like graph.py itself.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

import graph as g
import model_config as mc

DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"
DEFAULT_AGENTS_DIR = Path.home() / ".claude" / "agents"

WORKER_ROLES = ("worker-low", "worker-medium")
FLAG_TO_ROLE = {"orchestrator": "orchestrator", "worker_low": "worker-low",
                 "worker_medium": "worker-medium"}


# --- config read/write --------------------------------------------------------

def _load(path: Path) -> dict:
    """Same shape as claude_hooks._load: {} if missing or not a JSON object."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_models(path: Path, role_updates: dict[str, dict[str, str]]) -> None:
    """Merge role_updates into the "models" key of the JSON object at `path`,
    leaving every other key (and every other role/field already there) alone."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load(path)
    models = data.get("models")
    models = dict(models) if isinstance(models, dict) else {}
    for role, values in role_updates.items():
        entry = models.get(role)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry.update(values)
        models[role] = entry
    data["models"] = models
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8", newline="\n")


def _backup(path: Path) -> Path | None:
    """A dated .bak copy of `path` before it is overwritten, or None if there
    was nothing to back up. Collision-safe like claude_hooks' settings backup."""
    if not path.exists():
        return None
    today = datetime.date.today().strftime("%Y%m%d")
    backup = path.with_name(path.name + f".bak-{today}")
    n = 1
    while backup.exists():
        n += 1
        backup = path.with_name(path.name + f".bak-{today}-{n}")
    backup.write_bytes(path.read_bytes())
    return backup


# --- apply: settings.json + worker-*.md frontmatter ---------------------------

def settings_matches(effective: dict, settings_path: Path) -> bool:
    """True if settings.json's "model" and "effortLevel" already match the
    configured orchestrator (the fields `--apply` writes)."""
    settings = _load(settings_path)
    orch = effective["orchestrator"]
    return settings.get("model") == orch["model"] and settings.get("effortLevel") == orch["effort"]


def _apply_settings(effective: dict, settings_path: Path) -> bool:
    """Write orchestrator model/effort into settings.json's "model"/"effortLevel"
    fields, leaving every other key untouched. Returns True if it wrote anything.

    Note: Claude Code's own `/effort` can additionally save a per-model override
    under "modelSettings.<model-id>.effortLevel" (a specific model id, not the
    "opus"/"sonnet"/"haiku" alias used here); that field takes precedence for
    that model id over the plain "effortLevel" this writes, and this command
    never touches it."""
    if settings_matches(effective, settings_path):
        return False
    settings = _load(settings_path)
    if settings_path.exists():
        _backup(settings_path)
    settings["model"] = effective["orchestrator"]["model"]
    settings["effortLevel"] = effective["orchestrator"]["effort"]
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8", newline="\n")
    return True


_FRONTMATTER_KEY = re.compile(r"^(model|effort):\s*(.*)$")


def _frontmatter_bounds(text: str) -> tuple[int, int] | None:
    """(start, end) line indices of the frontmatter body (between the two "---"
    delimiter lines), or None if `text` has no frontmatter block."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\n") != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\n") == "---":
            return 1, i
    return None


def worker_frontmatter(text: str) -> dict[str, str]:
    """The "model" and "effort" values currently in a worker-*.md's frontmatter
    (whichever of the two are present)."""
    bounds = _frontmatter_bounds(text)
    if bounds is None:
        return {}
    start, end = bounds
    found = {}
    for line in text.splitlines(keepends=True)[start:end]:
        m = _FRONTMATTER_KEY.match(line.rstrip("\n"))
        if m:
            found[m.group(1)] = m.group(2).strip()
    return found


def worker_md_matches(effective: dict, role: str, agents_dir: Path) -> bool:
    """True if <agents_dir>/<role>.md's frontmatter already has this role's
    configured model and effort. False (not an error) if the file is missing."""
    path = agents_dir / f"{role}.md"
    if not path.exists():
        return False
    found = worker_frontmatter(path.read_text(encoding="utf-8"))
    wanted = effective[role]
    return found.get("model") == wanted["model"] and found.get("effort") == wanted["effort"]


def _apply_worker_md(effective: dict, role: str, agents_dir: Path) -> bool:
    """Set model:/effort: in <agents_dir>/<role>.md's frontmatter to this role's
    configured values, adding either key if missing, touching nothing else in
    the file. Returns True if it wrote anything; False (not an error) if the
    file doesn't exist or has no frontmatter block."""
    path = agents_dir / f"{role}.md"
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    bounds = _frontmatter_bounds(text)
    if bounds is None:
        return False
    if worker_md_matches(effective, role, agents_dir):
        return False
    start, end = bounds
    lines = text.splitlines(keepends=True)
    wanted = {"model": effective[role]["model"], "effort": effective[role]["effort"]}
    seen: set[str] = set()
    new_body = []
    for line in lines[start:end]:
        m = _FRONTMATTER_KEY.match(line.rstrip("\n"))
        if m and m.group(1) in wanted:
            new_body.append(f"{m.group(1)}: {wanted[m.group(1)]}\n")
            seen.add(m.group(1))
        else:
            new_body.append(line)
    for key in ("model", "effort"):
        if key not in seen:
            new_body.append(f"{key}: {wanted[key]}\n")
    new_text = "".join(lines[:start] + new_body + lines[end:])
    _backup(path)
    path.write_text(new_text, encoding="utf-8", newline="\n")
    return True


def apply_config(effective: dict, settings_path: Path, agents_dir: Path) -> list[str]:
    """Write `effective` into settings.json and the worker-*.md frontmatter
    files. Returns the list of paths actually changed."""
    changed = []
    if _apply_settings(effective, settings_path):
        changed.append(str(settings_path))
    for role in WORKER_ROLES:
        if _apply_worker_md(effective, role, agents_dir):
            changed.append(str(agents_dir / f"{role}.md"))
    return changed


# --- doctor ---------------------------------------------------------------

def status(data_dir: Path, settings_path: Path = DEFAULT_SETTINGS,
           agents_dir: Path = DEFAULT_AGENTS_DIR) -> tuple[str, str] | None:
    """("OK"|"WARN", message) for `doctor`: whether settings.json and the
    worker-*.md agent files match the configured model selection. None (skip
    silently) if settings.json doesn't exist yet -- nothing to compare."""
    if not settings_path.exists():
        return None
    effective, _ = mc.load_layered(data_dir / "vault.config.json",
                                    data_dir / ".graph" / "machine.json")
    stale = []
    if not settings_matches(effective, settings_path):
        stale.append(settings_path.name)
    for role in WORKER_ROLES:
        if not worker_md_matches(effective, role, agents_dir):
            stale.append(f"{role}.md")
    if stale:
        return "WARN", (f"model config out of date in {', '.join(stale)}: run "
                         "`graph.py models --apply` to sync")
    return "OK", "model config matches settings.json and worker agent files"


# --- command ----------------------------------------------------------------

def _downgrade_target(orchestrator_model: str) -> str | None:
    """The most capable model still strictly cheaper than `orchestrator_model`,
    for auto-downgrading a worker that is no longer cheaper. None if unknown."""
    cheaper = mc.cheaper_than(orchestrator_model)
    return cheaper[-1] if cheaper else None


def cmd_models(args: argparse.Namespace) -> None:
    data_dir = Path(args.data).expanduser().resolve() if args.data else g.resolve_data_dir()
    shared_path = data_dir / "vault.config.json"
    machine_path = data_dir / ".graph" / "machine.json"
    settings_path = Path(args.settings).expanduser() if args.settings else DEFAULT_SETTINGS
    agents_dir = Path(args.agents_dir).expanduser() if args.agents_dir else DEFAULT_AGENTS_DIR

    role_updates: dict[str, dict[str, str]] = {}
    explicit_worker_models: set[str] = set()

    def set_value(role: str, field: str, value: str | None, *, explicit_model: bool = False) -> None:
        if value is None:
            return
        role_updates.setdefault(role, {})[field] = value
        if explicit_model:
            explicit_worker_models.add(role)

    set_value("orchestrator", "model", args.orchestrator)
    set_value("orchestrator", "effort", args.effort)
    set_value("worker-low", "model", args.worker_low, explicit_model=True)
    set_value("worker-low", "effort", args.worker_low_effort)
    set_value("worker-medium", "model", args.worker_medium, explicit_model=True)
    set_value("worker-medium", "effort", args.worker_medium_effort)

    if role_updates:
        effective, _ = mc.load_layered(shared_path, machine_path)
        for role, values in role_updates.items():
            effective[role].update(values)

        orch_model = effective["orchestrator"]["model"]
        for role in WORKER_ROLES:
            worker_model = effective[role]["model"]
            if mc.is_cheaper(worker_model, orch_model):
                continue
            if role in explicit_worker_models:
                sys.exit(f"models: --{role} model {worker_model!r} is not cheaper than "
                          f"orchestrator {orch_model!r}; refusing. Pass a cheaper model, or "
                          "leave it unset to let it be downgraded automatically.")
            downgrade = _downgrade_target(orch_model)
            if downgrade is None:
                sys.exit(f"models: orchestrator model {orch_model!r} is not a known model "
                          "(haiku, sonnet, opus)")
            print(f"note: {role} ({worker_model}) is not cheaper than orchestrator "
                  f"({orch_model}); downgrading {role} to {downgrade!r}")
            role_updates.setdefault(role, {})["model"] = downgrade
            effective[role]["model"] = downgrade

        target = machine_path if args.this_computer else shared_path
        _write_models(target, role_updates)
        print(f"  wrote: {target} ({', '.join(sorted(role_updates))})")

    effective, sources = mc.load_layered(shared_path, machine_path)

    print("effective model config:")
    for role in mc.ROLES:
        model, effort = effective[role]["model"], effective[role]["effort"]
        model_src, effort_src = sources[role]["model"], sources[role]["effort"]
        print(f"  {role}: model={model} ({model_src})  effort={effort} ({effort_src})")

    if settings_path.exists():
        note = "matches" if settings_matches(effective, settings_path) else "OUT OF DATE"
        print(f"  {settings_path}: {note}")
    else:
        print(f"  {settings_path}: not found")

    for role in WORKER_ROLES:
        path = agents_dir / f"{role}.md"
        if path.exists():
            note = "matches" if worker_md_matches(effective, role, agents_dir) else "OUT OF DATE"
            print(f"  {path}: {note}")
        else:
            print(f"  {path}: not found")

    if args.apply:
        changed = apply_config(effective, settings_path, agents_dir)
        if changed:
            print("applied:")
            for path in changed:
                print(f"  {path}")
        else:
            print("applied: nothing to change")


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("models", help="show or set which Claude Code model each role "
                                       "(orchestrator, worker-low, worker-medium) uses")
    p.add_argument("--orchestrator", help="set the orchestrator model (e.g. opus, sonnet)")
    p.add_argument("--effort", help="set the orchestrator's effort (e.g. low, medium, high)")
    p.add_argument("--worker-low", help="set the worker-low model")
    p.add_argument("--worker-low-effort", help="set the worker-low effort")
    p.add_argument("--worker-medium", help="set the worker-medium model")
    p.add_argument("--worker-medium-effort", help="set the worker-medium effort")
    p.add_argument("--this-computer", action="store_true",
                    help="write to this computer's .graph/machine.json instead of the "
                         "shared vault.config.json")
    p.add_argument("--apply", action="store_true",
                    help="also write the effective config into settings.json's "
                         "model/effortLevel and the worker-*.md frontmatter "
                         "(backs up each file first)")
    p.add_argument("--data", help="data dir (default: resolve_data_dir())")
    p.add_argument("--settings", help=f"settings.json path (default: {DEFAULT_SETTINGS})")
    p.add_argument("--agents-dir", help=f"Claude Code agents dir (default: {DEFAULT_AGENTS_DIR})")
    p.set_defaults(func=cmd_models)
