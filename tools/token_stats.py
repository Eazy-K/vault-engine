#!/usr/bin/env python3
"""Read-only token usage report over local Claude Code transcripts.

Transcripts live under ``~/.claude/projects/<project-slug>/``:
  - main sessions:  ``<session-id>.jsonl``
  - subagent runs:  ``<session-id>/subagents/agent-<id>.jsonl`` with a sibling
    ``agent-<id>.meta.json`` holding the agent type (``agentType``).

Every line is a JSON record. Records with ``type == "assistant"`` carry
``message.id`` / ``message.model`` / ``message.usage`` (input_tokens,
cache_creation_input_tokens, cache_read_input_tokens, output_tokens). The same
``message.id`` can appear on several lines (streaming updates); we keep only
the last one seen per id, per file, since that carries the final usage.

This module never prints file contents or prompts -- only counts, ids and
token numbers -- and never writes anything (read-only).

Standard library only.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from statistics import median

# --- prices (USD per million tokens), a clearly-labelled estimate only ----
# input, output. Matched against message.model by substring (case-insensitive).
PRICES = {
    "opus": (5.0, 25.0),
    "sonnet": (2.0, 10.0),
    "haiku": (1.0, 5.0),
}
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25
PEAK_CONTEXT_ALERT = 150_000


def _price_for(model: str | None) -> tuple[float, float] | None:
    name = (model or "").lower()
    for key, prices in PRICES.items():
        if key in name:
            return prices
    return None


def estimate_cost_usd(input_tokens: int, cache_creation: int, cache_read: int,
                       output_tokens: int, model: str | None) -> float:
    """Estimated USD cost for one usage tuple, or 0.0 if the model is unknown."""
    prices = _price_for(model)
    if prices is None:
        return 0.0
    in_price, out_price = prices
    return (input_tokens * in_price
            + cache_creation * in_price * CACHE_WRITE_MULTIPLIER
            + cache_read * in_price * CACHE_READ_MULTIPLIER
            + output_tokens * out_price) / 1_000_000


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Call:
    id: str
    model: str | None
    timestamp: datetime | None
    input: int
    cache_creation: int
    cache_read: int
    output: int

    @property
    def context(self) -> int:
        return self.input + self.cache_creation + self.cache_read

    @property
    def total(self) -> int:
        return self.context + self.output


def load_calls(path: Path, since: date | None = None) -> list[Call]:
    """Deduped assistant calls from one transcript file, in file order.

    If ``since`` is given, lines whose timestamp is missing or older than it
    are dropped (a missing timestamp cannot be verified, so it is excluded
    rather than assumed to be recent).
    """
    by_id: dict[str, Call] = {}
    order: list[str] = []
    try:
        raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "assistant":
            continue
        message = rec.get("message") or {}
        msg_id = message.get("id")
        # "<synthetic>" marks client-side placeholder messages with no API call.
        if not msg_id or message.get("model") == "<synthetic>":
            continue
        ts = _parse_timestamp(rec.get("timestamp"))
        # --since is a local calendar date; transcript timestamps are UTC.
        if since is not None and (ts is None or ts.astimezone().date() < since):
            continue
        usage = message.get("usage") or {}
        if msg_id not in by_id:
            order.append(msg_id)
        by_id[msg_id] = Call(
            id=msg_id,
            model=message.get("model"),
            timestamp=ts,
            input=int(usage.get("input_tokens") or 0),
            cache_creation=int(usage.get("cache_creation_input_tokens") or 0),
            cache_read=int(usage.get("cache_read_input_tokens") or 0),
            output=int(usage.get("output_tokens") or 0),
        )
    return [by_id[i] for i in order]


@dataclass
class Session:
    kind: str  # "main" or "subagent"
    slug: str
    session_id: str
    agent_type: str | None
    calls: list[Call] = field(default_factory=list)

    @property
    def n_calls(self) -> int:
        return len(self.calls)

    @property
    def first_context(self) -> int:
        return self.calls[0].context if self.calls else 0

    @property
    def peak_context(self) -> int:
        return max((c.context for c in self.calls), default=0)

    @property
    def total_tokens(self) -> int:
        return sum(c.total for c in self.calls)


def discover_transcripts(projects_dir: Path):
    """Yields (kind, slug, session_id, path, agent_type) for every transcript
    file found under projects_dir. Read-only; never opens files here."""
    if not projects_dir.is_dir():
        return
    for slug_dir in sorted(p for p in projects_dir.iterdir() if p.is_dir()):
        try:
            entries = sorted(slug_dir.iterdir())
        except OSError:
            continue
        for item in entries:
            if item.is_file() and item.suffix == ".jsonl":
                yield ("main", slug_dir.name, item.stem, item, None)
            elif item.is_dir():
                sub_dir = item / "subagents"
                if not sub_dir.is_dir():
                    continue
                for agent_file in sorted(sub_dir.glob("agent-*.jsonl")):
                    meta_path = sub_dir / f"{agent_file.stem}.meta.json"
                    agent_type = "unknown"
                    if meta_path.is_file():
                        try:
                            meta = json.loads(meta_path.read_text(encoding="utf-8"))
                            agent_type = meta.get("agentType") or "unknown"
                        except (OSError, json.JSONDecodeError):
                            pass
                    yield ("subagent", slug_dir.name, f"{item.name}/{agent_file.stem}",
                           agent_file, agent_type)


def collect_sessions(projects_dir: Path, since: date | None = None) -> list[Session]:
    """Loads every session/subagent transcript into Session objects (calls
    already deduped and since-filtered). Read-only."""
    sessions = []
    for kind, slug, session_id, path, agent_type in discover_transcripts(projects_dir):
        calls = load_calls(path, since=since)
        sessions.append(Session(kind=kind, slug=slug, session_id=session_id,
                                 agent_type=agent_type, calls=calls))
    return sessions


def percentile(values: list[int | float], pct: float) -> float:
    """Linear-interpolation percentile (pct in [0, 1]); 0 for an empty list."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * pct
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return float(s[int(k)])
    return s[lo] * (hi - k) + s[hi] * (k - lo)


