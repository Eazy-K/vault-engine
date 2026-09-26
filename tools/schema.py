#!/usr/bin/env python3
"""Data repo schema version, so engines of different versions can share one vault.

vault.config.json carries `"schema": <int>`, the layout version of the data repo.
A vault without the field is schema 1. Each engine release knows which schema it
writes (SCHEMA_VERSION). Two computers may run different engine versions against
the same vault; the rules that keep that safe:

  - data schema > SCHEMA_VERSION: this engine is older than the vault. Reading
    (`context`, `query`) still works; every command that writes shared data
    refuses via require_writable() and asks for an update on this computer.
  - data schema < SCHEMA_VERSION: the vault predates this engine. migrate()
    applies the steps in MIGRATIONS one schema at a time and commits the result
    in the data repo.

migrate() also applies config fixups that older engines still read correctly,
so they never bump the schema and are checked on every run: recording a
missing "schema" field, and moving per-computer `project_roots` out of the
shared vault.config.json (older `init` wrote an absolute path there) into this
computer's gitignored .graph/machine.json.

This module is loaded by tools/graph.py (see EXTENSIONS) if present.
Standard library only, matching the engine's own constraint.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import graph as g

# The data layout this engine writes. Bump it only together with a migration
# step in MIGRATIONS and an "Upgrade notes" entry in CHANGELOG.md.
SCHEMA_VERSION = 1

# MIGRATIONS[n] upgrades a data repo from schema n to n + 1, in place, and
# returns the files it changed (committed together with vault.config.json).
# Each step must be idempotent enough to rerun after a crash (the schema field
# is written only after the step returns).
MIGRATIONS: dict[int, Callable[["g.Paths"], "list[Path] | None"]] = {}

CONFIG_NAME = "vault.config.json"

_SCHEMA_RE = re.compile(r"^SCHEMA_VERSION\s*=\s*(\d+)\s*$", re.M)


def _schema_field(raw: dict) -> int | None:
    """The explicit, valid "schema" value of a config dict, else None."""
    value = raw.get("schema")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def read_schema(paths: "g.Paths") -> int:
    """The data repo's schema; 1 when vault.config.json or the field is missing."""
    return _schema_field(_load_config(paths)) or 1


def has_schema_field(paths: "g.Paths") -> bool:
    """False when vault.config.json leaves the schema implicit (older vaults)."""
    return _schema_field(_load_config(paths)) is not None


def shared_project_roots(paths: "g.Paths") -> list[str]:
    """project_roots still set in the shared vault.config.json (older `init`
    wrote this computer's absolute path there); [] when absent."""
    raw = _load_config(paths)
    if "project_roots" not in raw:
        return []
    return [r for r in g._as_list(raw["project_roots"]) if isinstance(r, str) and r.strip()]


def require_writable(paths: "g.Paths") -> None:
    """Exit before a command writes shared data into a vault that a newer engine
    has already upgraded; this engine would not know its layout."""
    data_schema = read_schema(paths)
    if data_schema > SCHEMA_VERSION:
        raise SystemExit(
            f"this vault was upgraded by a newer engine (schema {data_schema}, this "
            f"engine writes {SCHEMA_VERSION}); run `update` on this computer first. "
            "Reading notes (context) still works.")


def schema_of_engine_ref(engine: Path, ref: str) -> int | None:
    """SCHEMA_VERSION at a git ref of the engine repo (e.g. a release tag), so
    `update` can refuse a downgrade below the vault's schema. Releases from before
    this module existed write schema 1. None if the ref cannot be read."""
    try:
        exists = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                                cwd=engine, capture_output=True, text=True)
        if exists.returncode != 0:
            return None
        out = subprocess.run(["git", "show", f"{ref}:tools/schema.py"], cwd=engine,
                             capture_output=True, text=True, encoding="utf-8")
    except OSError:
        return None
    if out.returncode != 0:
        return 1
    match = _SCHEMA_RE.search(out.stdout)
    return int(match.group(1)) if match else None


# --- migration ---------------------------------------------------------------
# migrate() plans first (plan_migration), so --dry-run, the confirmation prompt
# and the actual run all describe the same work, then writes and commits only
# vault.config.json plus the files the schema steps report.

@dataclass
class Plan:
    """What migrate() would change in the data repo, computed without writing."""

    current: int
    steps: list[int] = field(default_factory=list)  # schemas reached via MIGRATIONS
    record_field: bool = False  # "schema" missing (implicit): write it
    roots_action: str = ""  # "", "move", "drop" or "keep" (see _plan_roots)
    roots: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.steps or self.record_field or self.roots_action in ("move", "drop"))

    def commit_message(self) -> str:
        parts = []
        if self.steps:
            parts.append(f"migrate vault schema {self.current} -> {self.steps[-1]}")
        elif self.record_field:
            parts.append(f"record vault schema {SCHEMA_VERSION}")
        if self.roots_action == "move":
            parts.append("move project_roots to machine.json")
        elif self.roots_action == "drop":
            parts.append("remove project_roots from the shared config")
        return "chore: " + ", ".join(parts)


