"""Onboarding: scaffold a new data repo, wire it to this engine, and check the
setup's health.

Optional extension module: graph.py imports this if present (see EXTENSIONS
in graph.py) and calls register(sub) with its argparse subparsers object.
Stdlib only, like graph.py itself.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

import graph as g

FEEDBACK_LEVELS = ("off", "metrics", "reports")
FEEDBACK_MODES = ("auto", "ask")

# Shown to the user when asking interactively; one line per level.
FEEDBACK_EXPLAIN = {
    "off": "off: nothing is sent",
    "metrics": "metrics: only anonymous counters are sent",
    "reports": "reports: counters + filtered template friction reports are sent",
}

USER_LEVEL_CODEX_LINE = "Context and rules are in {agents}, read it first and follow it."

SKELETON_MARKER = "<!-- vault:skeleton -->"


# --- shared helpers ------------------------------------------------------------

def _ask(prompt: str, default: str, strict: bool = False) -> str:
    """Prompt with a shown default; EOF (non-interactive stdin) falls back to it,
    or, with strict=True, is raised so the caller can stop without writing."""
    try:
        raw = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        if strict:
            raise
        return default
    return raw or default


def _ask_yn(prompt: str, default: bool) -> bool:
    shown = "Y/n" if default else "y/N"
    try:
        raw = input(f"{prompt} [{shown}]: ").strip().lower()
    except EOFError:
        return default
    if not raw:
        return default
    return raw.startswith("y") or raw.startswith("e")


def _is_interactive(args: argparse.Namespace) -> bool:
    return not args.yes and g.stdin_is_interactive()


def _is_git_repo(path: Path) -> bool:
    try:
        out = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=path,
                              capture_output=True, text=True, encoding="utf-8")
    except OSError:
        return False
    return out.returncode == 0


def _enclosing_repo(path: Path) -> Path | None:
    """Top level of the git work tree that holds `path`, or would hold it once
    created (its nearest existing parent is checked then); None outside git.
    Unlike _is_git_repo, this tells a repo's own folder apart from a subfolder."""
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            return None
        probe = probe.parent
    if not probe.is_dir():
        return None
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=probe,
                              capture_output=True, text=True, encoding="utf-8")
    except OSError:
        return None
    top = out.stdout.strip()
    return Path(top) if out.returncode == 0 and top else None


def _append_if_missing(path: Path, line: str) -> None:
    """Append `line` to `path` unless already present; never rewrites existing content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if line in text:
        print(f"  ok: {path} already has the line")
        return
    with path.open("a", encoding="utf-8", newline="\n") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write(line + "\n")
    print(f"  appended: {path}")


# --- init ------------------------------------------------------------------------

def _copy_templates(target: Path) -> tuple[list[str], list[str]]:
    """Copy templates/ into target, never overwriting. Returns (copied, skipped)."""
    templates = g.ENGINE / "templates"
    copied, skipped = [], []
    for src in sorted(templates.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(templates)
        dest = target / rel
        if dest.exists():
            skipped.append(str(rel))
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        text = _read_text(dest)
        if ENGINE_REPO_PLACEHOLDER in text:
            _fill_engine_repo(dest)
        if ENGINE_REF_PLACEHOLDER in _read_text(dest):
            _fill_engine_ref(dest)
        copied.append(str(rel))
    return copied, skipped


ENGINE_REPO_PLACEHOLDER = "{{ENGINE_REPO}}"
ENGINE_REF_PLACEHOLDER = "{{ENGINE_REF}}"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def _fill_engine_repo(path: Path) -> None:
    """Templates (the CI workflow) name the engine repo; it is derived from this
    engine's own remote so every user's data repo points at their engine."""
    try:
        import feedback  # same tools/ folder; owns the remote parsing
        repo = feedback._derive_repo()
    except Exception:
        repo = None
    if repo:
        text = _read_text(path).replace(ENGINE_REPO_PLACEHOLDER, repo)
        path.write_text(text, encoding="utf-8", newline="\n")
    else:
        print(f"  note: set the engine repo (owner/name) in {path.name}: no GitHub remote found")


def _engine_ref() -> str:
    """Ref to pin the data repo's CI workflow to: the installed engine's release
    tag when this checkout is on the stable channel, else `main` (dev/unknown
    channel, or update.py unavailable). Guarded import so onboarding.py never
    hard-depends on update.py."""
    update = sys.modules.get("update")
    if update is None:
        try:
            import update as update_mod
        except Exception:
            return "main"
        update = update_mod
    try:
        status, ref = update.channel(g.ENGINE)
    except Exception:
        return "main"
    return ref if status == "stable" and ref else "main"


def _fill_engine_ref(path: Path) -> None:
    """Templates (the CI workflow) pin the engine action to a ref; fill it with
    the installed engine's release tag (or `main` off the stable channel)."""
    text = _read_text(path).replace(ENGINE_REF_PLACEHOLDER, _engine_ref())
    path.write_text(text, encoding="utf-8", newline="\n")


def _load_existing_config(path: Path) -> dict:
    """The JSON object at `path`, or {} if missing/unreadable -- never raises,
    so init can always fall back to writing a fresh one. Works for both
    vault.config.json and machine.json (same small-JSON-object shape)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_machine_json(target: Path, updates: dict) -> Path:
    """Merge `updates` into <target>/.graph/machine.json: gitignored and
    per-computer, unlike vault.config.json which every computer shares."""
    path = target / ".graph" / "machine.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_existing_config(path)
    data.update(updates)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8", newline="\n")
    return path


def _has_commits(repo: Path) -> bool:
    out = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=repo,
                          capture_output=True, text=True, encoding="utf-8")
    return out.returncode == 0


def _has_git_identity(repo: Path) -> bool:
    """True if `git commit` would resolve an author here (local, global or
    system config all count -- only `git config <key>` without --local sees
    all of them)."""
    for key in ("user.email", "user.name"):
        out = subprocess.run(["git", "config", key], cwd=repo,
                              capture_output=True, text=True, encoding="utf-8")
        if out.returncode != 0 or not out.stdout.strip():
            return False
    return True


def _initial_commit(target: Path) -> None:
    """Commit everything for a brand-new vault, if git has an identity to
    commit with. core.hooksPath (set just before this runs) points at the
    engine's guard hook, which the commit goes through like any other."""
    if not _has_git_identity(target):
        print("  git: no user.name/user.email configured; set them in this data repo "
              "and commit yourself, e.g.:")
        print(f'    git -C "{target}" add -A')
        print(f'    git -C "{target}" commit -m "chore: initialize vault"')
        return
    subprocess.run(["git", "add", "-A"], cwd=target, check=True, capture_output=True)
    commit = subprocess.run(["git", "commit", "-m", "chore: initialize vault"], cwd=target,
                             capture_output=True, text=True, encoding="utf-8")
    if commit.returncode == 0:
        print("  git: initial commit created (chore: initialize vault)")
    else:
        print("  git: initial commit failed:")
        print(f"    {commit.stderr.strip()}")
        print("  hint: fix the issue above, then `git add -A && git commit` yourself")


