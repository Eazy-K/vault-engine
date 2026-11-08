"""Transcript-based usage metrics (read-only, measurement only).

Two measurements over Claude Code transcripts:
  (a) implicit usage: vault notes the agent opened (``graph.py show --body <id>``
      or a Read of a file under the vault data root) after its first ``context``
      call in the same transcript;
  (b) sessions that never call ``context``, per project slug.

Only counts and note ids / slugs leave this module; transcript text is never
stored or printed.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path

from token_stats import discover_transcripts

MIN_TURNS = 3  # sessions with fewer assistant turns are trivial and skipped in (b)
LAB_MARKERS = ("claude-lab-runs", "lab-orch")
LAB_LABEL = " or ".join(LAB_MARKERS)

_CONTEXT_RE = re.compile(r"""graph\.py["']?\s+(?:--\S+\s+)*context\b""")
_SHOW_RE = re.compile(r"""graph\.py["']?\s+show\s+--body\s+([^|;&<>\n]+)""")
_ID_RE = re.compile(r"^\w[\w./-]*$")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).replace("\\", "/")


def note_id_from_path(file_path: str, vault_root: Path) -> str | None:
    """Note id for a file under the vault root (relative path, no .md), else None."""
    if not file_path:
        return None
    root = _norm(str(vault_root)).rstrip("/") + "/"
    norm = _norm(file_path)
    if not norm.startswith(root) or not norm.endswith(".md"):
        return None
    rel = norm[len(root):-3]
    if not rel or rel.startswith(".graph/") or rel.startswith(".git/"):
        return None
    return rel


def show_ids(command: str) -> list[str]:
    """Note ids from every ``graph.py show --body <id> [<id>...]`` in a command."""
    ids: list[str] = []
    for m in _SHOW_RE.finditer(command):
        for tok in m.group(1).split():
            tok = tok.strip("\"'")
            if _ID_RE.match(tok):
                ids.append(tok)
    return ids


def scan_transcript(path: Path, vault_root: Path) -> dict:
    """Returns {turns, context_calls, opened: set[str]} for one transcript.

    ``opened`` only holds notes opened after the first context call. Tool calls
    are deduped by tool_use id (one message can span several JSONL lines).
    """
    turns: set[str] = set()
    seen: set[str] = set()
    context_calls = 0
    opened: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        message = rec.get("message") or {}
        if message.get("id"):
            turns.add(message["id"])
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tid = block.get("id")
            if tid:
                if tid in seen:
                    continue
                seen.add(tid)
            inp = block.get("input") or {}
            name = block.get("name")
            if name == "Bash":
                cmd = str(inp.get("command") or "")
                if _CONTEXT_RE.search(cmd):
                    context_calls += 1
                elif context_calls:
                    opened.update(show_ids(cmd))
            elif name == "Read" and context_calls:
                nid = note_id_from_path(str(inp.get("file_path") or ""), vault_root)
                if nid:
                    opened.add(nid)
    return {"turns": len(turns), "context_calls": context_calls, "opened": opened}


def collect(projects_dir: Path, vault_root: Path) -> dict:
    """Aggregates (a) and (b) over all transcripts under projects_dir.

    opened counts distinct transcripts (main sessions and subagent runs) that
    opened the note after a context call. Context-ratio rows only use main
    sessions with at least MIN_TURNS assistant turns; lab slugs are listed
    separately and left out of the total.
    """
    opened: Counter = Counter()
    slugs: dict[str, dict] = {}
    skipped = 0
    for kind, slug, _sid, path, _agent in discover_transcripts(projects_dir):
        res = scan_transcript(path, vault_root)
        opened.update(res["opened"])
        if kind != "main":
            continue
        if res["turns"] < MIN_TURNS:
            skipped += 1
            continue
        row = slugs.setdefault(slug, {"sessions": 0, "with_context": 0})
        row["sessions"] += 1
        row["with_context"] += 1 if res["context_calls"] else 0
    rows = [{"slug": slug, "sessions": r["sessions"], "with_context": r["with_context"],
             "ratio": r["with_context"] / r["sessions"], "lab": any(m in slug for m in LAB_MARKERS)}
            for slug, r in sorted(slugs.items())]
    real = [r for r in rows if not r["lab"]]
    sessions = sum(r["sessions"] for r in real)
    with_ctx = sum(r["with_context"] for r in real)
    return {
        "opened": dict(opened),
        "min_turns": MIN_TURNS,
        "skipped_trivial": skipped,
        "slugs": rows,
        "total": {"sessions": sessions, "with_context": with_ctx,
                  "ratio": (with_ctx / sessions) if sessions else None},
    }


def format_sessions(result: dict) -> str:
    out = [f"\nsessions with a context call (main sessions with >= {result['min_turns']} "
           f"assistant turns; {result['skipped_trivial']} trivial skipped; lab slugs "
           f"{LAB_LABEL} listed but excluded from total)",
           "slug                                                    sessions  with-context  ratio"]
    for r in sorted(result["slugs"], key=lambda r: -r["sessions"]):
        tag = " (lab)" if r["lab"] else ""
        out.append(f"{(r['slug'] + tag)[:54]:<56}{r['sessions']:>8}{r['with_context']:>14}"
                   f"{r['ratio']:>7.0%}")
    t = result["total"]
    ratio = "-" if t["ratio"] is None else f"{t['ratio']:.0%}"
    out.append(f"{'TOTAL (excl. lab)':<56}{t['sessions']:>8}{t['with_context']:>14}{ratio:>7}")
    return "\n".join(out)