@dataclass
class MigrationResult:
    plan: Plan
    # The committed state of vault.config.json still needed the plan below; the
    # working copy already has it (an earlier migrate that was never committed).
    leftover: Plan | None = None
    written: bool = False
    committed: str | None = None  # commit subject, if a commit was made
    uncommitted_reason: str = ""


def _load_config(paths: "g.Paths") -> dict:
    """Raw vault.config.json as a dict, preserving key order; {} if missing or
    unreadable (json.loads keeps insertion order, so round-tripping this dict
    never reorders the file's other keys)."""
    return _load_json(paths.config_file)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")


def _write_config(paths: "g.Paths", data: dict) -> None:
    _write_json(paths.config_file, data)


def _machine_file(paths: "g.Paths") -> Path:
    return paths.data / ".graph" / "machine.json"


def _resolve(raw: str) -> Path | None:
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _plan_roots(paths: "g.Paths", raw: dict, plan: Plan) -> None:
    """project_roots in the shared config is a per-computer path. It moves to
    this computer's machine.json only when every root exists here; paths that
    are missing here probably belong to another computer, so they stay (this
    computer ignores them, see graph.project_roots) until migrate runs there."""
    if "project_roots" not in raw:
        return
    roots = [r for r in g._as_list(raw["project_roots"]) if isinstance(r, str) and r.strip()]
    plan.roots = roots
    resolved = [_resolve(r) for r in roots]
    default = paths.engine.resolve().parent
    machine = _load_json(_machine_file(paths))
    if not roots:
        plan.roots_action = "drop"
        plan.actions.append(f"remove the empty project_roots from {CONFIG_NAME}")
    elif all(p == default for p in resolved):
        plan.roots_action = "drop"
        plan.actions.append(f"remove project_roots from {CONFIG_NAME} (it names the engine's "
                            "parent folder, which is already the default)")
    elif not all(p is not None and p.is_dir() for p in resolved):
        plan.roots_action = "keep"
        plan.notes.append(f"project_roots in {CONFIG_NAME} names folders that do not exist on "
                          "this computer (probably another computer's); left in place and "
                          "ignored here. Run migrate on the computer it belongs to.")
    elif machine.get("project_roots"):
        plan.roots_action = "drop"
        plan.actions.append(f"remove project_roots from {CONFIG_NAME} (this computer's "
                            ".graph/machine.json already sets its own)")
    else:
        plan.roots_action = "move"
        plan.actions.append(f"move project_roots from the shared {CONFIG_NAME} to this "
                            "computer's .graph/machine.json (gitignored)")


