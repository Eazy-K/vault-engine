#!/usr/bin/env python3
"""Opt-in feedback from a vault installation to the engine's maintainers.

Two things can be sent, both only with explicit opt-in (see `feedback set`):
  - metrics: numbers and fixed enums only (engine version, OS, note-count
    bucket, usage counters). No note ids, queries, paths or machine names.
  - reports: short free-text notes the user queues with `feedback add`, sent
    together with the metrics header.

Everything free-text is filtered twice (at `add` time and again right before
`send`) against the same personal-data guard the vault uses for notes, plus
identity terms (home path, git identity, denylist) and locally known project
names, so a leak is rejected rather than sent. Rejected text is never echoed
back -- only the kind of match is reported.

This module is loaded by tools/graph.py (see EXTENSIONS) if present; it adds
its own `feedback` subcommand via register(sub) and never edits graph.py.
Standard library only, matching the engine's own constraint.
"""
from __future__ import annotations

import json
import os
import platform
import re
import secrets
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import graph as g

# --- settings ------------------------------------------------------------

DEFAULT_SETTINGS = {"level": "off", "mode": "ask", "repo": None}
LEVELS = ("off", "metrics", "reports")
MODES = ("auto", "ask")


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}


def _machine_file(paths: "g.Paths") -> Path:
    # Gitignored per-machine override, e.g. for a sensitive machine that must
    # never send even when the shared data-repo config says otherwise.
    return paths.data / ".graph" / "machine.json"


def load_settings(paths: "g.Paths") -> dict:
    """Effective feedback settings: vault.config.json, then the per-machine
    override on top (machine values win, key by key)."""
    merged = dict(DEFAULT_SETTINGS)
    config_fb = _load_json(paths.config_file).get("feedback")
    if isinstance(config_fb, dict):
        merged.update({k: v for k, v in config_fb.items() if v is not None})
    machine_fb = _load_json(_machine_file(paths)).get("feedback")
    if isinstance(machine_fb, dict):
        merged.update({k: v for k, v in machine_fb.items() if v is not None})
    return merged


_REPO_RE = re.compile(r"github\.com[:/]+(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$", re.I)


def _derive_repo() -> str | None:
    """owner/name parsed from the engine's own `origin` remote (https or ssh)."""
    try:
        out = subprocess.run(["git", "remote", "get-url", "origin"], cwd=g.ENGINE,
                              capture_output=True, text=True, encoding="utf-8", timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    m = _REPO_RE.search(out.stdout.strip())
    return f"{m.group('owner')}/{m.group('name')}" if m else None


def target_repo(settings: dict) -> str | None:
    """The configured repo wins; otherwise derive from the engine's remote.
    None means sending is unavailable (callers must report, not crash)."""
    return settings.get("repo") or _derive_repo()


# --- paths -----------------------------------------------------------------

def _fb_dir(paths: "g.Paths") -> Path:
    return paths.data / ".graph" / "feedback"


def _queue_dir(paths: "g.Paths") -> Path:
    return _fb_dir(paths) / "queue"


def _quarantine_dir(paths: "g.Paths") -> Path:
    return _fb_dir(paths) / "quarantine"


def _sent_dir(paths: "g.Paths") -> Path:
    return _fb_dir(paths) / "sent"


def _state_file(paths: "g.Paths") -> Path:
    return _fb_dir(paths) / "state.json"


def _load_state(paths: "g.Paths") -> dict:
    return _load_json(_state_file(paths))


def _save_state(paths: "g.Paths", state: dict) -> None:
    try:
        _fb_dir(paths).mkdir(parents=True, exist_ok=True)
        _state_file(paths).write_text(json.dumps(state, indent=2, ensure_ascii=False),
                                       encoding="utf-8", newline="\n")
    except OSError:
        pass


def _due(last: str | None, now: datetime, days: float = 0, hours: float = 0) -> bool:
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (now - last_dt).total_seconds() >= days * 86400 + hours * 3600


# --- metrics -----------------------------------------------------------------

AGENTS = ("claude-code", "codex", "unknown")  # the fixed set g.detect_agent returns


def _note_bucket(n: int) -> str:
    if n < 25:
        return "<25"
    if n < 100:
        return "25-100"
    if n < 500:
        return "100-500"
    return "500+"


def _engine_version() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=g.ENGINE,
                              capture_output=True, text=True, encoding="utf-8", timeout=5)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    value = out.stdout.strip()
    return value if out.returncode == 0 and value else "unknown"


