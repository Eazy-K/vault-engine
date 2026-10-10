"""Project discovery: find the user's repos next to the engine so the agent
can suggest notes for projects that don't have any yet.

Only lightweight metadata is read (repo name, README first line, file
extension counts, git remote host, last commit date). Code is never indexed
and nothing leaves the machine; results are cached under the data folder,
which is gitignored (.graph/projects.json).

This module never writes project notes itself: `projects --ask` only prints a
report so the agent can ask the user which discovered projects should get
notes, then write real ones by hand (see `docs/agent-setup.md`). Two ways a
project stops being suggested, and how they differ:
- `exclude` (globs) in the shared `vault.config.json`: the project is hidden
  from discovery entirely, on every computer that uses this vault.
- `skip_projects` (names) in this computer's gitignored `.graph/machine.json`
  (written by `projects --skip`): the project still shows up in `projects`
  (marked skipped) and stays out of `--missing`/`--ask`, but only on this
  computer -- another computer sharing the vault is still asked about it.
"""
from __future__ import annotations

import fnmatch
import json
import subprocess
import time
from pathlib import Path

import graph as g

CACHE_TTL = 3600  # seconds; --refresh forces a rebuild sooner than this
WALK_FILE_LIMIT = 2000  # cap on files inspected per project for language stats
GIT_TIMEOUT = 3  # seconds; a hung remote must never stall discovery
SKIP_WALK_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build",
                   "target", "__pycache__"}
README_NAMES = ("README.md", "README")


def _is_git_repo(p: Path) -> bool:
    return (p / ".git").exists()