def cmd_init(args: argparse.Namespace) -> None:
    target = Path(args.dir).expanduser().resolve()
    if target == g.ENGINE.resolve() or g.ENGINE.resolve() in target.parents:
        sys.exit(f"refusing to create a data repo inside the engine ({g.ENGINE})")
    enclosing = _enclosing_repo(target)
    if enclosing is not None and not (target.is_dir() and _same_path(str(enclosing), str(target))):
        # Its hooks setting and commits would land in that other repo (e.g. turning off husky).
        sys.exit(f"refusing to create a data repo inside another git repo ({enclosing}): "
                 "init would change that repo's settings. Pick a folder outside it, "
                 "for example next to it (or run `git init` in the folder first to make it "
                 "a repo of its own).")

    config_path = target / "vault.config.json"
    existing = _load_existing_config(config_path)
    if existing:
        # Only a vault that already has a config can conflict with a newer
        # engine's layout; a brand-new one has nothing to protect yet.
        g._require_writable(g.Paths(g.ENGINE, target))

    target.mkdir(parents=True, exist_ok=True)
    copied, skipped = _copy_templates(target)
    for rel in copied:
        print(f"  created: {rel}")
    for rel in skipped:
        print(f"  skipped (already exists): {rel}")
    if existing and copied:
        print("  note: this data repo already existed; the files marked created came back from "
              "the templates. Delete them again if you had removed them on purpose. To set only "
              "this computer's settings, use `machine` instead of `init`.")

    repo_existed = enclosing is not None
    if not repo_existed:
        subprocess.run(["git", "init"], cwd=target, check=True, capture_output=True)
        # CI only runs on main; `git init` alone may default to master depending
        # on the local git/global config, so pin it explicitly. Works on every
        # git version, unlike `git init -b main` (added in 2.28).
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=target,
                        check=True, capture_output=True)
        print("  git repo initialised (branch: main)")
    # Never touch a repo that already had commits before this run.
    had_commits = _has_commits(target) if repo_existed else False
    hooks_path = (g.ENGINE / "tools" / "hooks").as_posix()
    previous_hooks = subprocess.run(["git", "config", "--local", "core.hooksPath"], cwd=target,
                                    capture_output=True, text=True, encoding="utf-8").stdout.strip()
    subprocess.run(["git", "config", "core.hooksPath", hooks_path], cwd=target,
                    check=True, capture_output=True)
    was = (f" (was {previous_hooks})"
           if previous_hooks and not _same_path(previous_hooks, hooks_path) else "")
    print(f"  core.hooksPath = {hooks_path}{was}")

    interactive = _is_interactive(args)
    existing_feedback = existing.get("feedback") if isinstance(existing.get("feedback"), dict) else {}

    # project_roots is per-computer, never written into the shared vault.config.json
    # (every computer using this vault reads that file). An explicit flag, or an
    # interactive answer that differs from the default, is saved to this computer's
    # machine.json instead. No flag / no explicit answer -> nothing is written, and
    # graph.project_roots() falls back to the engine's own parent folder (or, for an
    # already-configured vault, to whatever vault.config.json still has, kept only
    # for backward compatibility).
    machine_roots = None
    if args.project_root:
        machine_roots = args.project_root
    elif not existing:
        default = str(g.ENGINE.parent)
        if interactive:
            raw = _ask("Project root folder (comma-separated for multiple paths)", default)
            answered = [p.strip() for p in raw.split(",") if p.strip()]
            if answered and answered != [default]:
                machine_roots = answered
    if machine_roots:
        machine_path = _write_machine_json(target, {"project_roots": machine_roots})
        print(f"  wrote: {machine_path.relative_to(target).as_posix()} "
              "(project roots are per computer, not shared via git)")
        if existing:
            print("  hint: `machine --project-root <path>` changes only this file")

    feedback_level = args.feedback
    if not feedback_level:
        if "level" in existing_feedback:
            feedback_level = existing_feedback["level"]
        elif interactive:
            for line in FEEDBACK_EXPLAIN.values():
                print(f"  {line}")
            feedback_level = _ask("Feedback level (off/metrics/reports)", "off")
        else:
            feedback_level = "off"  # opt-in only
    if feedback_level not in FEEDBACK_LEVELS:
        sys.exit(f"invalid feedback level: {feedback_level}")

    feedback_mode = args.feedback_mode
    if not feedback_mode:
        if "mode" in existing_feedback:
            feedback_mode = existing_feedback["mode"]
        elif interactive:
            feedback_mode = _ask("Feedback mode (auto/ask)", "ask")
        else:
            feedback_mode = "ask"
    if feedback_mode not in FEEDBACK_MODES:
        sys.exit(f"invalid feedback mode: {feedback_mode}")

    # config = dict(existing) carries forward any project_roots an older engine
    # version wrote there; init itself never adds or overwrites that key.
    config = dict(existing)
    config["feedback"] = {"level": feedback_level, "mode": feedback_mode}
    if "schema" not in config:
        schema = sys.modules.get("schema")
        config["schema"] = schema.SCHEMA_VERSION if schema is not None else 1
    config_file = target / "vault.config.json"
    config_file.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8", newline="\n")
    print(f"  wrote: {config_file.relative_to(target).as_posix()}")

    if not had_commits:
        _initial_commit(target)

    graph_py = g.ENGINE / "tools" / "graph.py"
    print("\nNext steps:")
    print(f"  1. python \"{graph_py}\" setup --data \"{target}\"")
    print("  2. fill in your profile with a few questions:")
    print(f"     agent: python \"{graph_py}\" onboard --questions, ask the user, then")
    print(f"            python \"{graph_py}\" onboard --answers <answers.json> --data \"{target}\"")
    print(f"     terminal: python \"{graph_py}\" onboard --data \"{target}\"")


# --- setup -----------------------------------------------------------------------

def _set_user_env_var(name: str, value: str) -> None:
    """Persist a user-level env var on Windows. Isolated so tests can mock it
    without ever touching the real environment."""
    subprocess.run(["setx", name, value], check=True, capture_output=True)


def _user_env_var(name: str) -> str | None:
    """The value persisted for the user on Windows (what setx wrote). Processes
    started before setx, such as an open terminal or agent, don't see it."""
    if not _is_windows():
        return None
    return g.user_env_var(name)


def _same_path(a: str, b: str) -> bool:
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except OSError:
        return False


def _is_windows() -> bool:
    # A function, not a bare os.name check, so tests can pretend to be Windows
    # without patching os.name (which breaks pathlib on other systems).
    return os.name == "nt"