def build_metrics(paths: "g.Paths") -> dict:
    """Numbers and fixed enums only -- no note ids, queries, paths or machine
    names, so this is safe to send regardless of what filtering catches."""
    notes = g.load_notes(paths.data, paths.defaults_dir)
    events = g.read_usage(paths)
    contexts = [e for e in events if e.get("event") == "context"]
    reinforces = [e for e in events if e.get("event") == "reinforce"]
    closed = {e["task"] for e in reinforces if e.get("task")}
    done = sum(1 for c in contexts if c.get("task") in closed)
    rate = round(done / len(contexts), 3) if contexts else None
    agent_counts = Counter(c.get("agent", "unknown") for c in contexts)
    return {
        "engine_version": _engine_version(),
        "os": platform.system(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "note_count_bucket": _note_bucket(len(notes)),
        "context_calls": len(contexts),
        "reinforce_closed_rate": rate,
        "agents": {a: agent_counts.get(a, 0) for a in AGENTS},
        "reinforce_without_task": sum(1 for e in reinforces if not e.get("task")),
        "no_useful_notes": sum(1 for e in reinforces if not e.get("notes")),
    }


# --- leak filtering ------------------------------------------------------
# Anything free-text (report summary/details/command) is checked against the
# same guard patterns notes are, plus identity terms and locally known
# project names. Only the *kind* of match is ever reported back.

def _leak_terms_for(paths: "g.Paths") -> list[tuple[str, str]]:
    try:
        return g._leak_terms(paths.data)
    except (OSError, subprocess.SubprocessError):
        return []  # data dir not a git repo (or git unavailable) -- nothing to add


def _project_names(paths: "g.Paths") -> set[str]:
    names: set[str] = set()
    proj_dir = paths.data / "projects"
    if proj_dir.is_dir():
        names.update(p.name for p in proj_dir.iterdir() if p.is_dir())
    try:
        roots = g.project_roots(paths)
    except Exception:
        roots = []
    for root in roots:
        if root.is_dir():
            names.update(p.name for p in root.iterdir() if p.is_dir())
    return {n for n in names if len(n) >= 4}


def _filter_text(text: str, paths: "g.Paths") -> str | None:
    """The kind of the first match found, or None if the text looks clean."""
    if not text:
        return None
    hits = g.scan_line(text)
    if hits:
        return hits[0]
    low = text.lower()
    for term, kind in _leak_terms_for(paths):
        if len(term) >= 4 and term.lower() in low:
            return kind
    for var in ("USERNAME", "USER"):
        val = os.environ.get(var, "")
        if len(val) >= 4 and val.lower() in low:
            return "machine username"
    for name in _project_names(paths):
        if re.search(rf"\b{re.escape(name)}\b", text, re.I):
            return "project name"
    return None


def _filter_report(report: dict, paths: "g.Paths") -> str | None:
    for field in ("summary", "details", "command"):
        hit = _filter_text(str(report.get(field, "") or ""), paths)
        if hit:
            return hit
    return None


def _move_to_quarantine(path: Path, paths: "g.Paths") -> None:
    qdir = _quarantine_dir(paths)
    qdir.mkdir(parents=True, exist_ok=True)
    path.replace(qdir / path.name)


def _move_to_sent(path: Path, paths: "g.Paths") -> None:
    sdir = _sent_dir(paths)
    sdir.mkdir(parents=True, exist_ok=True)
    path.replace(sdir / path.name)


def _log_sent(paths: "g.Paths", record: dict) -> None:
    try:
        sdir = _sent_dir(paths)
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / f"{secrets.token_hex(4)}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")
    except OSError:
        pass


# --- sending -----------------------------------------------------------------

