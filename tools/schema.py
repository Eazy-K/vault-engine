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
