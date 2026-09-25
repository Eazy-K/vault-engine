"""Onboarding: scaffold a new data repo, wire it to this engine, and check the
setup's health.

Optional extension module: graph.py imports this if present (see EXTENSIONS
in graph.py) and calls register(sub) with its argparse subparsers object.
Stdlib only, like graph.py itself.
"""
from __future__ import annotations

import argparse
import io
import json
import os
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

def _ask(prompt: str, default: str) -> str:
    """Prompt with a shown default; EOF (non-interactive stdin) falls back to it."""
    try:
        raw = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
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
    return sys.stdin.isatty() and not args.yes


def _is_git_repo(path: Path) -> bool:
    try:
        out = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=path,
                              capture_output=True, text=True, encoding="utf-8")
    except OSError:
        return False
    return out.returncode == 0


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
        if ENGINE_REPO_PLACEHOLDER in _read_text(dest):
            _fill_engine_repo(dest)
        copied.append(str(rel))
    return copied, skipped


ENGINE_REPO_PLACEHOLDER = "{{ENGINE_REPO}}"


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


def _load_existing_config(path: Path) -> dict:
    """The current vault.config.json, or {} if missing/unreadable -- never
    raises, so init can always fall back to writing a fresh one."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cmd_init(args: argparse.Namespace) -> None:
    target = Path(args.dir).expanduser().resolve()
    if target == g.ENGINE.resolve() or g.ENGINE.resolve() in target.parents:
        sys.exit(f"refusing to create a data repo inside the engine ({g.ENGINE})")

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

    if not _is_git_repo(target):
        subprocess.run(["git", "init"], cwd=target, check=True, capture_output=True)
        print("  git repo initialised")
    hooks_path = (g.ENGINE / "tools" / "hooks").as_posix()
    subprocess.run(["git", "config", "core.hooksPath", hooks_path], cwd=target,
                    check=True, capture_output=True)
    print(f"  core.hooksPath = {hooks_path}")

    interactive = _is_interactive(args)
    existing_feedback = existing.get("feedback") if isinstance(existing.get("feedback"), dict) else {}

    # An explicit flag always wins; otherwise an already-configured vault keeps
    # what it has, and only a brand-new value is asked for or defaulted.
    # An existing config is shared by every computer using this vault: without
    # a flag, init never adds this computer's paths to it (each computer then
    # falls back to its own engine's parent folder).
    project_roots = args.project_root
    if not project_roots:
        if existing:
            project_roots = existing.get("project_roots")
        else:
            default = str(g.ENGINE.parent)
            if interactive:
                raw = _ask("Project root folder (comma-separated for multiple paths)", default)
                project_roots = [p.strip() for p in raw.split(",") if p.strip()]
            else:
                project_roots = [default]

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

    config = dict(existing)
    if project_roots:
        config["project_roots"] = project_roots
    config["feedback"] = {"level": feedback_level, "mode": feedback_mode}
    if "schema" not in config:
        schema = sys.modules.get("schema")
        config["schema"] = schema.SCHEMA_VERSION if schema is not None else 1
    config_file = target / "vault.config.json"
    config_file.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8", newline="\n")
    print(f"  wrote: {config_file.relative_to(target).as_posix()}")

    print("\nNext steps:")
    print(f"  1. python \"{g.ENGINE / 'tools' / 'graph.py'}\" setup --data \"{target}\"")
    print(f"  2. python \"{g.ENGINE / 'tools' / 'graph.py'}\" onboard --data \"{target}\""
          "   (a few questions that fill in your profile)")


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
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except (ImportError, OSError):
        return None


def _same_path(a: str, b: str) -> bool:
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except OSError:
        return False


def _is_windows() -> bool:
    # A function, not a bare os.name check, so tests can pretend to be Windows
    # without patching os.name (which breaks pathlib on other systems).
    return os.name == "nt"


def _setup_env(data: Path, interactive: bool) -> None:
    pairs = [("VAULT_ENGINE", str(g.ENGINE)), ("VAULT_DATA", str(data))]
    pending = [(n, v) for n, v in pairs if not (os.environ.get(n) and _same_path(os.environ[n], v))]
    for name, value in pairs:
        if (name, value) not in pending:
            print(f"  ok: {name} already set")
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
        print("  Add the following lines to your shell rc file:")
        for name, value in pending:
            print(f"    export {name}={value}")


def _setup_agents() -> None:
    src_dir = g.ENGINE / "tools" / "claude-agents"
    dest_dir = Path.home() / ".claude" / "agents"
    if not src_dir.is_dir():
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(src_dir.glob("*.md")):
        dest = dest_dir / src.name
        content = src.read_text(encoding="utf-8")
        if dest.exists() and dest.read_text(encoding="utf-8") == content:
            print(f"  unchanged: {dest}")
            continue
        dest.write_text(content, encoding="utf-8", newline="\n")
        print(f"  copied: {dest}")


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
        _setup_env(data, interactive)
    if not args.no_agents:
        print("Claude Code subagents:")
        _setup_agents()
    if not args.no_routing:
        print("Project root routing:")
        _setup_routing(data)
    if args.user_level:
        print("User-level routing:")
        _setup_user_level(data)


# --- doctor ------------------------------------------------------------------------

def _lint_summary(data: Path) -> tuple[bool, str]:
    """Run cmd_lint's checks without letting its sys.exit escape. Returns (ok, summary)."""
    buf = io.StringIO()
    ok = True
    try:
        with redirect_stdout(buf):
            g.cmd_lint(argparse.Namespace())
    except SystemExit as exc:
        ok = not exc.code
    lines = [l for l in buf.getvalue().splitlines() if l.strip()]
    summary = lines[-1] if lines else "no output"
    return ok, summary


