"""Project discovery: find the user's repos next to the engine so the agent
can suggest notes for projects that don't have any yet.

Only lightweight metadata is read (repo name, README first line, file
extension counts, git remote host, last commit date). Code is never indexed
and nothing leaves the machine; results are cached under the data folder,
which is gitignored (.graph/projects.json).
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
            if resolved in skip:
                continue
            if _excluded(entry.name, exclude_patterns):
                continue
            if (entry / ".vaultignore").exists():
                continue
            notes_root = paths.data / "projects" / entry.name
            has_notes = notes_root.is_dir() and any(notes_root.rglob("*.md"))
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


def _cache_path(paths: g.Paths) -> Path:
    return paths.data / ".graph" / "projects.json"


def load_cached(paths: g.Paths, refresh: bool = False) -> list[dict]:
    """Reuse the cache when it is fresh (< CACHE_TTL); rebuild otherwise."""
    cache_file = _cache_path(paths)
    # The roots are part of the key: a cache built for other roots (another engine
    # checkout, an edited config) must not be reused.
    roots = [str(r) for r in g.project_roots(paths)]
    if not refresh and cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < CACHE_TTL:
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and cached.get("roots") == roots:
                    return cached["projects"]
            except (OSError, ValueError, KeyError):
                pass
    results = discover(paths)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"roots": roots, "projects": results}, indent=2,
                                     ensure_ascii=False), encoding="utf-8", newline="\n")
    return results


def missing_projects(paths: g.Paths) -> list[str]:
    """Names of discovered projects that have no notes yet, from the cache."""
    return [p["name"] for p in load_cached(paths) if not p["has_notes"]]


def _print_table(projects: list[dict]) -> None:
    if not projects:
        print("No projects found.")
        return
    for p in projects:
        notes = "yes" if p["has_notes"] else "no"
        langs = ",".join(p["languages"]) or "-"
        commit = p["last_commit"] or "-"
        readme = p["readme"] or ""
        print(f"{p['name']:<24} notes:{notes:<4} {langs:<18} {commit:<20} {readme}")


def cmd_projects(args) -> None:
    paths = g.default_paths()
    projects = load_cached(paths, refresh=args.refresh)
    if args.missing:
        projects = [p for p in projects if not p["has_notes"]]
    if args.json:
        print(json.dumps(projects, indent=2, ensure_ascii=False))
        return
    _print_table(projects)
    if args.missing and projects:
        names = ", ".join(p["name"] for p in projects)
        print(f"\nsuggest creating projects/<name>/<name>-overview.md and "
              f"-status.md for: {names}")


def register(sub) -> None:
    p = sub.add_parser("projects", help="discover sibling projects and note coverage")
    p.add_argument("--refresh", action="store_true", help="ignore the cache")
    p.add_argument("--json", action="store_true")
    p.add_argument("--missing", action="store_true", help="only projects without notes")
    p.set_defaults(func=cmd_projects)
