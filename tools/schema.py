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

This module is loaded by tools/graph.py (see EXTENSIONS) if present.
Standard library only, matching the engine's own constraint.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import graph as g

# The data layout this engine writes. Bump it only together with a migration
# step in MIGRATIONS and an "Upgrade notes" entry in CHANGELOG.md.
SCHEMA_VERSION = 1

# MIGRATIONS[n] upgrades a data repo from schema n to n + 1, in place. Each step
# must be idempotent enough to rerun after a crash (the schema field is written
# only after the step returns).
MIGRATIONS: dict[int, Callable[["g.Paths"], None]] = {}

_SCHEMA_RE = re.compile(r"^SCHEMA_VERSION\s*=\s*(\d+)\s*$", re.M)


def read_schema(paths: "g.Paths") -> int:
    """The data repo's schema; 1 when vault.config.json or the field is missing."""
    try:
        raw = json.loads(paths.config_file.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return 1
    value = raw.get("schema", 1) if isinstance(raw, dict) else 1
    return value if isinstance(value, int) and value >= 1 else 1


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

def _load_config(paths: "g.Paths") -> dict:
    """Raw vault.config.json as a dict, preserving key order; {} if missing or
    unreadable (json.loads keeps insertion order, so round-tripping this dict
    never reorders the file's other keys)."""
    try:
        raw = json.loads(paths.config_file.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_config(paths: "g.Paths", data: dict) -> None:
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                                  encoding="utf-8", newline="\n")


def _is_git_repo(data: Path) -> bool:
    try:
        out = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=data,
                              capture_output=True, text=True, encoding="utf-8")
    except OSError:
        return False
    return out.returncode == 0


def _commit_migration(paths: "g.Paths", touched: set[Path], from_schema: int, to_schema: int) -> None:
    """Stage exactly vault.config.json plus whatever the migration steps report
    as changed, and commit -- never `git add -A`, so unrelated pending work in
    the data repo is left alone. If something is already staged, this backs
    off entirely and lets the user commit by hand."""
    data = paths.data
    if not _is_git_repo(data):
        return
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=data,
                            capture_output=True, text=True, encoding="utf-8")
    if staged.returncode == 0 and staged.stdout.strip():
        print("data repo already has staged changes; commit them yourself, then rerun "
              "`migrate` (vault.config.json and any migrated files are left uncommitted)")
        return
    to_add = [str(paths.config_file)] + [str(p) for p in sorted(touched)]
    add = subprocess.run(["git", "add"] + to_add, cwd=data, capture_output=True, text=True)
    if add.returncode != 0:
        print(f"git add failed: {add.stderr.strip()}")
        return
    message = (f"chore: migrate vault schema {from_schema} -> {to_schema}"
               if to_schema != from_schema else f"chore: record vault schema {to_schema}")
    commit = subprocess.run(["git", "commit", "-q", "-m", message], cwd=data,
                            capture_output=True, text=True)
    if commit.returncode != 0:
        print(f"git commit failed: {commit.stderr.strip()}")


def migrate(paths: "g.Paths", *, commit: bool = True) -> list[int]:
    """Upgrade the data repo to SCHEMA_VERSION, one step at a time. Returns the
    list of schemas reached (empty if the vault was already current and its
    config already named its schema explicitly)."""
    require_writable(paths)
    current = read_schema(paths)
    raw = _load_config(paths)
    had_field = "schema" in raw
    reached: list[int] = []
    touched: set[Path] = set()

    n = current
    while n < SCHEMA_VERSION:
        step = MIGRATIONS.get(n)
        if step is None:
            raise SystemExit(f"no migration step registered for schema {n} -> {n + 1}; "
                             "cannot migrate (this engine release is missing it)")
        changed = step(paths)
        if changed:
            touched.update(Path(p) for p in changed)
        n += 1
        raw["schema"] = n
        _write_config(paths, raw)
        reached.append(n)

    if not reached and not had_field:
        # Schema 1, already current, just never written explicitly.
        raw["schema"] = SCHEMA_VERSION
        _write_config(paths, raw)
        reached.append(SCHEMA_VERSION)

    if reached and commit:
        _commit_migration(paths, touched, current, reached[-1])
    return reached


def cmd_migrate(args) -> None:
    paths = g.default_paths()
    require_writable(paths)
    current = read_schema(paths)
    if current < SCHEMA_VERSION:
        print(f"vault schema {current} -> {SCHEMA_VERSION}")
        if not args.yes:
            if sys.stdin.isatty():
                answer = input("proceed? [y/N]: ").strip().lower()
                if not answer.startswith("y"):
                    print("aborted")
                    return
            else:
                sys.exit("rerun with --yes")
    else:
        print(f"vault schema is up to date ({SCHEMA_VERSION})")

    reached = migrate(paths, commit=not args.no_commit)
    if reached:
        print(f"now at schema {reached[-1]}")


def register(sub) -> None:
    """Called by graph.py's extension mechanism (see EXTENSIONS)."""
    p = sub.add_parser("migrate", help="upgrade the data repo to this engine's schema version")
    p.add_argument("--yes", action="store_true", help="never prompt, proceed automatically")
    p.add_argument("--no-commit", action="store_true", help="leave changes uncommitted")
    p.set_defaults(func=cmd_migrate)
