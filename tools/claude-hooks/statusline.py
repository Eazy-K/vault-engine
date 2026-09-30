#!/usr/bin/env python3
"""statusLine command for Claude Code: prints a short one-line status,
e.g. "Opus 5.5·med │ vault-engine (fix/windows-hook-quoting) │ ctx 80K/1000K 8% │ $1.42".

Reads the JSON session object Claude Code sends on stdin (see
https://code.claude.com/docs/en/statusline) and prints up to five
segments, joined by " │ ", omitting any segment whose data is missing:

1. Model name and reasoning effort (model.display_name). Effort is read
   from the stdin JSON's `effort.level` field when present (Claude Code
   >= the version that added it); otherwise it falls back to
   ~/.claude/settings.json's `modelSettings[<model.id>].effortLevel` or
   top-level `effortLevel`. If no effort can be determined, only the
   model name is shown.
2. Folder (branch): basename of workspace.current_dir (falling back to
   top-level cwd), plus the current git branch in parentheses when the
   directory is inside a git repository.
3. Context window usage: context_window.current_usage (input_tokens +
   cache_creation_input_tokens + cache_read_input_tokens +
   output_tokens), context_window.context_window_size and
   context_window.used_percentage. `current_usage`/`used_percentage` are
   null before the first model call in a session and right after
   /compact, so that case is reported as "no data yet" rather than as an
   error.
4. Session cost: cost.total_cost_usd formatted as "$1.42".
5. Model shares of this session by total tokens (input + cache + output),
   main transcript plus its subagent transcripts combined, e.g.
   "Opus 65%·Sonnet 35%". Omitted when only one model was used, when
   there is no data, or on any error. Transcripts are parsed
   incrementally (byte offset + per-model totals cached as JSON, under
   CLAUDE_STATUSLINE_CACHE or the OS temp dir), so a refresh only reads
   the bytes appended since the last one. Per-call counting mirrors
   tools/token_stats.py load_calls (assistant messages deduped by
   message id, last one wins, "<synthetic>" skipped).

Any parse error or missing field is handled gracefully -- never a
traceback, never a non-zero exit -- so a bug here can never break the
status line.
"""
from __future__ import annotations

import json
import os
import hashlib
import re
import subprocess
import sys
import tempfile
from glob import glob

# Overridable so tests never read the real ~/.claude/settings.json.
SETTINGS_ENV_VAR = "CLAUDE_STATUSLINE_SETTINGS"

# Overridable so tests never write into the real temp/cache dir.
CACHE_ENV_VAR = "CLAUDE_STATUSLINE_CACHE"
CACHE_VERSION = 1
MAX_SEEN_IDS = 20000  # per file; oldest ids are forgotten beyond this
HEAD_BYTES = 256  # file-identity fingerprint (start of the file)

EFFORT_ABBREV = {
    "low": "low",
    "medium": "med",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}


def context_tokens(usage: dict) -> int:
    return (
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("output_tokens") or 0)
    )


def ctx_segment(data: dict) -> str:
    cw = data.get("context_window") or {}
    usage = cw.get("current_usage")
    size = cw.get("context_window_size")
    pct = cw.get("used_percentage")

    if not isinstance(usage, dict):
        return "ctx --K (no data yet)"

    k = context_tokens(usage) / 1000.0
    size_str = f"/{size // 1000}K" if isinstance(size, (int, float)) and size else ""
    pct_str = f" {pct:.0f}%" if isinstance(pct, (int, float)) else ""
    return f"ctx {k:.0f}K{size_str}{pct_str}"