def build_report(sessions: list[Session], top: int = 5) -> dict:
    """Aggregates a list of Sessions into a plain-data report (used for both
    the text and --json output)."""
    main_sessions = [s for s in sessions if s.kind == "main" and s.n_calls]
    subagents = [s for s in sessions if s.kind == "subagent" and s.n_calls]

    by_model: dict[str, dict[str, int]] = {}
    for s in sessions:
        for c in s.calls:
            model = c.model or "unknown"
            row = by_model.setdefault(model, {"calls": 0, "input": 0, "cache_creation": 0,
                                               "cache_read": 0, "output": 0})
            row["calls"] += 1
            row["input"] += c.input
            row["cache_creation"] += c.cache_creation
            row["cache_read"] += c.cache_read
            row["output"] += c.output
    model_rows = []
    total_cost = 0.0
    for model, row in sorted(by_model.items()):
        cost = estimate_cost_usd(row["input"], row["cache_creation"], row["cache_read"],
                                  row["output"], model)
        total_cost += cost
        model_rows.append({**row, "model": model,
                            "total": row["input"] + row["cache_creation"]
                            + row["cache_read"] + row["output"],
                            "est_usd": round(cost, 2)})

    top_main = sorted(main_sessions, key=lambda s: -s.total_tokens)[:top]

    by_agent_type: dict[str, list[int]] = {}
    for s in subagents:
        by_agent_type.setdefault(s.agent_type or "unknown", []).append(s.n_calls)
    agent_rows = []
    for agent_type, turns in sorted(by_agent_type.items()):
        agent_rows.append({
            "agent_type": agent_type,
            "n": len(turns),
            "median_turns": median(turns),
            "p90_turns": percentile(turns, 0.9),
            "max_turns": max(turns),
        })

    peak_over_alert = sum(1 for s in main_sessions if s.peak_context > PEAK_CONTEXT_ALERT)

    return {
        "by_model": model_rows,
        "est_total_usd": round(total_cost, 2),
        "main_sessions": {
            "count": len(main_sessions),
            "calls": sum(s.n_calls for s in main_sessions),
            "total_tokens": sum(s.total_tokens for s in main_sessions),
            "median_first_context": median([s.first_context for s in main_sessions])
            if main_sessions else 0,
            "peak_context_over_150k": peak_over_alert,
        },
        "subagents": {
            "count": len(subagents),
            "calls": sum(s.n_calls for s in subagents),
            "total_tokens": sum(s.total_tokens for s in subagents),
            "median_first_context": median([s.first_context for s in subagents])
            if subagents else 0,
        },
        "top_main_sessions": [
            {"slug": s.slug, "session_id": s.session_id, "calls": s.n_calls,
             "first_context": s.first_context, "peak_context": s.peak_context,
             "total_tokens": s.total_tokens}
            for s in top_main
        ],
        "subagent_turns_by_type": agent_rows,
    }


