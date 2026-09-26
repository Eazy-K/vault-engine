#!/usr/bin/env python3
"""Self-update for the engine checkout: move to a newer (or older) release tag.

Two channels an engine checkout can be on:
  - stable: HEAD is detached exactly at a SemVer tag (the normal way most
    users run the engine, after `git checkout <tag>`).
  - dev: HEAD is on a branch (the developer's own checkout, usually `main`).
`update` only ever acts on the stable channel; on dev it just explains that
`git pull` + `migrate` is the right move, and leaves the checkout untouched.

This module also wires two passive, best-effort pieces into graph.py:
  - `maybe_check`, called at the end of `reinforce`, refreshes a small cache
    of the latest remote tag (throttled to once per day, network only, never
    touches the working tree).
  - `context_hint`, called from `context`, reads that cache (no network) and
    prints a one-line HTML comment when a newer stable release exists, so an
    agent can offer to run `update`.

This module is loaded by tools/graph.py (see EXTENSIONS) if present; it adds
its own `update` subcommand via register(sub) and never edits graph.py.
Standard library only, matching the engine's own constraint.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import graph as g
import schema as sch

# --- version parsing ---------------------------------------------------------

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(tag: str) -> tuple[int, int, int] | None:
    """SemVer only: "v0.10.0" or "0.10.0" -> (0, 10, 0). None for anything else
    (pre-release/build suffixes, non-numeric tags), so tags compare as tuples
    and v0.10.0 sorts above v0.9.0."""
    if not tag:
        return None
    m = _VERSION_RE.match(tag.strip())
    if not m:
        return None
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


# --- channel detection --------------------------------------------------------

def channel(engine: Path) -> tuple[str, str | None]:
    """("stable", tag) if HEAD is detached exactly at a SemVer tag, ("dev",
    branch) if HEAD is on a branch, ("unknown", None) otherwise (detached at
    something else, or not a git repo at all). Never raises."""
    try:
        branch = subprocess.run(["git", "symbolic-ref", "-q", "--short", "HEAD"], cwd=engine,
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown", None
    if branch.returncode == 0 and branch.stdout.strip():
        return "dev", branch.stdout.strip()
    try:
        tag = subprocess.run(["git", "describe", "--tags", "--exact-match"], cwd=engine,
                             capture_output=True, text=True, encoding="utf-8", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown", None
    if tag.returncode == 0:
        name = tag.stdout.strip()
        if parse_version(name) is not None:
            return "stable", name
    return "unknown", None


# --- settings ------------------------------------------------------------

DEFAULT_SETTINGS = {"check": True}


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}


def _machine_file(paths: "g.Paths") -> Path:
    return paths.data / ".graph" / "machine.json"


def load_settings(paths: "g.Paths") -> dict:
    """Effective update-check settings: vault.config.json, then the per-machine
    override on top (machine values win, key by key). Default check=True."""
    merged = dict(DEFAULT_SETTINGS)
    config = _load_json(paths.config_file).get("updates")
    if isinstance(config, dict):
        merged.update({k: v for k, v in config.items() if v is not None})
    machine = _load_json(_machine_file(paths)).get("updates")
    if isinstance(machine, dict):
        merged.update({k: v for k, v in machine.items() if v is not None})
    return merged


def _due(last: str | None, now: datetime, days: float = 0, hours: float = 0) -> bool:
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (now - last_dt).total_seconds() >= days * 86400 + hours * 3600


# --- remote check + cache -----------------------------------------------------
# The cache lives in the ENGINE folder (not the data repo): it describes this
# checkout, not the vault, and every checkout of the engine is its own working
# tree, so a per-engine, gitignored cache is the right scope.

def _cache_dir(engine: Path) -> Path:
    return engine / ".cache"


def _cache_file(engine: Path) -> Path:
    return _cache_dir(engine) / "update-check.json"


def _load_cache(engine: Path) -> dict:
    return _load_json(_cache_file(engine))


def _save_cache(engine: Path, data: dict) -> None:
    # .cache/ is in the engine's own .gitignore; editing that tracked file here
    # would leave a dirty checkout that `update` then refuses to move.
    try:
        _cache_dir(engine).mkdir(parents=True, exist_ok=True)
        _cache_file(engine).write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                       encoding="utf-8", newline="\n")
    except OSError:
        pass


def remote_latest(engine: Path, timeout: int = 5) -> str | None:
    """Highest SemVer tag on `origin`, read with `ls-remote` (no fetch, never
    touches the working tree). None on any error/timeout/no tags."""
    try:
        out = subprocess.run(["git", "ls-remote", "--tags", "--refs", "origin"], cwd=engine,
                             capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    best: tuple[int, int, int] | None = None
    best_tag: str | None = None
    for line in out.stdout.splitlines():
        _, _, ref = line.partition("refs/tags/")
        tag = ref.strip()
        v = parse_version(tag)
        if v is None:
            continue
        if best is None or v > best:
            best, best_tag = v, tag
    return best_tag


def record_check(engine: Path) -> str | None:
    """Fresh remote check regardless of the throttle; updates the cache on
    success. Never raises; None means the check failed."""
    latest = remote_latest(engine)
    if latest is not None:
        _save_cache(engine, {"latest": latest, "checked": datetime.now().isoformat(timespec="seconds")})
    return latest


def maybe_check(paths: "g.Paths") -> None:
    """Called by the orchestrator at the end of `reinforce`. Only on the stable
    channel, only if settings allow, at most once per 24h. Never raises, prints
    nothing (the result surfaces later via context_hint / doctor)."""
    try:
        settings = load_settings(paths)
        if not settings.get("check", True):
            return
        status, _ref = channel(g.ENGINE)
        if status != "stable":
            return
        cache = _load_cache(g.ENGINE)
        if not _due(cache.get("checked"), datetime.now(), hours=24):
            return
        record_check(g.ENGINE)
    except Exception:
        pass


def context_hint(paths: "g.Paths") -> str | None:
    """Cache-only (no network): a one-line hint for `context` when a newer
    stable release is cached and settings allow checks. None otherwise."""
    try:
        settings = load_settings(paths)
        if not settings.get("check", True):
            return None
        status, _ref = channel(g.ENGINE)
        if status != "stable":
            return None
        latest = _load_cache(g.ENGINE).get("latest")
        if not latest:
            return None
        latest_v = parse_version(latest)
        current_v = parse_version(g.__version__)
        if latest_v is None or current_v is None or latest_v <= current_v:
            return None
        latest_str = ".".join(str(n) for n in latest_v)
        graph_py = g.ENGINE / "tools" / "graph.py"
        return (f"<!-- vault-engine {latest_str} is available (this computer has "
                f"{g.__version__}): ask the user once whether to run "
                f"`python \"{graph_py}\" update` -->")
    except Exception:
        return None


# --- update command: local git plumbing --------------------------------------

def _dirty_engine(engine: Path) -> str | None:
    """git status --porcelain, ignoring the gitignored .cache/ dir. None if clean."""
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=engine,
                             capture_output=True, text=True, encoding="utf-8", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    lines = [l for l in out.stdout.splitlines() if l.strip() and ".cache/" not in l]
    return "\n".join(lines) if lines else None


def _local_tags(engine: Path) -> list[str]:
    try:
        out = subprocess.run(["git", "tag"], cwd=engine, capture_output=True, text=True,
                             encoding="utf-8", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [t.strip() for t in out.stdout.splitlines() if t.strip()]


def _highest_tag(tags: list[str]) -> str | None:
    versioned = [(v, t) for t in tags if (v := parse_version(t)) is not None]
    if not versioned:
        return None
    return max(versioned, key=lambda vt: vt[0])[1]


def _tag_exists(engine: Path, tag: str) -> bool:
    try:
        out = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}"],
                             cwd=engine, capture_output=True, text=True, encoding="utf-8", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def _read_changelog(engine: Path, ref: str) -> str:
    try:
        out = subprocess.run(["git", "show", f"{ref}:CHANGELOG.md"], cwd=engine,
                             capture_output=True, text=True, encoding="utf-8", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


_SECTION_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\][^\n]*$", re.M)


def _changelog_sections(text: str, low: tuple[int, int, int], high: tuple[int, int, int]) -> str:
    """Every section with low < version <= high, in the order they appear."""
    matches = list(_SECTION_RE.finditer(text))
    parts = []
    for i, m in enumerate(matches):
        v = parse_version(m.group(1))
        if v is None or not (low < v <= high):
            continue
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        parts.append(text[start:end].rstrip())
    return "\n\n".join(parts)


def _engine_has_schema(engine: Path, ref: str) -> bool:
    try:
        out = subprocess.run(["git", "show", f"{ref}:tools/schema.py"], cwd=engine,
                             capture_output=True, text=True, encoding="utf-8", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def run_step(engine: Path, data: Path, cmd_args: list[str]) -> subprocess.CompletedProcess:
    """Runs a graph.py subcommand of the (already checked-out) NEW engine, with
    VAULT_DATA pointed at the vault. Factored out so tests can mock it without
    ever running a real subprocess."""
    env = dict(os.environ)
    env["VAULT_DATA"] = str(data)
    return subprocess.run([sys.executable, str(engine / "tools" / "graph.py"), *cmd_args],
                          cwd=engine, env=env, capture_output=True, text=True, encoding="utf-8")


def _print_step_output(result: subprocess.CompletedProcess) -> None:
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.returncode != 0 and result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n")


def _rollback_hint(previous_ref: str | None) -> str:
    graph_py = Path(__file__).resolve().parent / "graph.py"
    return (f"go back with: python \"{graph_py}\" update --to {previous_ref} --yes"
            if previous_ref else "go back with a manual `git checkout` to the previous tag")


def _run_post_checkout(engine: Path, data: Path, target: str, is_upgrade: bool,
                       previous_ref: str | None) -> bool:
    """Runs the new engine's migrate/setup/doctor. Returns True if every step
    that ran succeeded."""
    if is_upgrade and _engine_has_schema(engine, target):
        print("running: migrate --yes")
        result = run_step(engine, data, ["migrate", "--yes"])
        _print_step_output(result)
        if result.returncode != 0:
            print(f"update: migrate failed ({_rollback_hint(previous_ref)})")
            return False

    # --no-env: the engine and data folders did not move, and shell startup files
    # or setx are never changed without the user running setup themselves.
    print("running: setup --yes --no-env")
    result = run_step(engine, data, ["setup", "--yes", "--no-env", "--data", str(data)])
    _print_step_output(result)
    if result.returncode != 0:
        print(f"update: setup failed ({_rollback_hint(previous_ref)})")
        return False

    print("running: doctor")
    result = run_step(engine, data, ["doctor"])
    _print_step_output(result)
    if result.returncode != 0:
        print(f"update: doctor reported problems ({_rollback_hint(previous_ref)})")
        return False
    return True


# --- update command: CI pin ---------------------------------------------------

_CI_PIN_RE = re.compile(r"(uses:\s*[\w.\-]+/vault-engine)@([^\s]+)")

# The header comment that 0.1.0 and 0.2.0 shipped in vault.yml; the engine has
# been public since, so it is replaced along with the pin.
_OLD_CI_COMMENT = ("# PR, so commits made from the cloud or a phone are checked too. The engine is a\n"
                   "# private action: in the engine repo, Settings > Actions > Access must allow\n"
                   "# repositories of the same owner.\n")
_NEW_CI_COMMENT = ("# PR, so commits made from the cloud or a phone are checked too. The engine is\n"
                   "# public, so this works as-is. If you instead use a private fork of the engine,\n"
                   "# do this once: in that repo, Settings > Actions > General > Access must allow\n"
                   "# repositories of the same owner.\n")


def _rewrite_ci_pin(data: Path, new_ref: str) -> bool:
    """Move the data repo's CI pin (`uses: <owner>/vault-engine@<ref>` in
    <data>/.github/workflows/vault.yml) to the newly installed engine ref, so the
    guard workflow always matches the engine version in use. No-op if the
    workflow file is missing or doesn't have that line. Returns True if the file
    was changed (the caller in the data repo must commit and push it)."""
    path = data / ".github" / "workflows" / "vault.yml"
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    new_text = text.replace(_OLD_CI_COMMENT, _NEW_CI_COMMENT)
    m = _CI_PIN_RE.search(new_text)
    repin = bool(m) and m.group(2) != new_ref
    if repin:
        new_text = new_text[:m.start(2)] + new_ref + new_text[m.end(2):]
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8", newline="\n")
    if repin:
        print(f"updated CI pin in {path}: @{m.group(2)} -> @{new_ref} "
              "(commit and push this change in the data repo)")
    else:
        print(f"updated the outdated comment in {path} "
              "(commit and push this change in the data repo)")
    return True


# --- update command: AGENTS.md ------------------------------------------------
# The template ends with a revision line (TEMPLATE_MARKER_RE). A vault's AGENTS.md
# that keeps that line after being translated or edited counts as based on that
# revision, so only a newer template brings up a diff; a file without the line
# is compared by content alone.

TEMPLATE_MARKER_RE = re.compile(r"<!--\s*vault-engine AGENTS\.md template:\s*(\d+)\b")


def _template_revision(text: str) -> int | None:
    m = TEMPLATE_MARKER_RE.search(text)
    return int(m.group(1)) if m else None


def _read_optional(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def agents_md_status(engine: Path, data: Path) -> tuple[str, str]:
    """How <data>/AGENTS.md relates to the engine's templates/AGENTS.md, as
    (state, message). States:
      - "current": same text as the template;
      - "customized": different text (translated or edited) but it keeps the
        marker line of the template's current revision;
      - "stale": behind the template (older or no marker, or different text
        when the template has no marker);
      - "newer": follows a newer template revision than this engine has;
      - "missing": the vault has no AGENTS.md;
      - "unknown": the engine has no template to compare with.
    Shared by `update` and `doctor`; never raises."""
    template = _read_optional(engine / "templates" / "AGENTS.md")
    if template is None:
        return "unknown", "the engine has no templates/AGENTS.md"
    current = _read_optional(data / "AGENTS.md")
    if current is None:
        return "missing", f"{data / 'AGENTS.md'} does not exist"
    if current == template:
        return "current", "AGENTS.md matches this engine's template"
    t_rev, d_rev = _template_revision(template), _template_revision(current)
    if t_rev is not None and d_rev is not None:
        if d_rev == t_rev:
            return "customized", ("AGENTS.md is edited or translated, based on the current "
                                  f"template ({t_rev})")
        if d_rev > t_rev:
            return "newer", (f"AGENTS.md follows template {d_rev}, newer than this engine's ({t_rev}): "
                             "another computer runs a newer engine")
    yours = f"yours: {d_rev}" if d_rev is not None else "yours has no template line"
    theirs = f"template {t_rev}, " if t_rev is not None else ""
    return "stale", f"AGENTS.md is behind this engine's template ({theirs}{yours})"


def _agents_diff(engine: Path, data: Path) -> str | None:
    template = engine / "templates" / "AGENTS.md"
    target = data / "AGENTS.md"
    t_text = _read_optional(template) or ""
    d_text = _read_optional(target) or ""
    if t_text == d_text:
        return None
    diff = difflib.unified_diff(d_text.splitlines(keepends=True), t_text.splitlines(keepends=True),
                                fromfile=str(target), tofile=str(template))
    return "".join(diff)


def _write_agents(engine: Path, data: Path) -> None:
    template_text = (engine / "templates" / "AGENTS.md").read_text(encoding="utf-8")
    (data / "AGENTS.md").write_text(template_text, encoding="utf-8", newline="\n")
    print(f"wrote {data / 'AGENTS.md'} (commit it in the data repo)")


def _handle_agents_md(engine: Path, data: Path, args) -> None:
    """Compares the vault's AGENTS.md with the engine's template (whatever the
    engine moved from). Without --apply-agents a diff is shown only when the file
    is behind the template, so a translated file that keeps the marker line stays
    quiet; --apply-agents replaces any file that differs, after showing the diff."""
    state, message = agents_md_status(engine, data)
    if state in ("current", "unknown"):
        if args.apply_agents:
            print(message if state == "unknown" else "AGENTS.md already matches the template")
        return
    if state == "newer":
        print(message + ("; not replaced" if args.apply_agents else ""))
        return
    if state == "customized" and not args.apply_agents:
        return
    print(f"\n{message}. Diff from it to the template:\n")
    print(_agents_diff(engine, data))
    if args.apply_agents:
        _write_agents(engine, data)
        return
    if not args.yes and g.stdin_is_interactive():
        try:
            answer = input("Replace AGENTS.md with the template? [y/N]: ").strip().lower()
        except EOFError:
            answer = ""
        if answer.startswith("y"):
            _write_agents(engine, data)
            return
    print("review the diff above; rerun with --apply-agents to replace "
          "(a translated copy should keep the template line at the end)")


def _agents_md_step(engine: Path, args) -> None:
    """The AGENTS.md check on paths where the engine did not move (already up to
    date, dev or unknown channel): a hand-upgraded vault still gets the diff, and
    --apply-agents works there too."""
    try:
        data = g.default_paths().data
    except SystemExit:
        if args.apply_agents:
            raise
        return
    _handle_agents_md(engine, data, args)


# --- update command ------------------------------------------------------------

def cmd_update(args) -> None:
    engine = g.ENGINE
    current_v = parse_version(g.__version__)
    status, ref = channel(engine)

    if args.check:
        latest = remote_latest(engine)
        print(f"current: v{g.__version__}")
        print(f"channel: {status}" + (f" ({ref})" if ref else ""))
        print(f"latest:  {latest or 'unknown (could not reach origin)'}")
        return

    if status == "dev":
        print(f"this is a development checkout (branch {ref}). "
              "Update it with: git pull, then `python tools/graph.py migrate` (if available). "
              "The engine was not changed.")
        _agents_md_step(engine, args)
        return
    if status != "stable":
        if args.apply_agents:
            _agents_md_step(engine, args)
        sys.exit("update: channel is unknown (not a git checkout of the engine); "
                 "update this manually (git clone / re-download).")

    dirty = _dirty_engine(engine)
    if dirty:
        sys.exit("update: the engine checkout has local changes; commit, stash or discard "
                 f"them first:\n{dirty}")

    fetch = subprocess.run(["git", "fetch", "--tags", "--quiet", "origin"], cwd=engine,
                           capture_output=True, text=True, encoding="utf-8")
    if fetch.returncode != 0:
        sys.exit(f"update: git fetch failed:\n{fetch.stderr.strip()}")

    target = args.to or _highest_tag(_local_tags(engine))
    if not target:
        sys.exit("update: no version tags found")
    if not _tag_exists(engine, target):
        sys.exit(f"update: unknown tag {target}")
    target_v = parse_version(target)
    if target_v is None:
        sys.exit(f"update: {target} is not a recognizable version tag (expected vX.Y.Z)")

    if target == ref or target_v == current_v:
        print(f"already up to date (v{g.__version__})")
        # A vault upgraded by hand (before `update` existed) still pins its CI
        # to an old ref; bring it in line here too, as doctor suggests.
        try:
            _rewrite_ci_pin(g.default_paths().data, ref or target)
        except SystemExit:
            pass
        _agents_md_step(engine, args)
        return

    is_upgrade = current_v is not None and target_v > current_v
    print(f"{'upgrade' if is_upgrade else 'downgrade'}: v{g.__version__} -> {target}")
    if is_upgrade:
        changelog = _changelog_sections(_read_changelog(engine, target), current_v or (0, 0, 0), target_v)
        if changelog:
            print(changelog)
            if "Upgrade notes" in changelog:
                print("\nNOTE: read the 'Upgrade notes' section(s) above before proceeding.")
        else:
            print("(no CHANGELOG.md sections found for this range)")
    else:
        print("this is a downgrade.")

    paths = g.default_paths()
    if not is_upgrade:
        target_schema = sch.schema_of_engine_ref(engine, target)
        vault_schema = sch.read_schema(paths)
        if target_schema is not None and target_schema < vault_schema:
            sys.exit(f"update: refusing -- this vault was migrated to schema {vault_schema}, "
                     f"but {target} only supports schema {target_schema}; downgrading would "
                     "corrupt it. Update the engine on this computer instead.")

    print("after switching: migrate (one commit in the data repo), setup (only the engine's "
          "own subagent files in ~/.claude/agents and missing routing files, never shell "
          "startup files or environment variables) and doctor; each change is listed.")
    if not args.yes:
        if g.stdin_is_interactive():
            try:
                answer = input(f"Proceed with the {'upgrade' if is_upgrade else 'downgrade'} "
                               f"to {target}? [y/N]: ").strip().lower()
            except EOFError:
                print()
                sys.exit("update: no answer (input ended), nothing changed; "
                         "rerun with --yes to apply.")
            if not answer.startswith("y"):
                print("update: cancelled")
                return
        else:
            sys.exit("update: rerun with --yes to apply (non-interactive session).")

    checkout = subprocess.run(["git", "checkout", "--quiet", target], cwd=engine,
                              capture_output=True, text=True, encoding="utf-8")
    if checkout.returncode != 0:
        sys.exit(f"update: git checkout failed:\n{checkout.stderr.strip()}")
    print(f"checked out {target}")
    _rewrite_ci_pin(paths.data, target)

    ok = _run_post_checkout(engine, paths.data, target, is_upgrade, ref)
    _handle_agents_md(engine, paths.data, args)
    if not ok:
        sys.exit(1)


def register(sub) -> None:
    """Called by graph.py's extension mechanism (see EXTENSIONS)."""
    p = sub.add_parser("update", help="update the engine checkout to a newer (or older) release tag")
    p.add_argument("--check", action="store_true", help="print current/channel/latest and exit")
    p.add_argument("--to", help="target tag (default: highest local tag after fetch)")
    p.add_argument("--yes", action="store_true", help="apply without an interactive confirmation")
    p.add_argument("--apply-agents", action="store_true",
                   help="replace the data repo's AGENTS.md with the engine's template after "
                        "showing the diff; also works when the engine is already up to date")
    p.set_defaults(func=cmd_update)