# Marks the block this tool owns inside a shell rc file, so a rerun replaces
# it in place instead of appending duplicates.
RC_BLOCK_START = "# >>> vault-engine >>>"
RC_BLOCK_END = "# <<< vault-engine <<<"


def _shell_name() -> str:
    """Basename of the user's login shell, e.g. 'zsh', 'bash', 'fish'."""
    return Path(os.environ.get("SHELL", "")).name


def _rc_file_for_shell(shell: str, platform: str, home: Path) -> Path:
    """Pure so it's unit-testable without touching the real home or $SHELL."""
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "bash":
        return home / (".bash_profile" if platform == "darwin" else ".bashrc")
    if shell == "fish":
        return home / ".config" / "fish" / "config.fish"
    return home / ".profile"  # unknown/plain sh: the most portable fallback


def _fish_quote(value: str) -> str:
    # fish single quotes only recognise \\ and \' as escapes; shlex.quote's
    # POSIX-style '\'' trick isn't valid fish syntax.
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _render_rc_block(pairs: list[tuple[str, str]], shell: str) -> str:
    """The delimited block written into the rc file, newline-terminated."""
    lines = [RC_BLOCK_START]
    for name, value in pairs:
        if shell == "fish":
            lines.append(f"set -gx {name} {_fish_quote(value)}")
        else:
            lines.append(f"export {name}={shlex.quote(value)}")
    lines.append(RC_BLOCK_END)
    return "\n".join(lines) + "\n"


def _upsert_block(text: str, block: str) -> str:
    """Replace an existing vault-engine block in `text`, or append `block` if
    there isn't one. Pure text-in, text-out so it's easy to unit test."""
    start = text.find(RC_BLOCK_START)
    if start != -1:
        end = text.find(RC_BLOCK_END, start)
        if end != -1:
            end += len(RC_BLOCK_END)
            if text[end:end + 1] == "\n":
                end += 1
            return text[:start] + block + text[end:]
    if text and not text.endswith("\n"):
        text += "\n"
    if text:
        text += "\n"
    return text + block


def _rc_block_present(path: Path, name: str) -> bool:
    """True if `path` has a vault-engine block that mentions `name` (used by
    doctor to tell 'set in the rc file but not in this process' from unset)."""
    if not path.exists():
        return False
    text = _read_text(path)
    start = text.find(RC_BLOCK_START)
    if start == -1:
        return False
    end = text.find(RC_BLOCK_END, start)
    return end != -1 and name in text[start:end]