def format_report_text(report: dict, projects_dir: Path, since: date | None) -> str:
    lines = [f"Token usage report: {projects_dir}"]
    if since:
        lines.append(f"  since {since.isoformat()}")
    lines.append("")
    lines.append("By model (calls, input, cache-write, cache-read, output, total, est. USD*):")
    for row in report["by_model"]:
        lines.append(f"  {row['model']:<24} {row['calls']:>6}  {row['input']:>12,}  "
                      f"{row['cache_creation']:>12,}  {row['cache_read']:>12,}  "
                      f"{row['output']:>10,}  {row['total']:>14,}  ${row['est_usd']:,.2f}")
    lines.append(f"  est. total*: ${report['est_total_usd']:,.2f}")
    lines.append("")

    m = report["main_sessions"]
    lines.append(f"Main sessions:  {m['count']} sessions, {m['calls']} calls, "
                 f"{m['total_tokens']:,} tokens total")
    lines.append(f"  median first-call context: {m['median_first_context']:,.0f}")
    lines.append(f"  sessions with peak context > {PEAK_CONTEXT_ALERT:,}: "
                 f"{m['peak_context_over_150k']}")

    a = report["subagents"]
    lines.append(f"Subagents:      {a['count']} runs, {a['calls']} calls, "
                 f"{a['total_tokens']:,} tokens total")
    lines.append(f"  median first-call context: {a['median_first_context']:,.0f}")
    lines.append("")

    lines.append(f"Top {len(report['top_main_sessions'])} main sessions by total tokens:")
    for row in report["top_main_sessions"]:
        lines.append(f"  {row['slug']}/{row['session_id']}  calls={row['calls']}  "
                      f"context first={row['first_context']:,} peak={row['peak_context']:,}  "
                      f"total={row['total_tokens']:,} tokens")
    lines.append("")

    lines.append("Subagent turns by type (turn = deduped assistant messages; n, median, p90, max):")
    for row in report["subagent_turns_by_type"]:
        lines.append(f"  {row['agent_type']:<20} n={row['n']:<5} "
                      f"median={row['median_turns']:.0f}  p90={row['p90_turns']:.1f}  "
                      f"max={row['max_turns']}")
    lines.append("")
    lines.append("* USD is a rough estimate from list prices (opus $5/$25, sonnet $2/$10, "
                 "haiku $1/$5 per MTok in/out; cache read 0.1x input, cache write 1.25x "
                 "input) -- actual billing may differ.")
    return "\n".join(lines)


def run(projects_dir: Path, since: date | None = None, top: int = 5,
        as_json: bool = False) -> str:
    """Builds and formats the full report; the only entry point graph.py needs."""
    sessions = collect_sessions(projects_dir, since=since)
    report = build_report(sessions, top=top)
    if as_json:
        return json.dumps(report, indent=2, sort_keys=False)
    return format_report_text(report, projects_dir, since)
