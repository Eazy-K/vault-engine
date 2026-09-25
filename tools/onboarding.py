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
    "off": "off: hiçbir şey gönderilmez",
    "metrics": "metrics: sadece anonim sayaçlar gönderilir",
    "reports": "reports: sayaçlar + filtrelenmiş şablon sürtünme raporları gönderilir",
}

USER_LEVEL_CODEX_LINE = "Bağlam ve kurallar {agents} dosyasında, önce onu oku ve izle."


# --- shared helpers ------------------------------------------------------------

def _ask(prompt: str, default: str) -> str:
    """Prompt with a shown default; EOF (non-interactive stdin) falls back to it."""
    try:
        raw = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        return default
    return raw or default


def _ask_yn(prompt: str, default: bool) -> bool:
    shown = "E/h" if default else "e/H"
    try:
        raw = input(f"{prompt} [{shown}]: ").strip().lower()
    except EOFError:
        return default
    if not raw:
        return default
    return raw.startswith("e")


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
    with path.open("a", encoding="utf-8") as f:
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
        copied.append(str(rel))
    return copied, skipped


def cmd_init(args: argparse.Namespace) -> None:
    target = Path(args.dir).expanduser().resolve()
    if target == g.ENGINE.resolve() or g.ENGINE.resolve() in target.parents:
        sys.exit(f"refusing to create a data repo inside the engine ({g.ENGINE})")

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

    project_roots = args.project_root
    if not project_roots:
        default = str(g.ENGINE.parent)
        if interactive:
            raw = _ask("Proje kökü klasörü (virgülle ayrılmış birden fazla yol olabilir)", default)
            project_roots = [p.strip() for p in raw.split(",") if p.strip()]
        else:
            project_roots = [default]

    feedback_level = args.feedback
    if not feedback_level:
        if interactive:
            for line in FEEDBACK_EXPLAIN.values():
                print(f"  {line}")
            feedback_level = _ask("Feedback seviyesi (off/metrics/reports)", "off")
        else:
            feedback_level = "off"  # opt-in only
    if feedback_level not in FEEDBACK_LEVELS:
        sys.exit(f"invalid feedback level: {feedback_level}")

    feedback_mode = args.feedback_mode
    if not feedback_mode:
        if interactive:
            feedback_mode = _ask("Feedback modu (auto/ask)", "ask")
        else:
            feedback_mode = "ask"
    if feedback_mode not in FEEDBACK_MODES:
        sys.exit(f"invalid feedback mode: {feedback_mode}")

    config = {
        "project_roots": project_roots,
        "feedback": {"level": feedback_level, "mode": feedback_mode},
    }
    config_file = target / "vault.config.json"
    config_file.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8", newline="\n")
    print(f"  wrote: {config_file.relative_to(target).as_posix()}")

    print("\nSonraki adımlar:")
    print(f"  1. python \"{g.ENGINE / 'tools' / 'graph.py'}\" setup --data \"{target}\"")
    print(f"  2. profile/ altındaki notları doldur ({target / 'profile'})")


# --- setup -----------------------------------------------------------------------

def _set_user_env_var(name: str, value: str) -> None:
    """Persist a user-level env var on Windows. Isolated so tests can mock it
    without ever touching the real environment."""
    subprocess.run(["setx", name, value], check=True, capture_output=True)


def _same_path(a: str, b: str) -> bool:
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except OSError:
        return False


def _setup_env(data: Path, interactive: bool) -> None:
    pairs = [("VAULT_ENGINE", str(g.ENGINE)), ("VAULT_DATA", str(data))]
    pending = [(n, v) for n, v in pairs if not (os.environ.get(n) and _same_path(os.environ[n], v))]
    for name, value in pairs:
        if (name, value) not in pending:
            print(f"  ok: {name} already set")
    if not pending:
        return
    if os.name == "nt":
        do_it = True
        if interactive:
            do_it = _ask_yn("Ortam değişkenleri (setx ile kalıcı) ayarlansın mı?", True)
        if do_it:
            for name, value in pending:
                _set_user_env_var(name, value)
                print(f"  set (setx): {name}={value}  (yeni terminalde etkili olur)")
        else:
            for name, value in pending:
                print(f"  export {name}={value}")
    else:
        print("  Aşağıdaki satırları shell rc dosyana ekle:")
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
        dest.write_text(content, encoding="utf-8")
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
        claude_md.write_text(line + "\n", encoding="utf-8")
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
        print("Ortam değişkenleri:")
        _setup_env(data, interactive)
    if not args.no_agents:
        print("Claude Code alt ajanları:")
        _setup_agents()
    if not args.no_routing:
        print("Proje kökü yönlendirmesi:")
        _setup_routing(data)
    if args.user_level:
        print("Kullanıcı seviyesi yönlendirme:")
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


def cmd_doctor(_args: argparse.Namespace) -> None:
    checks: list[tuple[str, str]] = []

    def check(status: str, msg: str) -> None:
        checks.append((status, msg))

    if sys.version_info >= (3, 10):
        check("OK", f"Python {sys.version.split()[0]}")
    else:
        check("FAIL", f"Python {sys.version.split()[0]} < 3.10")

    raw_engine = os.environ.get("VAULT_ENGINE")
    if not raw_engine:
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

    for status, msg in checks:
        print(f"{status:<4} {msg}")
    sys.exit(1 if any(status == "FAIL" for status, _ in checks) else 0)


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