AGENT_TOOLS = (("claude", "Claude Code"), ("codex", "Codex"))


def _which(tool: str) -> str | None:
    # Wrapped so tests can control what is "installed".
    return shutil.which(tool)


def cmd_doctor(_args: argparse.Namespace) -> None:
    checks: list[tuple[str, str]] = []

    def check(status: str, msg: str) -> None:
        checks.append((status, msg))

    check("OK", f"vault-engine {g.__version__}")

    update = sys.modules.get("update")
    if update is not None:
        status, ref = update.channel(g.ENGINE)
        if status == "stable":
            check("OK", f"channel: stable ({ref})")
        elif status == "dev":
            check("INFO", f"channel: dev ({ref}): update with git pull, then migrate")
        else:
            check("INFO", "channel: unknown (not a git checkout)")
        if status == "stable":
            try:
                update_data = g.resolve_data_dir()
            except SystemExit:
                update_data = None
            settings = (update.load_settings(g.Paths(g.ENGINE, update_data))
                       if update_data is not None else dict(update.DEFAULT_SETTINGS))
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
    elif not raw_engine:
        check("WARN", "VAULT_ENGINE not set")
    elif not _same_path(raw_engine, str(g.ENGINE)):
        check("WARN", f"VAULT_ENGINE={raw_engine} != {g.ENGINE}")
    else:
        check("OK", "VAULT_ENGINE matches this engine")

    data: Path | None
    try:
        data = g.resolve_data_dir()
    except SystemExit:
        check("FAIL", "VAULT_DATA (or VAULT_HOME) not set")
        data = None

    if data is not None:
        if (data / "AGENTS.md").exists():
            check("OK", f"data dir has AGENTS.md ({data})")
        else:
            check("FAIL", f"data dir has no AGENTS.md ({data})")

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
            check("WARN", "profile not filled yet (run `onboard`)")

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
        data_schema = schema.read_schema(g.Paths(g.ENGINE, data))
        if data_schema == schema.SCHEMA_VERSION:
            check("OK", f"vault schema {data_schema}")
        elif data_schema > schema.SCHEMA_VERSION:
            check("FAIL", f"vault schema {data_schema} is newer than this engine "
                          f"({schema.SCHEMA_VERSION}): run update on this computer")
        else:
            check("WARN", f"vault schema {data_schema} is older than this engine "
                          f"({schema.SCHEMA_VERSION}): run migrate")

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
                 "Rephrase it (see standards/data-policy.md) and try again.")

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


def _default_for(qid: str, raw: dict) -> object:
    if qid == "notes_language":
        return raw.get("chat_language") or _QUESTIONS_BY_ID["chat_language"]["default"]
    return _QUESTIONS_BY_ID[qid]["default"]


def _ask_question(q: dict, default) -> object:
    if q["kind"] == "choice":
        prompt = f"{q['question']} ({'/'.join(q['options'])})"
        while True:
            raw = _ask(prompt, str(default))
            if raw in q["options"]:
                return raw
            print(f"  invalid choice: {raw!r}; options: {', '.join(q['options'])}")
    if q["kind"] == "multi":
        prompt = f"{q['question']} ({', '.join(q['options'])}; comma-separated)"
        default_str = ",".join(default)
        while True:
            raw = _ask(prompt, default_str)
            items = [x.strip() for x in raw.split(",") if x.strip()]
            bad = [i for i in items if i not in q["options"]]
            if not bad:
                return items
            print(f"  invalid option(s): {', '.join(bad)}; options: {', '.join(q['options'])}")
    return _ask(q["question"], str(default))


def cmd_onboard(args: argparse.Namespace) -> None:
    if args.questions:
        print(json.dumps(QUESTIONS, indent=2, ensure_ascii=False))
        return

    if not args.answers and not sys.stdin.isatty():
        print("Not an interactive terminal. Get the questions with "
              "`onboard --questions`, ask the user, then run "
              "`onboard --answers <file>` with the answers as JSON.")
        return

    data = Path(args.data).expanduser().resolve() if args.data else g.resolve_data_dir()
    g._require_writable(g.Paths(g.ENGINE, data))

    if args.answers:
        raw = json.loads(Path(args.answers).read_text(encoding="utf-8"))
        _run_onboard(data, raw, args.force)
        return

    raw: dict = {}
    for q in QUESTIONS:
        raw[q["id"]] = _ask_question(q, _default_for(q["id"], raw))
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
    p.add_argument("--yes", action="store_true", help="never prompt, use defaults")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("doctor", help="check the onboarding setup, one line per check")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("onboard", help="fill in profile/ from a few questions")
    p.add_argument("--questions", action="store_true", help="print the questions as JSON")
    p.add_argument("--answers", help="JSON file with answers")
    p.add_argument("--data", help="data dir (default: resolve_data_dir())")
    p.add_argument("--force", action="store_true", help="overwrite even if already filled")
    p.set_defaults(func=cmd_onboard)