def _load_json(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def plan_migration(paths: "g.Paths", raw: dict | None = None) -> Plan:
    """The work migrate() would do for `raw` (default: the vault.config.json on
    disk). Raises SystemExit if a needed schema step is not registered."""
    if raw is None:
        raw = _load_config(paths)
    explicit = _schema_field(raw)
    plan = Plan(explicit or 1)
    plan.steps = list(range(plan.current + 1, SCHEMA_VERSION + 1))
    for n in plan.steps:
        if n - 1 not in MIGRATIONS:
            raise SystemExit(f"no migration step registered for schema {n - 1} -> {n}; "
                             "cannot migrate (this engine release is missing it)")
    if plan.steps:
        plan.actions.append(f"migrate the data layout: schema {plan.current} -> {SCHEMA_VERSION}")
    elif explicit is None:
        plan.record_field = True
        plan.actions.append(f'record "schema": {SCHEMA_VERSION} in {CONFIG_NAME} '
                            "(missing, so far implicit)")
    _plan_roots(paths, raw, plan)
    return plan


def _final_config(raw: dict, plan: Plan) -> dict:
    """vault.config.json after `plan` (schema steps may change other files too)."""
    out = dict(raw)
    if plan.steps or plan.record_field:
        out["schema"] = plan.steps[-1] if plan.steps else SCHEMA_VERSION
    if plan.roots_action in ("move", "drop"):
        out.pop("project_roots", None)
    return out


def _git(data: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=data, capture_output=True, text=True,
                          encoding="utf-8")


def _is_git_repo(data: Path) -> bool:
    try:
        return _git(data, "rev-parse", "--git-dir").returncode == 0
    except OSError:
        return False


def _has_head(data: Path) -> bool:
    return _git(data, "rev-parse", "--verify", "-q", "HEAD").returncode == 0


def _config_pending(data: Path) -> bool:
    """vault.config.json differs from HEAD (staged, unstaged or untracked)."""
    out = _git(data, "status", "--porcelain", "--untracked-files=all", "--", CONFIG_NAME)
    return out.returncode == 0 and bool(out.stdout.strip())


def _head_config(data: Path) -> dict:
    out = _git(data, "show", f"HEAD:{CONFIG_NAME}")
    if out.returncode != 0:
        return {}
    try:
        raw = json.loads(out.stdout or "{}")
    except ValueError:
        return {}
    return raw if isinstance(raw, dict) else {}


def migrate(paths: "g.Paths", *, commit: bool = True, dry_run: bool = False) -> MigrationResult:
    """Bring the data repo up to this engine: schema steps plus config fixups.

    Commits exactly vault.config.json and the files the steps report, with
    `git commit --only`, so anything else already staged stays staged and out
    of the commit. If vault.config.json holds uncommitted edits that migrate
    itself would not make, nothing is written (SystemExit). Uncommitted edits
    that are exactly an earlier migrate's result are committed now, so a rerun
    after `--no-commit` or a failed commit converges on one migration commit."""
    require_writable(paths)
    data = paths.data
    raw = _load_config(paths)
    plan = plan_migration(paths, raw)
    result = MigrationResult(plan)

    can_commit = commit and _is_git_repo(data) and _has_head(data)
    if commit and not can_commit:
        result.uncommitted_reason = ("the data folder is not a git repo" if not _is_git_repo(data)
                                     else "the data repo has no commits yet")
    elif not commit:
        result.uncommitted_reason = "--no-commit"
    if can_commit and _config_pending(data):
        head_raw = _head_config(data)
        head_plan = plan_migration(paths, head_raw)
        if _final_config(head_raw, head_plan) != _final_config(raw, plan):
            if plan.empty:
                return result  # up to date; the pending edits are the user's own
            raise SystemExit(f"migrate: {CONFIG_NAME} has uncommitted changes that migrate "
                             "would not make; commit or discard them, then rerun migrate "
                             "(nothing was written)")
        if not head_plan.empty:
            result.leftover = head_plan

    if dry_run or (plan.empty and result.leftover is None):
        return result

    touched: set[Path] = set()
    for n in plan.steps:
        changed = MIGRATIONS[n - 1](paths)
        if changed:
            touched.update(Path(p) for p in changed)
        raw["schema"] = n
        _write_config(paths, raw)
    if plan.roots_action == "move":
        # machine.json first: a crash in between must not lose the value.
        machine = _load_json(_machine_file(paths))
        machine["project_roots"] = plan.roots
        _write_json(_machine_file(paths), machine)
    fixups = plan.roots_action in ("move", "drop")
    if fixups:
        raw.pop("project_roots", None)
    if plan.record_field:
        raw["schema"] = SCHEMA_VERSION
    if fixups or plan.record_field:
        _write_config(paths, raw)
    result.written = not plan.empty

    if not can_commit:
        return result
    files = [CONFIG_NAME] + [str(p) for p in sorted(touched)]
    add = _git(data, "add", "--", *files)
    if add.returncode != 0:
        raise SystemExit(f"migrate: git add failed: {add.stderr.strip()}\n"
                         "the changes are written; rerun migrate to commit them")
    message = (result.leftover or plan).commit_message()
    done = _git(data, "commit", "-q", "--only", "-m", message, "--", *files)
    if done.returncode != 0:
        raise SystemExit(f"migrate: git commit failed: {(done.stderr or done.stdout).strip()}\n"
                         "the changes are written; rerun migrate to commit them")
    result.committed = message
    return result


def cmd_migrate(args) -> None:
    paths = g.default_paths()
    commit = not args.no_commit
    dry_run = args.dry_run
    preview = migrate(paths, commit=commit, dry_run=True)  # exits if migrate would refuse
    plan = preview.plan
    implicit = " (implicit)" if plan.record_field else ""
    print(f"vault schema {plan.current}{implicit}; this engine writes schema {SCHEMA_VERSION}")
    for note in plan.notes:
        print(f"note: {note}")

    if plan.empty:
        if preview.leftover is None:
            print("nothing to migrate")
            return
        print(f"{CONFIG_NAME} holds an earlier migration that was never committed")
        if dry_run:
            print(f"dry run: would commit it ({preview.leftover.commit_message()})")
            return
        result = migrate(paths, commit=commit)
        print(f"committed: {result.committed}")
        return

    print("migrate will:")
    for action in plan.actions:
        print(f"  - {action}")
    if dry_run:
        print("dry run: nothing written")
        return
    if not args.yes:
        if not g.stdin_is_interactive():
            sys.exit("rerun with --yes to apply (or --dry-run to preview)")
        try:
            answer = input("proceed? [y/N]: ").strip().lower()
        except EOFError:
            print()
            sys.exit("no answer (input ended), nothing written; "
                     "rerun with --yes to apply (or --dry-run to preview)")
        if not answer.startswith("y"):
            print("aborted, nothing written")
            return

    result = migrate(paths, commit=commit)
    if result.committed:
        print(f"committed: {result.committed}")
    else:
        print(f"written, not committed ({result.uncommitted_reason})")


def register(sub) -> None:
    """Called by graph.py's extension mechanism (see EXTENSIONS)."""
    p = sub.add_parser("migrate", help="upgrade the data repo to this engine's schema and config")
    p.add_argument("--yes", action="store_true", help="never prompt, proceed automatically")
    p.add_argument("--no-commit", action="store_true", help="leave changes uncommitted")
    p.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    p.set_defaults(func=cmd_migrate)