def _write_rc_block(path: Path, pairs: list[tuple[str, str]], shell: str) -> bool:
    """Write/replace the vault-engine block in `path`. Returns True if the
    file's content changed (False when a rerun found nothing to update)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    new_text = _upsert_block(text, _render_rc_block(pairs, shell))
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8", newline="\n")
    return True


def _setup_env(data: Path, interactive: bool, assume_yes: bool = False) -> None:
    pairs = [("VAULT_ENGINE", str(g.ENGINE)), ("VAULT_DATA", str(data))]
    pending = [(n, v) for n, v in pairs if not (os.environ.get(n) and _same_path(os.environ[n], v))]
    for name, value in pairs:
        if (name, value) not in pending:
            print(f"  ok: {name} already set")
    if _is_windows():
        # A rerun in the same session: setx already saved it, only this process
        # (started before) can't see it yet.
        still_pending = []
        for name, value in pending:
            saved = _user_env_var(name)
            if saved and _same_path(saved, value):
                print(f"  ok: {name} already saved for your user "
                      "(restart terminals and agents to load it)")
            else:
                still_pending.append((name, value))
        pending = still_pending
    if not pending:
        return
    if _is_windows():
        do_it = True
        if interactive:
            do_it = _ask_yn("Set environment variables (persisted via setx)?", True)
        if do_it:
            for name, value in pending:
                _set_user_env_var(name, value)
                print(f"  set (setx): {name}={value}")
            print("  Restart open terminals and agents (Claude Code, Codex): "
                  "they only see the new values after a restart.")
        else:
            for name, value in pending:
                print(f"  export {name}={value}")
    else:
        rc_path = _rc_file_for_shell(_shell_name(), sys.platform, Path.home())
        is_tty = g.stdin_is_interactive()
        if is_tty and not assume_yes:
            do_it = _ask_yn(f"Write these to {rc_path}?", True)
        else:
            do_it = assume_yes or is_tty
        if do_it:
            changed = _write_rc_block(rc_path, pairs, _shell_name())
            if changed:
                print(f"  written: {rc_path}")
                print("  Restart open terminals and agents (Claude Code, Codex): "
                      "they only see the new values after a restart.")
            else:
                print(f"  ok: {rc_path} already set")
        else:
            print("  Add the following lines to your shell rc file:")
            for name, value in pending:
                print(f"    export {name}={value}")


def _blob_id(content: bytes) -> str:
    """The id git gives a file with this content (LF line endings, as stored)."""
    content = content.replace(b"\r\n", b"\n")
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def _shipped_blob_ids(rel: str) -> set[str]:
    """Ids of every version of the engine file `rel` in this checkout's history.
    Empty when the engine is not a git checkout (then nothing counts as ours)."""
    try:
        out = subprocess.run(["git", "log", "--root", "--format=", "--raw", "--no-abbrev", "--", rel],
                              cwd=g.ENGINE, capture_output=True, text=True, encoding="utf-8")
    except OSError:
        return set()
    ids = set()
    for line in out.stdout.splitlines() if out.returncode == 0 else []:
        fields = line.split()
        if line.startswith(":") and len(fields) >= 4:
            ids.update(f for f in fields[2:4] if f.strip("0"))
    return ids


def _backup_path(path: Path) -> Path:
    backup = path.with_name(path.name + ".bak")
    n = 1
    while backup.exists():
        n += 1
        backup = path.with_name(f"{path.name}.bak{n}")
    return backup


def _setup_agents(interactive: bool = False) -> None:
    """Copy the engine's subagent files into ~/.claude/agents. A file there that
    is neither the engine's current version nor an earlier one is the user's own
    (or edited): it is only replaced after a yes in a terminal, with a backup."""
    src_dir = g.ENGINE / "tools" / "claude-agents"
    dest_dir = Path.home() / ".claude" / "agents"
    if not src_dir.is_dir():
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(src_dir.glob("*.md")):
        dest = dest_dir / src.name
        content = src.read_text(encoding="utf-8")
        if not dest.exists():
            dest.write_text(content, encoding="utf-8", newline="\n")
            print(f"  copied: {dest}")
            continue
        current = dest.read_bytes()
        if current.replace(b"\r\n", b"\n") == content.encode("utf-8"):
            print(f"  unchanged: {dest}")
            continue
        if _blob_id(current) in _shipped_blob_ids(f"tools/claude-agents/{src.name}"):
            dest.write_text(content, encoding="utf-8", newline="\n")
            print(f"  updated: {dest}")
            continue
        if interactive and _ask_yn(f"{dest} differs from the engine's version (your own or "
                                   "edited). Replace it? A copy is kept", False):
            backup = _backup_path(dest)
            shutil.copy2(dest, backup)
            dest.write_text(content, encoding="utf-8", newline="\n")
            print(f"  replaced: {dest} (your version: {backup})")
            continue
        print(f"  skipped (your own or edited version, kept): {dest}")
        print("    to install the engine's version, run setup in a terminal (it asks and keeps "
              "a copy), or rename that file and rerun setup")


MACHINE_NAME_EXPLAIN = ("Learned links are saved per computer in .graph/learned/<name>.json, "
                        "which is committed and pushed with your notes. Pick a neutral name "
                        "(not your name or employer), e.g. laptop or pc-2.")


def _neutral_machine_name() -> str:
    return "pc-" + secrets.token_hex(2)


def _is_data_repo(data: Path) -> bool:
    return (data / "vault.config.json").exists() or (data / "AGENTS.md").exists()


def _machine_json(data: Path) -> dict:
    return _load_existing_config(data / ".graph" / "machine.json")


def _learned_file(data: Path, name: str) -> Path:
    return g.Paths(g.ENGINE, data).learned_dir / f"{name}.json"


def _rename_hint(data: Path, old: str, new: str) -> None:
    """Existing learned files are never renamed automatically (another computer
    may push to the same name); say how to carry one over."""
    if old != new and _learned_file(data, old).exists():
        print(f"  note: .graph/learned/{old}.json keeps its name and still counts. To keep "
              f"adding to it, rename it: git -C \"{data}\" mv .graph/learned/{old}.json "
              f".graph/learned/{new}.json (then commit)")


def _setup_machine(data: Path, interactive: bool) -> None:
    """Give this computer a name for its learned-links file, stored in the
    gitignored .graph/machine.json, so the hostname stays out of the vault. A
    computer that already has a learned file under its hostname keeps it."""
    if not _is_data_repo(data):
        print(f"  skipped (not a data repo): {data}")
        return
    env_name = g.sanitize_machine_name(os.environ.get("VAULT_MACHINE", ""))
    if env_name:
        print(f"  ok: {env_name} (from VAULT_MACHINE)")
        return
    saved = _machine_json(data).get("machine")
    if isinstance(saved, str) and g.sanitize_machine_name(saved):
        print(f"  ok: {g.sanitize_machine_name(saved)} (.graph/machine.json)")
        return
    host = g.machine_name()  # no VAULT_MACHINE, no saved name: the hostname
    host_used = _learned_file(data, host).exists()
    default = host if host_used else _neutral_machine_name()
    name = default
    if interactive:
        print(f"  {MACHINE_NAME_EXPLAIN}")
        raw = _ask("Name for this computer", default)
        answer = g.sanitize_machine_name(raw)
        if answer and not g.scan_line(raw):
            name = answer
        else:
            print(f"  using {default}: the answer was empty or looked like personal data")
    if name == host and host_used:
        print(f"  kept: .graph/learned/{host}.json (named after this computer's hostname). "
              "For a neutral name: machine --name <name>")
        return
    _write_machine_json(data, {"machine": name})
    print(f"  wrote: .graph/machine.json (machine name {name}, "
          f"learned links go to .graph/learned/{name}.json)")
    _rename_hint(data, host, name)


def _routing_line(data: Path) -> str:
    return f"@{data.as_posix()}/AGENTS.md"


def _routes_to(claude_md: Path, data: Path) -> bool:
    """True if any @import in claude_md resolves to <data>/AGENTS.md; a relative
    import such as `@vault/AGENTS.md` written by hand counts as well."""
    target = (data / "AGENTS.md").resolve()
    for raw in claude_md.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw.startswith("@"):
            ref = Path(raw[1:]).expanduser()
            if (ref if ref.is_absolute() else claude_md.parent / ref).resolve() == target:
                return True
    return False


def _setup_routing(data: Path) -> None:
    paths = g.Paths(g.ENGINE, data)
    line = _routing_line(data)
    for root in g.project_roots(paths):
        if not root.is_dir():
            print(f"  skipped (missing): {root}")
            continue
        if _is_git_repo(root):
            print(f"  skipped (inside a git repo): {root}")
            continue
        claude_md = root / "CLAUDE.md"
        if claude_md.exists():
            if _routes_to(claude_md, data):
                print(f"  ok: {claude_md} already routes here")
            else:
                print(f"  skipped (exists, no routing line): {claude_md}")
            continue
        claude_md.write_text(line + "\n", encoding="utf-8", newline="\n")
        print(f"  written: {claude_md}")


def _setup_user_level(data: Path) -> None:
    line = _routing_line(data)
    _append_if_missing(Path.home() / ".claude" / "CLAUDE.md", line)
    codex_line = USER_LEVEL_CODEX_LINE.format(agents=f"{data.as_posix()}/AGENTS.md")
    _append_if_missing(Path.home() / ".codex" / "AGENTS.md", codex_line)


def cmd_setup(args: argparse.Namespace) -> None:
    data = Path(args.data).expanduser().resolve() if args.data else g.resolve_data_dir()
    interactive = _is_interactive(args)

    if not args.no_env:
        print("Environment variables:")
        _setup_env(data, interactive, args.yes)
    if not getattr(args, "no_machine", False):
        print("Machine name:")
        _setup_machine(data, interactive)
    if not args.no_agents:
        print("Claude Code subagents:")
        _setup_agents(interactive)
    if not args.no_routing:
        print("Project root routing:")
        _setup_routing(data)
    if args.user_level:
        print("User-level routing:")
        _setup_user_level(data)


# --- machine -----------------------------------------------------------------------
# This computer's own settings for a shared data repo, e.g. on a second computer
# after cloning it: writes only the gitignored .graph/machine.json.

def cmd_machine(args: argparse.Namespace) -> None:
    data = Path(args.data).expanduser().resolve() if args.data else g.resolve_data_dir()
    if not _is_data_repo(data):
        sys.exit(f"machine: {data} is not a data repo (no vault.config.json or AGENTS.md)")
    paths = g.Paths(g.ENGINE, data)
    before = g.machine_name(paths)

    updates: dict = {}
    if args.project_root:
        roots = []
        for raw in args.project_root:
            root = Path(raw).expanduser().resolve()
            if not root.is_dir():
                print(f"  note: {root} is not a folder on this computer; it is ignored until it is")
            roots.append(str(root))
        updates["project_roots"] = roots
    if args.name is not None:
        name = g.sanitize_machine_name(args.name)
        if not name:
            sys.exit("machine: --name needs letters or digits")
        if g.scan_line(args.name):
            sys.exit("machine: that name looks like personal data; pick a neutral one "
                     "such as laptop or pc-2")
        updates["machine"] = name

    if updates:
        _write_machine_json(data, updates)
        print(f"  wrote: .graph/machine.json ({', '.join(updates)}); nothing else was changed")
        if "machine" in updates and g.sanitize_machine_name(os.environ.get("VAULT_MACHINE", "")):
            print("  note: VAULT_MACHINE is set and wins over this name")
        _rename_hint(data, before, g.machine_name(paths))

    saved = _machine_json(data)
    source = ("VAULT_MACHINE" if g.sanitize_machine_name(os.environ.get("VAULT_MACHINE", ""))
              else ".graph/machine.json" if isinstance(saved.get("machine"), str)
              and g.sanitize_machine_name(saved["machine"]) else "hostname")
    print(f"machine name: {g.machine_name(paths)} ({source})")
    print("project roots: " + ", ".join(str(r) for r in g.project_roots(paths)))


# --- doctor ------------------------------------------------------------------------

def _lint_summary(data: Path) -> tuple[bool, str]:
    """Run cmd_lint's checks without letting its sys.exit escape. Returns (ok, summary)."""
    buf = io.StringIO()
    ok = True
    try:
        with redirect_stdout(buf):
            g.cmd_lint(argparse.Namespace(), g.Paths(g.ENGINE, data))
    except SystemExit as exc:
        ok = not exc.code
    lines = [l for l in buf.getvalue().splitlines() if l.strip()]
    summary = lines[-1] if lines else "no output"
    return ok, summary