def send_issue(repo: str, title: str, body: str, timeout: int = 30) -> bool:
    """Create a GitHub issue via `gh`, body through stdin (no shell-quoting).
    Isolated on purpose so tests can mock it without ever touching the network."""
    try:
        result = subprocess.run(
            ["gh", "issue", "create", "--repo", repo, "--title", title, "--body-file", "-"],
            input=body, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def build_preview(paths: "g.Paths", settings: dict) -> dict:
    """Exactly what `send` would transmit, for `list` and for the ask-mode preview."""
    out: dict = {"level": settings["level"], "mode": settings["mode"],
                 "repo": target_repo(settings)}
    if settings["level"] in ("metrics", "reports"):
        out["metrics"] = build_metrics(paths)
    if settings["level"] == "reports":
        qdir = _queue_dir(paths)
        out["reports"] = ([json.loads(p.read_text(encoding="utf-8"))
                           for p in sorted(qdir.glob("*.json"))] if qdir.exists() else [])
    return out


def _send_all(paths: "g.Paths", settings: dict, repo: str, timeout: int) -> bool:
    sent_any = False
    state = _load_state(paths)
    now = datetime.now()
    if settings["level"] in ("metrics", "reports") and _due(state.get("last_metrics_sent"), now, days=7):
        metrics = build_metrics(paths)
        title = f"[metrics] {metrics['engine_version']} {metrics['os']}"
        body = json.dumps(metrics, indent=2, ensure_ascii=False)
        if send_issue(repo, title, body, timeout=timeout):
            state["last_metrics_sent"] = now.isoformat(timespec="seconds")
            _log_sent(paths, {"type": "metrics", "payload": metrics,
                              "sent_at": state["last_metrics_sent"]})
            sent_any = True
    if settings["level"] == "reports" and _queue_dir(paths).exists():
        for path in sorted(_queue_dir(paths).glob("*.json")):
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            hit = _filter_report(report, paths)
            if hit:
                _move_to_quarantine(path, paths)
                print(f"quarantined queued report {report.get('id', '?')}: possible {hit}")
                continue
            metrics = build_metrics(paths)
            title = f"[feedback:{report.get('kind', 'idea')}] {report.get('summary', '')}"
            body = json.dumps({**report, "metrics": metrics}, indent=2, ensure_ascii=False)
            if send_issue(repo, title, body, timeout=timeout):
                _move_to_sent(path, paths)
                sent_any = True
    _save_state(paths, state)
    return sent_any


def maybe_auto_send(paths: "g.Paths") -> None:
    """Called by the orchestrator at the end of `reinforce`. Must never raise
    or noticeably slow anything down: everything here is best-effort."""
    try:
        settings = load_settings(paths)
        if settings.get("level", "off") == "off" or settings.get("mode", "ask") != "auto":
            return
        state = _load_state(paths)
        now = datetime.now()
        if not _due(state.get("last_auto_send"), now, hours=24):
            return
        repo = target_repo(settings)
        if not repo:
            return
        _send_all(paths, settings, repo, timeout=10)
        state = _load_state(paths)
        state["last_auto_send"] = now.isoformat(timespec="seconds")
        _save_state(paths, state)
    except Exception:
        pass


# --- commands ----------------------------------------------------------------

def cmd_status(_args) -> None:
    paths = g.default_paths()
    settings = load_settings(paths)
    repo = target_repo(settings)
    state = _load_state(paths)
    queued = list(_queue_dir(paths).glob("*.json")) if _queue_dir(paths).exists() else []
    quarantined = list(_quarantine_dir(paths).glob("*.json")) if _quarantine_dir(paths).exists() else []
    print(f"level:      {settings['level']}")
    print(f"mode:       {settings['mode']}")
    print(f"repo:       {repo or '(none configured or derivable -- sending unavailable)'}")
    print(f"queued:     {len(queued)}")
    print(f"quarantine: {len(quarantined)}")
    print(f"last metrics sent: {state.get('last_metrics_sent', 'never')}")
    print(f"last auto send:    {state.get('last_auto_send', 'never')}")


def cmd_list(_args) -> None:
    paths = g.default_paths()
    settings = load_settings(paths)
    print(json.dumps(build_preview(paths, settings), indent=2, ensure_ascii=False))


def cmd_send(args) -> None:
    paths = g.default_paths()
    settings = load_settings(paths)
    if settings["level"] == "off":
        print("feedback: level is off, nothing sent")
        return
    repo = target_repo(settings)
    if not repo:
        print("feedback: no target repo configured or derivable; sending unavailable")
        return
    if settings["mode"] == "ask" and not args.yes:
        print(json.dumps(build_preview(paths, settings), indent=2, ensure_ascii=False))
        print("\nfeedback: mode is 'ask' -- show this preview to the user, then rerun "
              "`feedback send --yes` if they agree.")
        return
    sent = _send_all(paths, settings, repo, timeout=30)
    print("feedback: sent" if sent else "feedback: nothing new to send")


def cmd_add(args) -> None:
    paths = g.default_paths()
    settings = load_settings(paths)
    if settings["level"] != "reports":
        sys.exit("feedback add requires level=reports; run `feedback set --level reports` first")
    summary = args.summary.strip()[:200]
    details = (args.details or "").strip()[:1000]
    command = (args.command or "").strip()
    for field_name, text in (("summary", summary), ("details", details), ("command", command)):
        hit = _filter_text(text, paths)
        if hit:
            record = {"id": secrets.token_hex(4), "stage": "queue", "field": field_name,
                      "reason": hit, "created": datetime.now().isoformat(timespec="seconds")}
            try:
                _quarantine_dir(paths).mkdir(parents=True, exist_ok=True)
                (_quarantine_dir(paths) / f"{record['id']}.json").write_text(
                    json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
            except OSError:
                pass
            sys.exit(f"blocked: {field_name} looks like it contains a {hit}; edit it and try again")
    report = {
        "id": secrets.token_hex(4), "kind": args.kind, "command": command,
        "summary": summary, "details": details,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    _queue_dir(paths).mkdir(parents=True, exist_ok=True)
    (_queue_dir(paths) / f"{report['id']}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(f"queued feedback report {report['id']}")


def cmd_set(args) -> None:
    paths = g.default_paths()
    if not args.machine:
        g._require_writable(paths)  # the machine override never touches shared config
    target = _machine_file(paths) if args.machine else paths.config_file
    data = _load_json(target)
    fb = data.get("feedback") if isinstance(data.get("feedback"), dict) else {}
    if args.level:
        fb["level"] = args.level
    if args.mode:
        fb["mode"] = args.mode
    if args.repo:
        fb["repo"] = args.repo
    data["feedback"] = fb
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote feedback settings to {target}")


def register(sub) -> None:
    """Called by graph.py's extension mechanism (see EXTENSIONS)."""
    p = sub.add_parser("feedback", help="opt-in feedback to the engine maintainers")
    fsub = p.add_subparsers(dest="feedback_command", required=True)

    p_status = fsub.add_parser("status", help="show effective feedback settings")
    p_status.set_defaults(func=cmd_status)

    p_list = fsub.add_parser("list", help="preview exactly what would be sent")
    p_list.set_defaults(func=cmd_list)

    p_send = fsub.add_parser("send", help="send queued metrics/reports")
    p_send.add_argument("--yes", action="store_true", help="confirm sending in ask mode")
    p_send.set_defaults(func=cmd_send)

    p_add = fsub.add_parser("add", help="queue a friction/bug/idea report")
    p_add.add_argument("--kind", choices=["friction", "bug", "idea"], required=True)
    p_add.add_argument("--command", required=True, help="engine command this is about")
    p_add.add_argument("--summary", required=True, help="max 200 chars")
    p_add.add_argument("--details", help="max 1000 chars")
    p_add.set_defaults(func=cmd_add)

    p_set = fsub.add_parser("set", help="set feedback level/mode/repo")
    p_set.add_argument("--level", choices=list(LEVELS))
    p_set.add_argument("--mode", choices=list(MODES))
    p_set.add_argument("--repo", help="owner/name override")
    p_set.add_argument("--machine", action="store_true",
                       help="write the per-machine override instead of vault.config.json")
    p_set.set_defaults(func=cmd_set)