def _settings_path(settings_path: str | None = None) -> str:
    if settings_path is not None:
        return settings_path
    env_path = os.environ.get(SETTINGS_ENV_VAR)
    if env_path:
        return env_path
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if config_dir:
        return os.path.join(os.path.expanduser(config_dir), "settings.json")
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def effort_from_settings(model_id: str | None, settings_path: str | None = None) -> str | None:
    """Look up an effort level from a settings.json file.

    Checks modelSettings[<model_id>].effortLevel first, then the
    top-level effortLevel field. Returns None if the file is missing,
    unreadable, or has no matching field.
    """
    path = _settings_path(settings_path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except Exception:
        return None
    if not isinstance(settings, dict):
        return None

    if model_id:
        model_settings = settings.get("modelSettings")
        if isinstance(model_settings, dict):
            entry = model_settings.get(model_id)
            if isinstance(entry, dict):
                level = entry.get("effortLevel")
                if isinstance(level, str) and level:
                    return level

    level = settings.get("effortLevel")
    if isinstance(level, str) and level:
        return level
    return None


def model_segment(data: dict, settings_path: str | None = None) -> str | None:
    model = data.get("model")
    if not isinstance(model, dict):
        return None
    name = model.get("display_name")
    if not isinstance(name, str) or not name:
        return None

    effort = None
    effort_data = data.get("effort")
    if isinstance(effort_data, dict):
        level = effort_data.get("level")
        if isinstance(level, str) and level:
            effort = level
    if not effort:
        effort = effort_from_settings(model.get("id"), settings_path)

    if effort:
        abbrev = EFFORT_ABBREV.get(effort, effort)
        return f"{name}·{abbrev}"
    return name


def git_branch(directory: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", directory, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def folder_segment(data: dict) -> str | None:
    workspace = data.get("workspace")
    directory = None
    if isinstance(workspace, dict):
        directory = workspace.get("current_dir")
    if not isinstance(directory, str) or not directory:
        cwd = data.get("cwd")
        directory = cwd if isinstance(cwd, str) and cwd else None
    if not directory:
        return None

    name = os.path.basename(directory.rstrip("/\\")) or directory
    branch = git_branch(directory)
    if branch:
        return f"{name} ({branch})"
    return name


def cost_segment(data: dict) -> str | None:
    cost = data.get("cost")
    if not isinstance(cost, dict):
        return None
    total = cost.get("total_cost_usd")
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return None
    return f"${total:.2f}"


def _cache_dir() -> str:
    env = os.environ.get(CACHE_ENV_VAR)
    if env:
        return env
    return os.path.join(tempfile.gettempdir(), "claude-statusline-cache")


def _cache_path(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:100] or "session"
    return os.path.join(_cache_dir(), f"{safe}.json")


def _load_cache(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        if cache.get("v") == CACHE_VERSION and isinstance(cache.get("files"), dict):
            return cache
    except Exception:
        pass
    return {"v": CACHE_VERSION, "files": {}}


def _save_cache(path: str, cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        pass


def _apply_line(line: bytes, entry: dict) -> None:
    """Fold one transcript line into entry (same rules as token_stats.load_calls)."""
    try:
        rec = json.loads(line)
    except ValueError:
        return
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return
    message = rec.get("message")
    if not isinstance(message, dict):
        return
    msg_id = message.get("id")
    model = message.get("model")
    if not msg_id or not isinstance(msg_id, str) or model == "<synthetic>":
        return
    if not isinstance(model, str) or not model:
        model = "unknown"
    usage = message.get("usage") or {}
    total = sum(int(usage.get(k) or 0) for k in (
        "input_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens", "output_tokens"))
    totals, seen = entry["totals"], entry["seen"]
    old = seen.pop(msg_id, None)  # re-insert so it counts as newest
    if old is not None:
        totals[old[0]] = totals.get(old[0], 0) - old[1]
    seen[msg_id] = [model, total]
    totals[model] = totals.get(model, 0) + total
    while len(seen) > MAX_SEEN_IDS:
        del seen[next(iter(seen))]


def _valid_entry(entry) -> bool:
    return (isinstance(entry, dict) and isinstance(entry.get("off"), int)
            and isinstance(entry.get("fp"), str)
            and isinstance(entry.get("totals"), dict)
            and isinstance(entry.get("seen"), dict))


def _update_file(path: str, entry: dict | None) -> dict | None:
    """Advance one file's cache entry by reading only bytes appended since
    the last run; reparse from 0 if the file shrank or was replaced."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            head = f.read(min(HEAD_BYTES, size))
            fp = hashlib.sha1(head).hexdigest()
            if not _valid_entry(entry) or entry["off"] > size or (
                    entry["off"] >= len(head) and entry["fp"] != fp):
                entry = None
            if entry is None:
                entry = {"off": 0, "fp": fp, "totals": {}, "seen": {}}
            f.seek(entry["off"])
            chunk = f.read()
    except OSError:
        return None
    if entry["off"] < HEAD_BYTES:
        entry["fp"] = fp  # the head was still growing when last cached
    end = chunk.rfind(b"\n")
    if end >= 0:  # an unterminated last line is left for the next run
        for line in chunk[:end].split(b"\n"):
            if line.strip():
                _apply_line(line, entry)
        entry["off"] += end + 1
    return entry


def session_model_totals(data: dict) -> dict[str, int]:
    """Per-model total tokens for the session (main + subagents), cached."""
    transcript = data.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return {}
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = os.path.splitext(os.path.basename(transcript))[0]
    sub_glob = os.path.join(os.path.splitext(transcript)[0], "subagents", "agent-*.jsonl")
    paths = [transcript] + sorted(glob(sub_glob))

    cache_file = _cache_path(session_id)
    cache = _load_cache(cache_file)
    old_files = cache["files"]
    new_files = {}
    changed = set(old_files) != set(paths)
    for p in paths:
        before = old_files.get(p)
        before_off = before.get("off") if isinstance(before, dict) else None
        entry = _update_file(p, before)
        if entry is not None:
            new_files[p] = entry
            changed = changed or entry["off"] != before_off
    if changed:
        cache["files"] = new_files
        _save_cache(cache_file, cache)

    merged: dict[str, int] = {}
    for entry in new_files.values():
        for model, n in entry["totals"].items():
            merged[model] = merged.get(model, 0) + n
    return merged


def short_model_name(model_id: str) -> str:
    lower = model_id.lower()
    for family in ("opus", "sonnet", "haiku", "fable"):
        if family in lower:
            return family.capitalize()
    return re.sub(r"^claude-", "", lower)[:16]


def model_shares_segment(data: dict) -> str | None:
    try:
        totals = {m: n for m, n in session_model_totals(data).items() if n > 0}
        grand = sum(totals.values())
        if grand <= 0:
            return None
        shares: dict[str, float] = {}
        for model, n in totals.items():
            name = short_model_name(model)
            shares[name] = shares.get(name, 0) + n
        if len(shares) < 2:
            return None
        parts = [(name, round(100 * n / grand)) for name, n in
                 sorted(shares.items(), key=lambda kv: -kv[1])]
        parts = [f"{name} {pct}%" for name, pct in parts if pct >= 1]
        return "·".join(parts) if len(parts) >= 2 else None
    except Exception:
        return None


def format_line(data: dict, settings_path: str | None = None) -> str:
    segments = [
        model_segment(data, settings_path),
        folder_segment(data),
        ctx_segment(data),
        cost_segment(data),
        model_shares_segment(data),
    ]
    return " │ ".join(s for s in segments if s)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        print("ctx ?? (bad input)")
        return

    try:
        print(format_line(data))
    except Exception:
        print("ctx ??")


if __name__ == "__main__":
    main()