AGENT_TOOLS = (("claude", "Claude Code"), ("codex", "Codex"))


def _which(tool: str) -> str | None:
    # Wrapped so tests can control what is "installed".
    return shutil.which(tool)


_CI_PIN_RE = re.compile(r"uses:\s*[\w.\-]+/vault-engine@(\S+)")
_CI_PUSH_BRANCHES_RE = re.compile(r"^\s*push:[ \t]*\n\s*branches:\s*\[([^\]]*)\]", re.M)


def _vault_ci_checks(data: Path, channel: str, engine_ref: str | None) -> list[tuple[str, str]]:
    """Problems an older vault's CI workflow carries over: the engine pin lags
    behind a stable install, or the data repo is on a branch the workflow never
    runs on (v0.2.0 vaults were often created on `master`)."""
    text = _read_text(data / ".github" / "workflows" / "vault.yml")
    if not text:
        return []
    found: list[tuple[str, str]] = []
    pin = _CI_PIN_RE.search(text)
    if channel == "stable" and engine_ref and pin and pin.group(1) != engine_ref:
        found.append(("WARN", f"vault CI runs the engine at @{pin.group(1)}, this engine is "
                              f"{engine_ref}: run update (re-pins it and commits "
                              ".github/workflows/vault.yml), then push"))
    push = _CI_PUSH_BRANCHES_RE.search(text)
    branches = [b.strip().strip("'\"") for b in push.group(1).split(",")] if push else []
    try:
        out = subprocess.run(["git", "symbolic-ref", "-q", "--short", "HEAD"], cwd=data,
                              capture_output=True, text=True, encoding="utf-8")
        branch = out.stdout.strip() if out.returncode == 0 else ""
    except OSError:
        branch = ""
    if branch and branches and branch not in branches:
        target = "main" if "main" in branches else branches[0]
        found.append(("WARN", f"vault CI runs on pushes to {', '.join(branches)}, but the data "
                              f"repo is on {branch}, so CI never runs: git branch -m {branch} "
                              f"{target} && git push -u origin {target}"))
    return found


def _uncommitted_checks(data: Path) -> list[tuple[str, str]]:
    """Files engine commands write (g.ENGINE_FILES) that are not committed, and
    how many other changes are pending. Each engine command commits its own
    files; what is left here came from an older engine, a failed commit (no git
    identity, the guard) or a hand edit."""
    changed = g.git_status(data)
    if changed is None:
        return []  # not a git repo: the core.hooksPath check already fails
    if not changed:
        return [("OK", "data repo: nothing uncommitted")]
    has_head = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=data,
                              capture_output=True, text=True, encoding="utf-8").returncode == 0
    if not has_head:
        return [("WARN", f"data repo has no commits yet: git -C \"{data}\" add -A && "
                         f"git -C \"{data}\" commit -m \"chore: initialize vault\"")]
    owned = g.git_status(data, g.ENGINE_FILES) or []
    found: list[tuple[str, str]] = []
    if owned:
        names = " ".join(owned)
        found.append(("WARN", f"uncommitted vault-engine files in the data repo: {', '.join(owned)}: "
                              f"git -C \"{data}\" add -- {names} && git -C \"{data}\" commit "
                              f"-m \"chore: save vault-engine files\" -- {names}, then push"))
    others = len(changed) - len(owned)
    if others:
        found.append(("INFO", f"{others} other uncommitted change(s) in the data repo "
                              f"(git -C \"{data}\" status)"))
    return found


def _doctor_data_dir(data_arg: str | None,
                     rc_path: Path) -> tuple[Path | None, list[tuple[str, str]]]:
    """The data dir doctor checks and the VAULT_DATA lines to report. --data wins;
    otherwise VAULT_DATA/VAULT_HOME, then (Windows) the value setx saved for the
    user, which a terminal or agent started before setup can't see yet."""
    data = Path(data_arg).expanduser().resolve() if data_arg else None
    raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
    if raw:
        if data is not None and not _same_path(raw, str(data)):
            return data, [("WARN", f"VAULT_DATA={raw} != --data {data}")]
        return data or Path(raw).expanduser().resolve(), []
    saved = _user_env_var("VAULT_DATA") or _user_env_var("VAULT_HOME")
    if saved:
        return data or Path(saved).expanduser().resolve(), [
            ("WARN", "VAULT_DATA is set for the user but not in this process: "
                     "restart the terminal and the agent")]
    if not _is_windows() and _rc_block_present(rc_path, "VAULT_DATA"):
        msg = (f"VAULT_DATA is set in {rc_path} but not in this process: "
               "restart the terminal and the agent")
        return data, [("WARN", msg) if data else ("FAIL", msg + " (or pass --data)")]
    if data is not None:
        return data, [("WARN", "VAULT_DATA not set: run setup --data")]
    return None, [("FAIL", "VAULT_DATA (or VAULT_HOME) not set: run setup --data, "
                           "or pass --data to doctor")]