def _load_config(paths: g.Paths) -> dict:
    try:
        return json.loads(paths.config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _excluded(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def _readme_line(project: Path) -> str | None:
    for fname in README_NAMES:
        f = project / fname
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            return line[:160]
    return None


def _languages(project: Path) -> list[str]:
    counts: dict[str, int] = {}
    seen = 0
    stack = [project]
    while stack and seen < WALK_FILE_LIMIT:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in SKIP_WALK_DIRS:
                    stack.append(entry)
                continue
            seen += 1
            if seen > WALK_FILE_LIMIT:
                break
            ext = entry.suffix.lstrip(".")
            if ext:
                counts[ext] = counts.get(ext, 0) + 1
    return [ext for ext, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:3]]


def _remote_host(project: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=project,
            capture_output=True, text=True, encoding="utf-8",
            timeout=GIT_TIMEOUT, check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    if not out:
        return None
    # Never keep more than the host: the full URL can carry a username or path.
    if out.startswith("git@"):
        rest = out[len("git@"):]
        host = rest.split(":", 1)[0]
        return host or None
    for prefix in ("https://", "http://", "ssh://"):
        if out.startswith(prefix):
            rest = out[len(prefix):]
            rest = rest.split("@", 1)[-1]  # drop any user@ prefix
            host = rest.split("/", 1)[0].split(":", 1)[0]
            return host or None
    return None


def _last_commit(project: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI"], cwd=project,
            capture_output=True, text=True, encoding="utf-8",
            timeout=GIT_TIMEOUT, check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    return out or None


def discover(paths: g.Paths) -> list[dict]:
    """Scan configured project roots for git repos and collect their metadata."""
    config = _load_config(paths)
    exclude_patterns = config.get("exclude") or []
    skip = {paths.engine.resolve(), paths.data.resolve()}

    results: list[dict] = []
    for root in g.project_roots(paths):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or not _is_git_repo(entry):
                continue
            resolved = entry.resolve()
            if resolved in skip and not g.has_project_notes(paths, entry.name):
                continue
            if _excluded(entry.name, exclude_patterns):
                continue
            if (entry / ".vaultignore").exists():
                continue
            has_notes = g.has_project_notes(paths, entry.name)
            results.append({
                "name": entry.name,
                "path": str(resolved),
                "has_notes": has_notes,
                "readme": _readme_line(entry),
                "languages": _languages(entry),
                "remote_host": _remote_host(entry),
                "last_commit": _last_commit(entry),
            })
    return results


def _machine_json_path(paths: g.Paths) -> Path:
    return paths.data / ".graph" / "machine.json"


def _load_machine_json(paths: g.Paths) -> dict:
    try:
        data = g.load_state_json(_machine_json_path(paths))
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_machine_json(paths: g.Paths, updates: dict) -> None:
    """Merge `updates` into <data>/.graph/machine.json, keeping other keys
    (machine name, project_roots, ...) intact. Same shape and merge pattern
    as onboarding._write_machine_json, kept local here to avoid importing the
    much larger onboarding module for one write."""
    path = _machine_json_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_machine_json(paths)
    data.update(updates)
    g.atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def skip_list(paths: g.Paths) -> list[str]:
    """Project names this computer never suggests notes for (see the module
    docstring for how this differs from the shared `exclude` patterns)."""
    raw = _load_machine_json(paths).get("skip_projects")
    return [n for n in raw if isinstance(n, str)] if isinstance(raw, list) else []


def set_skip_list(paths: g.Paths, names: list[str]) -> None:
    _write_machine_json(paths, {"skip_projects": names})


def _cache_path(paths: g.Paths) -> Path:
    return paths.data / ".graph" / "projects.json"


def _root_signature(paths: g.Paths) -> list[list]:
    """[str(root), mtime] per configured root. A new or removed project folder
    changes its parent root's mtime, so this also catches that without waiting
    out CACHE_TTL; a root that doesn't exist (yet) gets mtime None."""
    sig = []
    for root in g.project_roots(paths):
        try:
            mtime = root.stat().st_mtime
        except OSError:
            mtime = None
        sig.append([str(root), mtime])
    return sig


def load_cached(paths: g.Paths, refresh: bool = False) -> list[dict]:
    """Reuse the cache when it is fresh (< CACHE_TTL) and no root's mtime moved
    (a new/removed project folder); rebuild otherwise."""
    cache_file = _cache_path(paths)
    # The roots (and their mtimes) are part of the key: a cache built for other
    # roots (another engine checkout, an edited config) or a since-changed root
    # must not be reused.
    signature = _root_signature(paths)
    if not refresh and cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < CACHE_TTL:
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and cached.get("roots") == signature:
                    return cached["projects"]
            except (OSError, ValueError, KeyError):
                pass
    results = discover(paths)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    g.atomic_write_text(cache_file, json.dumps({"roots": signature, "projects": results},
                                               indent=2, ensure_ascii=False))
    return results


def cached_only(paths: g.Paths) -> list[dict]:
    """Read the discovery cache without ever scanning or shelling out to git,
    for callers like `context` that run on every prompt and must stay fast.
    TTL is ignored here (a stale-but-present cache is still useful for a
    one-line hint); [] when there is no cache yet or it can't be parsed."""
    try:
        data = json.loads(_cache_path(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    projects = data.get("projects") if isinstance(data, dict) else None
    return projects if isinstance(projects, list) else []


def missing_projects(paths: g.Paths) -> list[str]:
    """Names of discovered projects that have no notes yet and are not on
    this computer's skip list, from the cache."""
    skip = set(skip_list(paths))
    return [p["name"] for p in load_cached(paths) if not p["has_notes"] and p["name"] not in skip]


def _print_table(projects: list[dict]) -> None:
    if not projects:
        print("No projects found.")
        return
    for p in projects:
        notes = "no (skipped)" if p.get("skipped") and not p["has_notes"] else \
                ("yes" if p["has_notes"] else "no")
        langs = ",".join(p["languages"]) or "-"
        commit = p["last_commit"] or "-"
        readme = p["readme"] or ""
        print(f"{p['name']:<24} notes:{notes:<13} {langs:<18} {commit:<20} {readme}")


def _project_line(p: dict) -> str:
    langs = ",".join(p["languages"]) or "-"
    commit = p["last_commit"] or "-"
    readme = p["readme"] or ""
    return f"  {p['name']:<24} {langs:<18} {commit:<20} {readme}"


def _print_ask(projects: list[dict]) -> None:
    """Report for the agent, not the user: `projects --ask` never writes notes
    itself (see the module docstring)."""
    candidates = [p for p in projects if not p["has_notes"] and not p["skipped"]]
    if not candidates:
        print("Nothing to ask: every discovered project already has notes or is on the "
              "skip list (see `projects` for why).")
        return
    graph_py = Path(g.__file__).resolve()
    print("Discovered projects without vault notes:")
    for p in candidates:
        print(_project_line(p))
    print()
    print("Ask the user which of these should get vault notes (projects/<name>/<name>-overview.md "
          "and projects/<name>/<name>-status.md), and whether there is a project folder not in "
          "this list -- for example one that isn't a git repo, or lives elsewhere -- they also "
          "want included.")
    print("For each project the user picks: read its README, top-level folder layout, and "
          "manifest files (package.json, pyproject.toml, ...) plus recent commit metadata only "
          "(never index code -- see defaults/standards/data-policy.md), then write real notes "
          "(no skeleton/placeholder notes -- see defaults/standards/vault-notes.md), run "
          f'python "{graph_py}" lint, and commit.')
    print(f'For the rest: python "{graph_py}" projects --skip ' +
          " ".join(p["name"] for p in candidates))


def cmd_projects(args) -> None:
    paths = g.default_paths()
    if args.skip or args.unskip:
        current = set(skip_list(paths))
        to_skip = set(args.skip or [])
        to_unskip = set(args.unskip or [])
        added = sorted(to_skip - current)
        removed = sorted(to_unskip & current)
        set_skip_list(paths, sorted((current | to_skip) - to_unskip))
        if added:
            print(f"skipped: {', '.join(added)}")
        if removed:
            print(f"unskipped: {', '.join(removed)}")
        if not added and not removed:
            print("no change")
        return
    projects = load_cached(paths, refresh=args.refresh)
    skip = set(skip_list(paths))
    for p in projects:
        p["skipped"] = p["name"] in skip
    if args.ask:
        _print_ask(projects)
        return
    if args.missing:
        projects = [p for p in projects if not p["has_notes"] and not p["skipped"]]
    if args.json:
        print(json.dumps(projects, indent=2, ensure_ascii=False))
        return
    _print_table(projects)
    if args.missing and projects:
        names = ", ".join(p["name"] for p in projects)
        print(f"\nsuggest asking the user about these with `projects --ask`: {names}")


def register(sub) -> None:
    p = sub.add_parser("projects", help="discover sibling projects and note coverage")
    p.add_argument("--refresh", action="store_true", help="ignore the cache")
    p.add_argument("--json", action="store_true")
    p.add_argument("--missing", action="store_true",
                   help="only projects without notes and not on the skip list")
    p.add_argument("--ask", action="store_true",
                   help="report for the agent: which projects to ask the user about")
    p.add_argument("--skip", nargs="+", metavar="NAME",
                   help="never suggest these project names on this computer")
    p.add_argument("--unskip", nargs="+", metavar="NAME",
                   help="suggest these project names again on this computer")
    p.set_defaults(func=cmd_projects)