def cmd_doctor(args: argparse.Namespace) -> None:
    checks: list[tuple[str, str]] = []

    def check(status: str, msg: str) -> None:
        checks.append((status, msg))

    rc_path = _rc_file_for_shell(_shell_name(), sys.platform, Path.home())
    data, data_checks = _doctor_data_dir(getattr(args, "data", None), rc_path)

    check("OK", f"vault-engine {g.__version__}")

    update = sys.modules.get("update")
    status, ref = "unknown", None
    if update is not None:
        status, ref = update.channel(g.ENGINE)
        if status == "stable":
            check("OK", f"channel: stable ({ref})")
        elif status == "dev":
            check("INFO", f"channel: dev ({ref}): update with git pull, then migrate")
        else:
            check("INFO", "channel: unknown (not a git checkout)")
        if status == "stable":
            settings = (update.load_settings(g.Paths(g.ENGINE, data))
                        if data is not None else dict(update.DEFAULT_SETTINGS))
            if settings.get("check", True):
                latest = update.record_check(g.ENGINE)
                if latest is None:
                    check("INFO", "could not check for updates")
                else:
                    current_v = update.parse_version(g.__version__)
                    latest_v = update.parse_version(latest)
                    if latest_v and current_v and latest_v > current_v:
                        check("WARN", f"vault-engine {'.'.join(str(n) for n in latest_v)} "
                                      f"available (current {g.__version__}): run update")
                    else:
                        check("OK", "up to date")

    if sys.version_info >= (3, 10):
        check("OK", f"Python {sys.version.split()[0]}")
    else:
        check("FAIL", f"Python {sys.version.split()[0]} < 3.10")

    # Which agent CLIs can follow AGENTS.md here; Ollama only adds semantic search.
    found = {tool: _which(tool) for tool, _ in AGENT_TOOLS}
    for tool, label in AGENT_TOOLS:
        check("OK" if found[tool] else "INFO", f"{label}: {'found' if found[tool] else 'not found'}")
    if not any(found.values()):
        check("WARN", "no supported agent CLI found (claude, codex)")
    if not _which("ollama"):
        check("INFO", "ollama CLI not found (keyword-only search unless Ollama runs elsewhere)")

    raw_engine = os.environ.get("VAULT_ENGINE")
    if not raw_engine and _user_env_var("VAULT_ENGINE"):
        check("WARN", "VAULT_ENGINE is set for the user but not in this process: "
                      "restart the terminal and the agent")
    elif not raw_engine and not _is_windows() and _rc_block_present(rc_path, "VAULT_ENGINE"):
        check("WARN", f"VAULT_ENGINE is set in {rc_path} but not in this process: "
                      "restart the terminal and the agent")
    elif not raw_engine:
        check("WARN", "VAULT_ENGINE not set")
    elif not _same_path(raw_engine, str(g.ENGINE)):
        check("WARN", f"VAULT_ENGINE={raw_engine} != {g.ENGINE}")
    else:
        check("OK", "VAULT_ENGINE matches this engine")

    for data_status, msg in data_checks:
        check(data_status, msg)

    if data is not None:
        if (data / "AGENTS.md").exists():
            check("OK", f"data dir has AGENTS.md ({data})")
        else:
            check("FAIL", f"data dir has no AGENTS.md ({data})")
        if update is not None:
            agents_state, agents_msg = update.agents_md_status(g.ENGINE, data)
            if agents_state in ("current", "customized"):
                check("OK", agents_msg)
            elif agents_state == "stale":
                check("WARN", f"{agents_msg}: run update to see the diff; "
                              "update --apply-agents replaces the file")
            elif agents_state == "newer":
                check("INFO", agents_msg)

        try:
            out = subprocess.run(["git", "config", "core.hooksPath"], cwd=data,
                                  capture_output=True, text=True, encoding="utf-8").stdout.strip()
        except OSError:
            out = ""
        expected = (g.ENGINE / "tools" / "hooks").as_posix()
        if out and _same_path(out, str(g.ENGINE / "tools" / "hooks")):
            check("OK", "core.hooksPath points at this engine's hooks")
        else:
            check("FAIL", f"core.hooksPath={out or '(unset)'}, expected {expected}")

    if data is not None:
        profile_dir = data / "profile"
        unfilled = [p.name for p in sorted(profile_dir.glob("*.md"))
                    if SKELETON_MARKER in _read_text(p)] if profile_dir.is_dir() else []
        if unfilled:
            check("WARN", "profile not filled yet: run `onboard --questions`, ask the user, "
                          "then `onboard --answers <file>` (or `onboard` in a terminal)")

    src_dir = g.ENGINE / "tools" / "claude-agents"
    dest_dir = Path.home() / ".claude" / "agents"
    stale = []
    for src in sorted(src_dir.glob("*.md")) if src_dir.is_dir() else []:
        dest = dest_dir / src.name
        if not dest.exists() or dest.read_text(encoding="utf-8") != src.read_text(encoding="utf-8"):
            stale.append(src.name)
    if stale:
        check("WARN", f"claude-agents out of date: {', '.join(stale)}")
    else:
        check("OK", "claude-agents up to date")

    if data is not None:
        paths = g.Paths(g.ENGINE, data)
        for root in g.project_roots(paths):
            claude_md = root / "CLAUDE.md"
            if claude_md.exists() and _routes_to(claude_md, data):
                check("OK", f"routing present for {root}")
            else:
                check("WARN", f"routing missing for {root}")

    try:
        urllib.request.urlopen(urllib.request.Request(f"{g.OLLAMA_URL}/api/tags"), timeout=2)
        check("OK", f"Ollama reachable at {g.OLLAMA_URL}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        check("WARN", f"Ollama unreachable at {g.OLLAMA_URL} ({exc}); keyword-only fallback")

    discovery = sys.modules.get("discovery")
    if data is not None and discovery is not None:
        # Cache only: doctor should stay fast, `projects --refresh` rescans.
        try:
            missing = discovery.missing_projects(g.Paths(g.ENGINE, data))
        except Exception as exc:  # discovery problems must not hide the other checks
            check("WARN", f"project discovery failed ({exc})")
        else:
            if missing:
                check("WARN", f"projects without notes: {', '.join(missing)} (see `projects --missing`)")
            else:
                check("OK", "every discovered project has notes")

    if data is not None:
        ok, summary = _lint_summary(data)
        check("OK" if ok else "FAIL", f"note lint: {summary}")

    schema = sys.modules.get("schema")
    if data is not None and schema is not None:
        schema_paths = g.Paths(g.ENGINE, data)
        data_schema = schema.read_schema(schema_paths)
        if data_schema == schema.SCHEMA_VERSION and not schema.has_schema_field(schema_paths):
            check("WARN", f"vault schema {data_schema} (implicit, not recorded in "
                          "vault.config.json): run migrate")
        elif data_schema == schema.SCHEMA_VERSION:
            check("OK", f"vault schema {data_schema}")
        elif data_schema > schema.SCHEMA_VERSION:
            check("FAIL", f"vault schema {data_schema} is newer than this engine "
                          f"({schema.SCHEMA_VERSION}): run update on this computer")
        else:
            check("WARN", f"vault schema {data_schema} is older than this engine "
                          f"({schema.SCHEMA_VERSION}): run migrate")
        shared_roots = schema.shared_project_roots(schema_paths)
        if shared_roots and all(Path(r).expanduser().is_dir() for r in shared_roots):
            check("WARN", "vault.config.json sets project_roots, a per-computer path, in the "
                          "shared config: run migrate (moves it to .graph/machine.json)")
        elif shared_roots:
            check("WARN", "vault.config.json sets project_roots that do not exist on this "
                          "computer (ignored here): run migrate on the computer they belong to")

    if data is not None:
        for ci_status, msg in _vault_ci_checks(data, status, ref):
            check(ci_status, msg)
        for git_status, msg in _uncommitted_checks(data):
            check(git_status, msg)

    for status, msg in checks:
        print(f"{status:<4} {msg}")
    sys.exit(1 if any(status == "FAIL" for status, _ in checks) else 0)


# --- onboard -----------------------------------------------------------------------
# Fills profile/language.md and profile/working-style.md from a few questions, so
# people who never write YAML/Markdown still get a filled profile. Works both from
# a terminal (interactive prompts) and through an agent (--questions / --answers),
# since most users will go through the latter, conversationally.

ASK_BEFORE_OPTIONS = ["architecture", "new-dependencies", "deleting",
                      "paid-or-external-services", "pushing-or-publishing"]

# Module level so docs and tests can rely on the exact question set.
QUESTIONS: list[dict] = [
    {"id": "chat_language", "kind": "text", "default": "English",
     "question": "What language should the agent talk to you in?"},
    {"id": "notes_language", "kind": "text", "default": None, "default_note": "same as chat_language",
     "question": "What language should notes and docs be written in?"},
    {"id": "code_language", "kind": "text", "default": "English",
     "question": "What language should code, identifiers and commit messages use?"},
    {"id": "detail", "kind": "choice", "options": ["short", "balanced", "detailed"], "default": "balanced",
     "question": "How much detail do you want in answers?"},
    {"id": "explain_level", "kind": "choice",
     "options": ["beginner", "intermediate", "expert"], "default": "intermediate",
     "question": "How much should the agent explain technical terms?"},
    {"id": "ask_before", "kind": "multi", "options": list(ASK_BEFORE_OPTIONS), "default": list(ASK_BEFORE_OPTIONS),
     "question": "Before which kinds of actions should the agent ask first?"},
    {"id": "extra", "kind": "text", "default": "",
     "question": "Anything else the agent should know? (optional, max 5 lines)"},
]

_QUESTIONS_BY_ID = {q["id"]: q for q in QUESTIONS}


class AnswersError(Exception):
    """An answers JSON file failed validation."""


def _trim_single_line(value: str, max_len: int = 200) -> str:
    text = str(value).replace("\r\n", "\n")
    line = text.splitlines()[0] if text.splitlines() else ""
    return line.strip()[:max_len]


def _trim_multiline(value: str, max_lines: int = 5, max_len: int = 200) -> list[str]:
    text = str(value).replace("\r\n", "\n")
    lines = [l.strip()[:max_len] for l in text.splitlines() if l.strip()]
    return lines[:max_lines]


def _validate_choice(qid: str, value, options: list[str], default: str) -> str:
    if value is None or value == "":
        return default
    if not isinstance(value, str) or value not in options:
        raise AnswersError(f"invalid choice for {qid}: {value!r} (expected one of {', '.join(options)})")
    return value


def _validate_multi(qid: str, value, options: list[str], default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    if not isinstance(value, list):
        raise AnswersError(f"invalid value for {qid}: expected a list")
    bad = [v for v in value if v not in options]
    if bad:
        raise AnswersError(f"invalid choice(s) for {qid}: {', '.join(bad)} (expected one of {', '.join(options)})")
    return list(value)


def resolve_answers(raw: dict) -> dict:
    """Validate a raw answers dict (from JSON or interactive prompts), fill in
    defaults for missing keys, and trim text. Raises AnswersError on bad choices."""
    raw = raw or {}
    out: dict = {}
    out["chat_language"] = _trim_single_line(raw.get("chat_language") or "English") or "English"
    out["notes_language"] = _trim_single_line(raw.get("notes_language") or out["chat_language"]) \
        or out["chat_language"]
    out["code_language"] = _trim_single_line(raw.get("code_language") or "English") or "English"
    out["detail"] = _validate_choice("detail", raw.get("detail"), _QUESTIONS_BY_ID["detail"]["options"],
                                      _QUESTIONS_BY_ID["detail"]["default"])
    out["explain_level"] = _validate_choice(
        "explain_level", raw.get("explain_level"), _QUESTIONS_BY_ID["explain_level"]["options"],
        _QUESTIONS_BY_ID["explain_level"]["default"])
    out["ask_before"] = _validate_multi("ask_before", raw.get("ask_before"), ASK_BEFORE_OPTIONS,
                                         ASK_BEFORE_OPTIONS)
    out["extra"] = _trim_multiline(raw.get("extra") or "")
    return out


def _scan_answers(answers: dict) -> list[str]:
    """Runs every answer text through the personal-data/secret guard. Callers must
    report only the returned kinds, never the offending value."""
    texts = [answers["chat_language"], answers["notes_language"], answers["code_language"]]
    texts += answers["extra"]
    findings: list[str] = []
    for text in texts:
        findings += g.scan_line(text)
    return findings


_DETAIL_TEXT = {
    "short": "Keep answers short.",
    "balanced": "Keep answers balanced: not too short, not too long.",
    "detailed": "Give detailed, thorough answers.",
}
_EXPLAIN_TEXT = {
    "beginner": "Explain technical terms simply; the user is a beginner.",
    "intermediate": "Explain unusual technical terms briefly; the user has intermediate experience.",
    "expert": "Skip basic explanations; the user is an expert.",
}


def _build_rules(answers: dict) -> tuple[list[str], list[str]]:
    """Turns validated answers into short bullet rules for each profile note.
    Returns (language_note_rules, working_style_rules)."""
    lang_rules = [
        f"Talk in {answers['chat_language']}.",
        f"Write notes and docs in {answers['notes_language']}.",
        f"Write code, identifiers and commit messages in {answers['code_language']}.",
    ]

    style_rules = [_DETAIL_TEXT[answers["detail"]], _EXPLAIN_TEXT[answers["explain_level"]]]
    if answers["ask_before"]:
        readable = ", ".join(a.replace("-", " ") for a in answers["ask_before"])
        style_rules.append(f"Ask before: {readable}.")
    style_rules += answers["extra"]
    return lang_rules, style_rules


def _note_text(title: str, keywords: list[str], rules: list[str]) -> str:
    kw = ", ".join(keywords)
    lines = ["---", "core: true", f"keywords: [{kw}]", "links: []", "weights: {}", "---", "", f"# {title}", ""]
    lines += [f"- {rule}" for rule in rules]
    return "\n".join(lines) + "\n"


def _write_profile_note(data: Path, rel: str, title: str, keywords: list[str], rules: list[str],
                         force: bool) -> str:
    path = data / "profile" / rel
    if path.exists() and SKELETON_MARKER not in _read_text(path) and not force:
        return "already filled, skipped"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_note_text(title, keywords, rules), encoding="utf-8", newline="\n")
    return "written"


def _run_onboard(data: Path, raw: dict, force: bool) -> None:
    try:
        answers = resolve_answers(raw)
    except AnswersError as exc:
        sys.exit(f"invalid answers: {exc}")

    findings = _scan_answers(answers)
    if findings:
        kinds = ", ".join(sorted(set(findings)))
        sys.exit(f"refused: an answer looks like personal data / a secret ({kinds}). "
                 f"Rephrase it (see {g.data_policy_hint()}) and try again.")

    lang_rules, style_rules = _build_rules(answers)
    status_lang = _write_profile_note(
        data, "language.md", "Language Preference",
        ["dil", "language", "türkçe", "ingilizce", "english"], lang_rules, force)
    status_style = _write_profile_note(
        data, "working-style.md", "Working Style",
        ["çalışma tarzı", "working style", "tercih", "preference", "iletişim", "communication"],
        style_rules, force)
    print(f"  profile/language.md: {status_lang}")
    print(f"  profile/working-style.md: {status_style}")
    if any(status != "written" for status in (status_lang, status_style)):
        print("  Filled notes were kept; rerun with --force to replace them with these answers.")
    written = [f"profile/{name}" for name, status in (("language.md", status_lang),
                                                      ("working-style.md", status_style))
               if status == "written"]
    if written:
        # Committed at once, so the next task's `git pull --rebase` has a clean tree.
        g.report_commit(data, written, g.commit_own_files(data, written, "chore: fill in profile"),
                        indent="  ")


def _default_for(qid: str, raw: dict) -> object:
    if qid == "notes_language":
        return raw.get("chat_language") or _QUESTIONS_BY_ID["chat_language"]["default"]
    return _QUESTIONS_BY_ID[qid]["default"]


def _ask_question(q: dict, default) -> object:
    if q["kind"] == "choice":
        prompt = f"{q['question']} ({'/'.join(q['options'])})"
        while True:
            raw = _ask(prompt, str(default), strict=True)
            if raw in q["options"]:
                return raw
            print(f"  invalid choice: {raw!r}; options: {', '.join(q['options'])}")
    if q["kind"] == "multi":
        prompt = f"{q['question']} ({', '.join(q['options'])}; comma-separated)"
        default_str = ",".join(default)
        while True:
            raw = _ask(prompt, default_str, strict=True)
            items = [x.strip() for x in raw.split(",") if x.strip()]
            bad = [i for i in items if i not in q["options"]]
            if not bad:
                return items
            print(f"  invalid option(s): {', '.join(bad)}; options: {', '.join(q['options'])}")
    return _ask(q["question"], str(default), strict=True)


ONBOARD_AGENT_HINT = ("Get the questions with `onboard --questions`, ask the user, then run "
                      "`onboard --answers <file>` with the answers as JSON.")


def _load_answers(path: Path) -> dict:
    """The answers JSON object. utf-8-sig: PowerShell 5.1's
    `Set-Content -Encoding UTF8` starts the file with a BOM."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        sys.exit(f"onboard: cannot read the answers file {path}: {exc.strerror or exc}")
    except UnicodeDecodeError:
        sys.exit(f"onboard: the answers file {path} is not UTF-8 text; save it as UTF-8")
    try:
        raw = json.loads(text)
    except ValueError as exc:
        sys.exit(f"onboard: the answers file {path} is not valid JSON ({exc}); "
                 "see `onboard --questions` for the expected keys")
    if not isinstance(raw, dict):
        sys.exit(f"onboard: the answers file {path} must hold one JSON object "
                 '({"chat_language": "...", ...})')
    return raw


def cmd_onboard(args: argparse.Namespace) -> None:
    if args.questions:
        print(json.dumps(QUESTIONS, indent=2, ensure_ascii=False))
        return

    if not args.answers and not g.stdin_is_interactive():
        sys.exit("onboard: not an interactive terminal, nothing written. " + ONBOARD_AGENT_HINT)

    data = Path(args.data).expanduser().resolve() if args.data else g.resolve_data_dir()
    g._require_writable(g.Paths(g.ENGINE, data))

    if args.answers:
        _run_onboard(data, _load_answers(Path(args.answers)), args.force)
        return

    raw: dict = {}
    try:
        for q in QUESTIONS:
            raw[q["id"]] = _ask_question(q, _default_for(q["id"], raw))
    except EOFError:
        # Input ended (e.g. stdin is NUL): stop before writing anything, so the
        # skeleton stays and a later `--answers` run still fills the profile.
        print()
        sys.exit("onboard: input ended before every question was answered; nothing written. "
                 + ONBOARD_AGENT_HINT)
    _run_onboard(data, raw, args.force)


# --- registration ------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("init", help="create a new data repo from templates")
    p.add_argument("dir")
    p.add_argument("--feedback", choices=FEEDBACK_LEVELS)
    p.add_argument("--feedback-mode", choices=FEEDBACK_MODES)
    p.add_argument("--project-root", action="append", help="repeatable")
    p.add_argument("--yes", action="store_true", help="never prompt, use defaults")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("setup", help="wire env vars, agents and routing to this engine")
    p.add_argument("--data", help="data dir (default: resolve_data_dir())")
    p.add_argument("--user-level", action="store_true", help="also wire ~/.claude and ~/.codex")
    p.add_argument("--no-env", action="store_true")
    p.add_argument("--no-agents", action="store_true")
    p.add_argument("--no-routing", action="store_true")
    p.add_argument("--no-machine", action="store_true", help="don't name this computer")
    p.add_argument("--yes", action="store_true", help="never prompt, use defaults")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("machine", help="show or set this computer's own settings "
                                       "(.graph/machine.json only)")
    p.add_argument("--data", help="data dir (default: resolve_data_dir())")
    p.add_argument("--project-root", action="append",
                   help="this computer's project folder (repeatable, replaces the saved list)")
    p.add_argument("--name", help="this computer's name for .graph/learned/<name>.json")
    p.set_defaults(func=cmd_machine)

    p = sub.add_parser("doctor", help="check the onboarding setup, one line per check")
    p.add_argument("--data", help="data dir (default: resolve_data_dir())")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("onboard", help="fill in profile/ from a few questions")
    p.add_argument("--questions", action="store_true", help="print the questions as JSON")
    p.add_argument("--answers", help="JSON file with answers")
    p.add_argument("--data", help="data dir (default: resolve_data_dir())")
    p.add_argument("--force", action="store_true", help="overwrite even if already filled")
    p.set_defaults(func=cmd_onboard)
