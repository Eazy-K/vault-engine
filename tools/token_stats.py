#!/usr/bin/env python3
"""Read-only token usage report over local Claude Code transcripts.

Transcripts live under ``<Claude config dir>/projects/<project-slug>/`` (config dir: $CLAUDE_CONFIG_DIR, else ~/.claude):
  - main sessions:  ``<session-id>.jsonl``
  - subagent runs:  ``<session-id>/subagents/agent-<id>.jsonl`` with a sibling
    ``agent-<id>.meta.json`` holding the agent type (``agentType``).

Every line is a JSON record. Records with ``type == "assistant"`` carry
``message.id`` / ``message.model`` / ``message.usage`` (input_tokens,
cache_creation_input_tokens, cache_read_input_tokens, output_tokens). The same
``message.id`` can appear on several lines (streaming updates); we keep only
the last one seen per id, per file, since that carries the final usage.

This module also reports token usage of local Codex CLI sessions, read from
rollout files under ``~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<id>.jsonl``
(one JSON record per line: ``{"timestamp": ..., "type": ..., "payload": {...}}``).
Relevant record types:
  - ``session_meta``: ``payload.id``, ``payload.timestamp`` (ISO, e.g.
    ``2026-09-27T14:12:50.597Z``).
  - ``turn_context``: ``payload.model``, ``payload.effort`` (can repeat; last wins).
  - ``event_msg`` with ``payload.type == "token_count"``:
    ``payload.info.total_token_usage`` (cumulative for the session, so the last
    one in the file wins; ``info`` may be null) and ``payload.rate_limits``.
Older rollouts have neither of those ``event_msg``/``token_count`` records;
instead each turn emits a ``token_usage_record`` with ``payload.usage`` (a
per-response delta, not cumulative) and ``payload.response_id``. For those
files, session usage is the sum of ``payload.usage`` across all
``token_usage_record`` lines, de-duplicated by ``response_id`` (last one seen
per id wins, in case a response is logged more than once). A file with a
usable ``token_count`` event always uses that instead (the two are never
combined).
Malformed or unrelated lines are skipped.

Cost is a list-price estimate per request and model version (``MODEL_PRICES``).
Cache writes are split by TTL (``usage.cache_creation.ephemeral_5m/1h_input_tokens``);
without that breakdown they are priced as 5m and flagged. A model without a known
price gets cost None plus a warning, never $0. Known limitation: fast mode and
``inference_geo`` multipliers are not applied.

This module never prints file contents or prompts -- only counts, ids and
token numbers -- and never writes anything (read-only).

Standard library only.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from statistics import median

# --- prices (USD per million tokens), a clearly-labelled estimate only ----
# Source: https://platform.claude.com/docs/en/about-claude/pricing (checked
# 2026-10-10). Keyed by model id (version aware); the most specific key wins
# and an optional ``-YYYYMMDD`` date suffix is ignored. Each entry is
# (input, output, cache_read_multiplier). Cache writes: 5m = 1.25x input,
# 1h = 2x input. A model that is not listed is NOT priced (cost None + warning).
#
# Known limitation: fast-mode and ``inference_geo`` (1.1x) multipliers, and
# Batch/Flex discounts, are not applied -- the transcripts do not reliably
# say which applied, so such calls are priced at standard rates.
MODEL_PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.025),
    "claude-mythos-5-1": (10.0, 50.0, 0.025),
    "claude-fable-5": (10.0, 50.0, 0.1),
    "claude-mythos-5": (10.0, 50.0, 0.1),
    "claude-opus-5-5": (4.0, 20.0, 0.05),
    "claude-opus-5": (5.0, 25.0, 0.1),
    "claude-opus-4-8": (5.0, 25.0, 0.1),
    "claude-opus-4-7": (5.0, 25.0, 0.1),
    "claude-opus-4-6": (5.0, 25.0, 0.1),
    "claude-opus-4-5": (5.0, 25.0, 0.1),
    "claude-opus-4-1": (15.0, 75.0, 0.1),
    "claude-opus-4": (15.0, 75.0, 0.1),
    "claude-sonnet-5-5": (2.0, 10.0, 0.05),
    "claude-sonnet-5": (2.0, 10.0, 0.1),
    "claude-sonnet-4-6": (3.0, 15.0, 0.1),
    "claude-sonnet-4-5": (3.0, 15.0, 0.1),
    "claude-sonnet-4": (3.0, 15.0, 0.1),
    "claude-haiku-5-5": (0.10, 0.50, 0.1),
    "claude-haiku-4-5": (1.0, 5.0, 0.1),
    "claude-haiku-3-5": (0.80, 4.0, 0.1),
    "claude-3-5-haiku": (0.80, 4.0, 0.1),
}
# Per-request prompt-length tier: (input, output) used instead when the
# request's whole prompt (input + cache read + cache write) is over the limit.
LONG_PROMPT_TOKENS = 100_000
LONG_PROMPT_PRICES = {
    "claude-haiku-5-5": (0.50, 2.50),
}
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
CACHE_WRITE_MULTIPLIER = CACHE_WRITE_5M_MULTIPLIER  # kept for compatibility
# Standard API list rates, USD per million tokens, verified from the official
# model pages. These produce an API-equivalent estimate, never a billing claim.
# Batch/Flex/Fast rates and long-context surcharges can differ.
CODEX_STANDARD_RATES = {
    "gpt-6-sol": (2.0, 0.20, 2.50, 10.0),
    "gpt-6-luna": (0.10, 0.01, 0.125, 0.50),
    "gpt-6-astra": (10.0, 1.0, 12.50, 50.0),
}
# Sources: https://developers.openai.com/api/docs/models/gpt-6-sol
#          https://developers.openai.com/api/docs/models/gpt-6-luna
#          https://developers.openai.com/api/docs/models/gpt-6-astra
PEAK_CONTEXT_ALERT = 150_000


def _price_key(model: str | None) -> str | None:
    """Most specific MODEL_PRICES key for a model id, or None if unlisted."""
    name = (model or "").lower().replace(".", "-")
    name = re.sub(r"\[.*\]$", "", name)  # e.g. "[1m]" context suffix
    best = None
    for key in MODEL_PRICES:
        if re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?:-\d{8}(?!\d).*)?$", name):
            if best is None or len(key) > len(best):
                best = key
    return best


def estimate_cost_usd(input_tokens: int, cache_creation: int, cache_read: int,
                       output_tokens: int, model: str | None,
                       cache_write_1h: int = 0) -> float | None:
    """Estimated USD cost of ONE request, or None if the model is unpriced.

    ``cache_creation`` is the total of cache-write tokens; ``cache_write_1h``
    is the part of it written with the 1h TTL (2x input), the rest is 5m
    (1.25x input). Models with a prompt-length tier are priced per request.
    """
    key = _price_key(model)
    if key is None:
        return None
    in_price, out_price, read_mult = MODEL_PRICES[key]
    long_prices = LONG_PROMPT_PRICES.get(key)
    if long_prices and input_tokens + cache_read + cache_creation > LONG_PROMPT_TOKENS:
        in_price, out_price = long_prices
    write_1h = min(max(cache_write_1h, 0), cache_creation)
    write_5m = cache_creation - write_1h
    return (input_tokens * in_price
            + write_5m * in_price * CACHE_WRITE_5M_MULTIPLIER
            + write_1h * in_price * CACHE_WRITE_1H_MULTIPLIER
            + cache_read * in_price * read_mult
            + output_tokens * out_price) / 1_000_000


def estimate_codex_cost_usd(usage: dict, model: str | None) -> float | None:
    """Standard-rate API-equivalent estimate; None for an unpriced model."""
    name = (model or "").lower()
    rates = CODEX_STANDARD_RATES.get(name)
    if rates is None:
        return None
    input_price, cached_price, write_price, output_price = rates
    input_tokens = int(usage.get("input_tokens") or 0)
    cached_input_tokens = int(usage.get("cached_input_tokens") or 0)
    cache_write_input_tokens = int(usage.get("cache_write_input_tokens") or 0)
    # OpenAI's prompt-caching formula subtracts both cached and cache-write
    # tokens from input; the Usage API defines input_tokens as including cache:
    # https://developers.openai.com/api/docs/guides/prompt-caching
    # https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/web_search_calls
    regular_input_tokens = max(
        0, input_tokens - cached_input_tokens - cache_write_input_tokens)
    return (regular_input_tokens * input_price
            + cached_input_tokens * cached_price
            + cache_write_input_tokens * write_price
            + int(usage.get("output_tokens") or 0) * output_price) / 1_000_000


def pct(part: float, total: float) -> float | None:
    """Share of ``part`` in ``total`` as a percentage (0-100), or None when the
    total is zero (so callers never divide by zero)."""
    if not total:
        return None
    return round(part * 100.0 / total, 1)


def fmt_usd(value: float | None) -> str:
    """USD for text output; ``-`` when the model is unpriced (never $0.00)."""
    return "-" if value is None else f"${value:,.2f}"


def fmt_write_5m(row: dict) -> str:
    """5m cache-write tokens; a trailing ``~`` marks tokens whose TTL is unknown."""
    return f"{row['cache_write_5m']:,}" + ("~" if row.get("cache_write_unknown") else "")


PRICE_FOOTNOTE = (
    "* USD is a rough estimate from list prices per model version (see "
    "MODEL_PRICES; e.g. opus 5.5 $4/$20, opus 4.5-5 $5/$25, sonnet 5/5.5 $2/$10, "
    "sonnet 4.x $3/$15, haiku 5.5 $0.10/$0.50 and $0.50/$2.50 for prompts over "
    "100K, fable 5.1 $10/$50 per MTok in/out). Cache write 5m 1.25x input, 1h 2x "
    "input; cache read 0.1x input (0.05x opus/sonnet 5.5, 0.025x fable/mythos 5.1). "
    "Known limitation: fast mode and inference_geo multipliers are not applied. "
    "'-' = model without a known price (left out of totals). "
    "~ = cache-write TTL unknown (no 5m/1h breakdown), priced as 5m. "
    "Actual billing may differ.")


def fmt_pct(value: float | None) -> str:
    """Formats a pct() value for text output; ``-`` when there is no share."""
    return "-" if value is None else f"{value:.1f}%"


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
    cache_write_1h: int = 0
    # Cache-write tokens without a 5m/1h breakdown (priced as 5m, flagged).
    cache_write_unknown: int = 0

    @property
    def cache_write_5m(self) -> int:
        return self.cache_creation - self.cache_write_1h

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
        total_write = int(usage.get("cache_creation_input_tokens") or 0)
        breakdown = usage.get("cache_creation")
        write_1h = write_unknown = 0
        if isinstance(breakdown, dict) and (
                "ephemeral_5m_input_tokens" in breakdown
                or "ephemeral_1h_input_tokens" in breakdown):
            write_1h = int(breakdown.get("ephemeral_1h_input_tokens") or 0)
            known = write_1h + int(breakdown.get("ephemeral_5m_input_tokens") or 0)
            write_unknown = max(0, total_write - known)
            total_write = max(total_write, known)
        else:
            write_unknown = total_write
        if msg_id not in by_id:
            order.append(msg_id)
        by_id[msg_id] = Call(
            id=msg_id,
            model=message.get("model"),
            timestamp=ts,
            input=int(usage.get("input_tokens") or 0),
            cache_creation=total_write,
            cache_read=int(usage.get("cache_read_input_tokens") or 0),
            output=int(usage.get("output_tokens") or 0),
            cache_write_1h=write_1h,
            cache_write_unknown=write_unknown,
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


@dataclass
class CodexSession:
    session_id: str | None
    path: Path
    start: datetime | None
    model: str | None
    effort: str | None
    total_token_usage: dict | None
    rate_limits: dict | None
    model_usage: dict[str, dict] | None = None
    model_attribution: str = "unknown"
    observed_models: list[str] = field(default_factory=list)
    calls: int | None = None
    model_calls: dict[str, int] | None = None
    parent_thread_id: str | None = None
    agent_role: str | None = None
    agent_nickname: str | None = None
    thread_ids: set[str] = field(default_factory=set)
    parent_link_known: bool = False


def _read_jsonl_records(path: Path):
    """Yields parsed JSON records from one JSONL file; skips malformed lines.
    Read-only."""
    try:
        raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


_USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                 "output_tokens", "reasoning_output_tokens", "total_tokens")


def parse_codex_rollout(path: Path) -> CodexSession | None:
    """Parses one Codex rollout file into a CodexSession.

    Uses the LAST ``turn_context`` for model/effort and the LAST non-null
    ``token_count`` event for total_token_usage/rate_limits (both are
    cumulative snapshots for the session). Only ids, timestamps, model/effort
    and token numbers are kept -- never prompt or message content.

    Older rollouts have no usable ``token_count`` event; for those, usage is
    summed from ``token_usage_record`` lines' ``payload.usage`` (a per-response
    delta), de-duplicated by ``payload.response_id`` (last one per id wins).
    """
    session_id = None
    parent_thread_id = None
    agent_role = None
    agent_nickname = None
    thread_ids: set[str] = set()
    parent_link_known = False
    start = None
    model = None
    effort = None
    total_token_usage = None
    rate_limits = None
    usage_by_response_id: dict[str, dict] = {}
    response_models: dict[str, str | None] = {}
    usage_order: list[str] = []
    unkeyed_usages: list[dict] = []
    unkeyed_models: list[str | None] = []
    model_by_turn: dict[str, set[str]] = {}
    observed_models: list[str] = []
    for rec in _read_jsonl_records(path):
        if not isinstance(rec, dict):
            continue
        rec_type = rec.get("type")
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        if rec_type == "session_meta":
            session_id = payload.get("id") or session_id
            parent_thread_id = payload.get("parent_thread_id") or parent_thread_id
            parent_link_known = "parent_thread_id" in payload
            agent_role = payload.get("agent_role") or agent_role
            agent_nickname = payload.get("agent_nickname") or agent_nickname
            for identity in (payload.get("id"), payload.get("session_id")):
                if identity:
                    thread_ids.add(str(identity))
            start = _parse_timestamp(payload.get("timestamp")) or start
            model = model or payload.get("model")
        elif rec_type == "turn_context":
            model = payload.get("model") or model
            effort = payload.get("effort") or effort
            context_model = payload.get("model")
            root_turn_id = payload.get("root_turn_id")
            turn_id = payload.get("turn_id")
            if context_model:
                if context_model not in observed_models:
                    observed_models.append(context_model)
                if turn_id is not None:
                    model_by_turn.setdefault(str(turn_id), set()).add(context_model)
        elif rec_type == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            if isinstance(info, dict):
                usage = info.get("total_token_usage")
                if isinstance(usage, dict):
                    total_token_usage = usage
            rl = payload.get("rate_limits")
            if isinstance(rl, dict):
                rate_limits = rl
        elif rec_type == "token_usage_record":
            if payload.get("thread_id"):
                thread_ids.add(str(payload["thread_id"]))
            usage = payload.get("usage")
            if isinstance(usage, dict):
                response_id = payload.get("response_id")
                turn_models = model_by_turn.get(str(payload.get("turn_id")), set())
                turn_model = next(iter(turn_models)) if len(turn_models) == 1 else None
                if response_id:
                    if response_id not in usage_by_response_id:
                        usage_order.append(response_id)
                    usage_by_response_id[response_id] = usage
                    response_models[response_id] = turn_model
                else:
                    # No id to de-dupe by: keep every record so nothing is lost.
                    unkeyed_usages.append(usage)
                    unkeyed_models.append(turn_model)
    if session_id is None:
        # Fall back to the filename so a session with a missing/malformed
        # session_meta record is still counted (its timestamp stays unknown).
        session_id = path.stem
    if total_token_usage is None and (usage_order or unkeyed_usages):
        # No usable token_count event in this (older) rollout: fall back to
        # summing the de-duplicated per-response token_usage_record deltas.
        summed = {field: 0 for field in _USAGE_FIELDS}
        for response_id in usage_order:
            usage = usage_by_response_id[response_id]
            for field in _USAGE_FIELDS:
                summed[field] += int(usage.get(field) or 0)
        for usage in unkeyed_usages:
            for field in _USAGE_FIELDS:
                summed[field] += int(usage.get(field) or 0)
        total_token_usage = summed
    # Per-model allocation is reliable only when each usage record joins to a
    # turn_context by both ids and its aggregate agrees with the authoritative
    # session total. Otherwise retain the total, but report model attribution
    # as unknown instead of assigning the whole session to the last model seen.
    model_usage = None
    calls = None
    model_calls = None
    attribution = "unknown"
    response_usages = [(usage_by_response_id[rid], response_models.get(rid))
                       for rid in usage_order]
    response_usages.extend(zip(unkeyed_usages, unkeyed_models))
    if response_usages and all(m for _, m in response_usages):
        candidate: dict[str, dict[str, int]] = {}
        for usage, response_model in response_usages:
            row = candidate.setdefault(response_model, {field: 0 for field in _USAGE_FIELDS})
            for field in _USAGE_FIELDS:
                row[field] += int(usage.get(field) or 0)
        candidate_total = {field: sum(row[field] for row in candidate.values())
                           for field in _USAGE_FIELDS}
        if total_token_usage is None or all(
                candidate_total[field] == int(total_token_usage.get(field) or 0)
                for field in _USAGE_FIELDS):
            model_usage = candidate
            calls = len(usage_order) if not unkeyed_usages else None
            if not unkeyed_usages:
                model_calls = {}
                for response_id in usage_order:
                    response_model = response_models[response_id]
                    model_calls[response_model] = model_calls.get(response_model, 0) + 1
            attribution = "per_response"
    elif (not response_usages and total_token_usage is not None
          and len(observed_models) == 1):
        # A single observed model can safely label the complete cumulative
        # total even when this rollout format omits per-response usage.
        model_usage = {observed_models[0]: {
            field: int(total_token_usage.get(field) or 0) for field in _USAGE_FIELDS}}
        attribution = "single_model_session"
    return CodexSession(session_id=session_id, path=path, start=start, model=model,
                         effort=effort, total_token_usage=total_token_usage,
                         rate_limits=rate_limits, model_usage=model_usage,
                         model_attribution=attribution, observed_models=observed_models,
                         calls=calls, model_calls=model_calls,
                         parent_thread_id=parent_thread_id,
                         agent_role=agent_role, agent_nickname=agent_nickname,
                         thread_ids=thread_ids, parent_link_known=parent_link_known)


def discover_codex_rollouts(sessions_root: Path):
    """Yields rollout-*.jsonl paths under sessions_root/YYYY/MM/DD/. Read-only."""
    if not sessions_root.is_dir():
        return
    for path in sorted(sessions_root.glob("*/*/*/rollout-*.jsonl")):
        if path.is_file():
            yield path


def collect_codex_sessions(sessions_root: Path, since: date | None = None) -> list[CodexSession]:
    """Loads every Codex rollout under sessions_root into CodexSession objects,
    since-filtered by session start timestamp (undated sessions are dropped
    when --since is given, since they cannot be verified). Read-only."""
    sessions = []
    for path in discover_codex_rollouts(sessions_root):
        session = parse_codex_rollout(path)
        if session is None:
            continue
        if since is not None and (session.start is None
                                   or session.start.astimezone().date() < since):
            continue
        sessions.append(session)
    return sessions


def build_codex_report(sessions: list[CodexSession]) -> dict | None:
    """Aggregates Codex sessions into a plain-data report, or None if there
    are no sessions (distinct from an empty-but-present report)."""
    if not sessions:
        return None

    totals = {"input": 0, "cached_input": 0, "cache_write_input": 0,
              "output": 0, "reasoning_output": 0, "total": 0}
    by_model: dict[str, dict[str, int | None]] = {}
    calls_known = True
    total_calls = 0
    split_known = True
    split_calls_known = True
    main_total_tokens = 0
    subagent_total_tokens = 0
    main_total_calls = 0
    subagent_total_calls = 0
    for s in sessions:
        usage = s.total_token_usage or {}
        input_t = int(usage.get("input_tokens") or 0)
        cached_t = int(usage.get("cached_input_tokens") or 0)
        cache_write_t = int(usage.get("cache_write_input_tokens") or 0)
        output_t = int(usage.get("output_tokens") or 0)
        reasoning_t = int(usage.get("reasoning_output_tokens") or 0)
        total_t = int(usage.get("total_tokens") or 0)

        totals["input"] += input_t
        totals["cached_input"] += cached_t
        totals["cache_write_input"] += cache_write_t
        totals["output"] += output_t
        totals["reasoning_output"] += reasoning_t
        totals["total"] += total_t

        model_usage = s.model_usage or {"unknown": usage}
        for model, model_totals in model_usage.items():
            row = by_model.setdefault(model, {"sessions": 0, "calls": 0,
                                               "input": 0, "cached_input": 0,
                                               "cache_write_input": 0,
                                               "output": 0, "reasoning_output": 0,
                                               "total": 0, "_cost": 0.0,
                                               "_priced": True})
            row["sessions"] = int(row["sessions"] or 0) + 1
            for out_key, usage_key in (("input", "input_tokens"),
                                       ("cached_input", "cached_input_tokens"),
                                       ("cache_write_input", "cache_write_input_tokens"),
                                       ("output", "output_tokens"),
                                       ("reasoning_output", "reasoning_output_tokens"),
                                       ("total", "total_tokens")):
                row[out_key] = int(row[out_key] or 0) + int(model_totals.get(usage_key) or 0)
            model_cost = estimate_codex_cost_usd(model_totals, model)
            if model_cost is None:
                row["_priced"] = False
            elif row["_priced"]:
                row["_cost"] = float(row["_cost"] or 0) + model_cost
            model_call_count = ((s.model_calls or {}).get(model)
                                if s.model_calls is not None else None)
            if model_call_count is None:
                calls_known = False
                row["calls"] = None
            elif row["calls"] is not None:
                row["calls"] = int(row["calls"] or 0) + model_call_count
        if s.calls is None or s.model_calls is None:
            calls_known = False
        else:
            total_calls += s.calls
        if not s.parent_link_known:
            split_known = False
            split_calls_known = False
        elif s.parent_thread_id:
            subagent_total_tokens += total_t
            if s.calls is None:
                split_calls_known = False
            else:
                subagent_total_calls += s.calls
        else:
            main_total_tokens += total_t
            if s.calls is None:
                split_calls_known = False
            else:
                main_total_calls += s.calls

        for model, model_totals in model_usage.items():
            row = by_model[model]
            model_token_count = int(model_totals.get("total_tokens") or 0)
            model_call_count = ((s.model_calls or {}).get(model)
                                if s.model_calls is not None else None)
            if not s.parent_link_known:
                row.setdefault("main_calls", None)
                row.setdefault("subagent_calls", None)
                row.setdefault("main_tokens", None)
                row.setdefault("subagent_tokens", None)
                row["main_calls"] = row["subagent_calls"] = None
                row["main_tokens"] = row["subagent_tokens"] = None
            elif s.parent_thread_id:
                for key, amount in (("subagent_calls", model_call_count),
                                    ("subagent_tokens", model_token_count)):
                    if row.get(key) is None and key in row:
                        continue
                    row[key] = int(row.get(key) or 0) + amount if amount is not None else None
            else:
                for key, amount in (("main_calls", model_call_count),
                                    ("main_tokens", model_token_count)):
                    if row.get(key) is None and key in row:
                        continue
                    row[key] = int(row.get(key) or 0) + amount if amount is not None else None

    total_cost = sum(float(row["_cost"] or 0) for row in by_model.values()
                     if row["_priced"])
    all_priced = all(row["_priced"] for row in by_model.values())
    model_rows = []
    for model, row in sorted(by_model.items()):
        for key in ("main_calls", "subagent_calls", "main_tokens", "subagent_tokens"):
            row.setdefault(key, 0)
        row["subagent_calls_pct"] = (pct(row["subagent_calls"], subagent_total_calls)
                                     if split_known and row.get("subagent_calls") is not None else None)
        row["main_calls_pct"] = (pct(row["main_calls"], main_total_calls)
                                 if split_known and row.get("main_calls") is not None else None)
        row["subagent_tokens_pct"] = (pct(row["subagent_tokens"], subagent_total_tokens)
                                      if split_known and row.get("subagent_tokens") is not None else None)
        row["main_tokens_pct"] = (pct(row["main_tokens"], main_total_tokens)
                                  if split_known and row.get("main_tokens") is not None else None)
        model_rows.append({
            "model": model,
            **{key: value for key, value in row.items() if not key.startswith("_")},
            "sessions_pct": pct(row["sessions"], len(sessions)),
            "total_pct": pct(row["total"], totals["total"]),
            "calls_pct": pct(row["calls"], total_calls) if calls_known else None,
            "est_usd": round(float(row["_cost"] or 0), 4) if row["_priced"] else None,
            "cost_pct": pct(row["_cost"], total_cost) if row["_priced"] else None,
        })

    rate_limits = None
    dated_with_rl = [s for s in sessions if s.start is not None and s.rate_limits]
    if dated_with_rl:
        newest = max(dated_with_rl, key=lambda s: s.start)
        rl = newest.rate_limits
        primary = rl.get("primary") or {}
        secondary = rl.get("secondary") or {}
        rate_limits = {
            "plan_type": rl.get("plan_type"),
            "primary": {"used_percent": primary.get("used_percent"),
                        "window_minutes": primary.get("window_minutes")},
            "secondary": {"used_percent": secondary.get("used_percent"),
                          "window_minutes": secondary.get("window_minutes")},
        }

    return {
        "sessions": len(sessions),
        "totals": totals,
        "calls": total_calls if calls_known else None,
        "cost_basis": "standard_api_list_estimate" if all_priced else "partial_api_list_estimate",
        "cost_estimate_status": "complete" if all_priced else "partial",
        "est_total_usd": round(total_cost, 4),
        "model_attribution": "verified" if all(
            s.model_attribution != "unknown" for s in sessions) else "partial_or_unknown",
        "agent_split_status": "verified" if split_known else "partial_or_unknown",
        "main": {"calls": main_total_calls if split_known and split_calls_known else None,
                 "total_tokens": main_total_tokens if split_known else None},
        "subagents_total": {"calls": subagent_total_calls if split_known and split_calls_known else None,
                            "total_tokens": subagent_total_tokens if split_known else None},
        "by_model": model_rows,
        "rate_limits": rate_limits,
    }


def format_codex_section(codex: dict | None, codex_dir: Path) -> list[str]:
    if codex is None:
        return [f"Codex: no sessions found under {codex_dir}."]
    lines = ["", "Codex sessions (experimental):"]
    t = codex["totals"]
    lines.append(f"  {codex['sessions']} sessions, {t['total']:,} tokens total "
                 f"(input {t['input']:,}, cached input {t['cached_input']:,}, "
                 f"cache write {t['cache_write_input']:,}, "
                 f"output {t['output']:,}, reasoning {t['reasoning_output']:,})")
    lines.append("  By model (sessions, calls, input, cached, output, reasoning, total, "
                 "session %, call %, token %, est. API USD*, cost %):")
    for row in codex["by_model"]:
        calls_text = "-" if row["calls"] is None else f"{row['calls']:,}"
        cost_text = "-" if row["est_usd"] is None else f"${row['est_usd']:,.4f}"
        lines.append(f"    {row['model']:<20} {row['sessions']:>6}  {calls_text:>7}  {row['input']:>12,}  "
                      f"{row['cached_input']:>12,}  {row['cache_write_input']:>12,}  "
                      f"{row['output']:>10,}  "
                      f"{row['reasoning_output']:>10,}  {row['total']:>14,}  "
                      f"{fmt_pct(row['sessions_pct']):>6}  {fmt_pct(row['calls_pct']):>6}  "
                      f"{fmt_pct(row['total_pct']):>6}  {cost_text:>10}  "
                      f"{fmt_pct(row['cost_pct']):>6}")
    lines.append("  * Standard API list-price estimate; actual subscription billing may differ.")
    if codex["cost_estimate_status"] == "partial":
        lines.append("  Cost estimate is partial; total and shares include priced models only.")
    lines.append(f"  Main/subagent attribution: {codex['agent_split_status']}")
    if codex["agent_split_status"] == "verified":
        main_calls = "-" if codex["main"]["calls"] is None else str(codex["main"]["calls"])
        sub_calls = "-" if codex["subagents_total"]["calls"] is None else str(
            codex["subagents_total"]["calls"])
        lines.append(f"    Main: {main_calls} calls, "
                     f"{codex['main']['total_tokens']:,} tokens; "
                     f"subagents: {sub_calls} calls, "
                     f"{codex['subagents_total']['total_tokens']:,} tokens")
        for row in codex["by_model"]:
            main_calls = "-" if row["main_calls"] is None else str(row["main_calls"])
            sub_calls = "-" if row["subagent_calls"] is None else str(row["subagent_calls"])
            main_tokens = "-" if row["main_tokens"] is None else f"{row['main_tokens']:,}"
            sub_tokens = "-" if row["subagent_tokens"] is None else f"{row['subagent_tokens']:,}"
            lines.append(f"    {row['model']}: main {main_calls} calls/{main_tokens} tokens, "
                         f"subagent {sub_calls} calls/{sub_tokens} tokens")
    rl = codex["rate_limits"]
    if rl:
        p, s = rl["primary"], rl["secondary"]
        lines.append(f"  rate limits (plan: {rl['plan_type']}): "
                      f"primary {p['used_percent']}% / {p['window_minutes']}min, "
                      f"secondary {s['used_percent']}% / {s['window_minutes']}min")
    return lines


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


def _new_model_row() -> dict:
    return {"calls": 0, "main_calls": 0, "sub_calls": 0, "input": 0,
            "cache_creation": 0, "cache_write_5m": 0, "cache_write_1h": 0,
            "cache_write_unknown": 0, "cache_read": 0, "output": 0,
            "_cost": 0.0, "_priced": True}


def _add_call(by_model: dict[str, dict], c: Call, is_main: bool) -> None:
    """Adds one request to its model's counters. Cost is accumulated per
    request because the Haiku 5.5 price tier depends on each request."""
    model = c.model or "unknown"
    row = by_model.setdefault(model, _new_model_row())
    row["calls"] += 1
    row["main_calls" if is_main else "sub_calls"] += 1
    row["input"] += c.input
    row["cache_creation"] += c.cache_creation
    row["cache_write_5m"] += c.cache_write_5m
    row["cache_write_1h"] += c.cache_write_1h
    row["cache_write_unknown"] += c.cache_write_unknown
    row["cache_read"] += c.cache_read
    row["output"] += c.output
    cost = _call_cost(c)
    if cost is None:
        row["_priced"] = False
    else:
        row["_cost"] += cost


def _call_cost(c: Call) -> float | None:
    return estimate_cost_usd(c.input, c.cache_creation, c.cache_read, c.output,
                             c.model, cache_write_1h=c.cache_write_1h)


def _model_rows(by_model: dict[str, dict]) -> tuple[list[dict], float, list[str]]:
    """Per-model rows (with total, est. USD and call/token/cost shares) from
    per-model counters, the summed cost of the priced models, and warnings.
    An unpriced model gets ``est_usd`` None (never 0) and a warning."""
    rows = []
    warnings = []
    for model, row in sorted(by_model.items()):
        priced = row["_priced"]
        cost = row["_cost"] if priced else None
        public = {k: v for k, v in row.items() if not k.startswith("_")}
        rows.append({**public, "model": model,
                     "total": row["input"] + row["cache_creation"]
                     + row["cache_read"] + row["output"],
                     "est_usd": None if cost is None else round(cost, 2),
                     "_cost": cost})
        if not priced:
            warnings.append(f"model '{model}' has no known price: its cost is "
                            "not estimated and is left out of the totals")
        if row["cache_write_unknown"]:
            warnings.append(
                f"model '{model}': {row['cache_write_unknown']:,} cache-write tokens "
                "have no 5m/1h breakdown; priced at the 5m rate (1.25x)")
    all_calls = sum(r["calls"] for r in rows)
    all_tokens = sum(r["total"] for r in rows)
    all_cost = sum(r["_cost"] for r in rows if r["_cost"] is not None)
    for r in rows:
        r["calls_pct"] = pct(r["calls"], all_calls)
        r["total_pct"] = pct(r["total"], all_tokens)
        r["cost_pct"] = None if r["_cost"] is None else pct(r["_cost"], all_cost)
        del r["_cost"]
    return rows, all_cost, warnings


def build_report(sessions: list[Session], top: int = 5) -> dict:
    """Aggregates a list of Sessions into a plain-data report (used for both
    the text and --json output)."""
    main_sessions = [s for s in sessions if s.kind == "main" and s.n_calls]
    subagents = [s for s in sessions if s.kind == "subagent" and s.n_calls]

    by_model: dict[str, dict[str, int]] = {}
    for s in sessions:
        for c in s.calls:
            _add_call(by_model, c, s.kind == "main")
    model_rows, total_cost, warnings = _model_rows(by_model)

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
        "cost_estimate_status": "partial" if any(
            r["est_usd"] is None for r in model_rows) else "complete",
        "warnings": warnings,
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
    lines.append("By model (calls, input, cache-write 5m, cache-write 1h, cache-read, output, "
                 "total, est. USD*, call %, token %, cost %, main/sub calls):")
    for row in report["by_model"]:
        lines.append(f"  {row['model']:<24} {row['calls']:>6}  {row['input']:>12,}  "
                      f"{fmt_write_5m(row):>13}  {row['cache_write_1h']:>12,}  "
                      f"{row['cache_read']:>12,}  "
                      f"{row['output']:>10,}  {row['total']:>14,}  {fmt_usd(row['est_usd'])}  "
                      f"{fmt_pct(row['calls_pct']):>6}  {fmt_pct(row['total_pct']):>6}  "
                      f"{fmt_pct(row['cost_pct']):>6}  "
                      f"main {row['main_calls']:,} / sub {row['sub_calls']:,}")
    lines.append(f"  est. total*: ${report['est_total_usd']:,.2f}"
                 + (" (partial; priced models only)"
                    if report.get("cost_estimate_status") == "partial" else ""))
    for warning in report.get("warnings", []):
        lines.append(f"  WARNING: {warning}")
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
    lines.append(PRICE_FOOTNOTE)
    return "\n".join(lines)


class SessionNotFound(Exception):
    """Raised when --session cannot be resolved to a transcript/rollout."""


NO_ENV_NOTE = "note: no session id in the environment; showing the most recent session (a guess)"


def project_slug_for(cwd: Path) -> str:
    """Claude Code's project dir name for a working directory: every
    non-alphanumeric character becomes ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def _find_claude_session(projects_dir: Path, session_id: str) -> list | None:
    """All transcripts of one Claude session (main file first, then its
    subagents) as discover_transcripts tuples, or None if there is no main file."""
    found = [t for t in discover_transcripts(projects_dir)
             if (t[0] == "main" and t[2] == session_id)
             or (t[0] == "subagent" and t[2].startswith(session_id + "/"))]
    if not any(t[0] == "main" for t in found):
        return None
    return sorted(found, key=lambda t: (t[0] != "main", t[2]))


def _find_codex_rollout(codex_dir: Path, session_id: str) -> Path | None:
    for path in discover_codex_rollouts(codex_dir):
        if path.stem.endswith("-" + session_id):
            return path
    return None


def _newest_transcript(projects_dir: Path, cwd: Path) -> str | None:
    """Session id of the most recently modified main transcript for ``cwd``'s
    project dir; the newest overall if that project has none."""
    mains = [t for t in discover_transcripts(projects_dir) if t[0] == "main"]
    mine = [t for t in mains if t[1] == project_slug_for(cwd)]
    pool = mine or mains
    if not pool:
        return None

    def mtime(t):
        try:
            return t[3].stat().st_mtime
        except OSError:
            return 0.0
    return max(pool, key=mtime)[2]


def _cost_of(calls: list[Call]) -> float | None:
    """Summed cost of the calls; None if any call's model is unpriced."""
    costs = [_call_cost(c) for c in calls]
    if any(cost is None for cost in costs):
        return None
    return sum(costs)


def _round_usd(cost: float | None) -> float | None:
    return None if cost is None else round(cost, 2)


def build_claude_session_report(found: list) -> dict:
    """Plain-data report for one Claude session from its transcript tuples."""
    slug = found[0][1]
    session_id = found[0][2]
    main_calls: list[Call] = []
    subs = []
    for kind, _slug, sid, path, agent_type in found:
        calls = load_calls(path)
        if kind == "main":
            main_calls = calls
        else:
            subs.append((sid.split("/", 1)[1], agent_type, calls))
    all_calls = main_calls + [c for _, _, calls in subs for c in calls]

    by_model: dict[str, dict[str, int]] = {}
    for is_main, calls in [(True, main_calls)] + [(False, cs) for _, _, cs in subs]:
        for c in calls:
            _add_call(by_model, c, is_main)
    model_rows, total_cost, warnings = _model_rows(by_model)

    total_tokens = sum(c.total for c in all_calls)
    stamps = sorted(c.timestamp for c in all_calls if c.timestamp)
    start, end = (stamps[0], stamps[-1]) if stamps else (None, None)

    sub_rows = []
    for agent_id, agent_type, calls in subs:
        tokens = sum(c.total for c in calls)
        sub_rows.append({
            "agent_id": agent_id, "agent_type": agent_type,
            "model": ", ".join(sorted({c.model or "unknown" for c in calls})) or "-",
            "calls": len(calls), "total_tokens": tokens,
            "est_usd": _round_usd(_cost_of(calls)),
            "tokens_pct": pct(tokens, total_tokens),
        })
    main_tokens = sum(c.total for c in main_calls)
    sub_tokens = total_tokens - main_tokens
    return {
        "kind": "claude",
        "session_id": session_id,
        "project": slug,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "duration_seconds": int((end - start).total_seconds()) if start and end else None,
        "by_model": model_rows,
        "main": {"calls": len(main_calls), "total_tokens": main_tokens,
                 "est_usd": _round_usd(_cost_of(main_calls)),
                 "tokens_pct": pct(main_tokens, total_tokens)},
        "subagents_total": {"runs": len(subs), "calls": sum(len(cs) for _, _, cs in subs),
                            "total_tokens": sub_tokens,
                            "est_usd": _round_usd(_cost_of([c for _, _, cs in subs for c in cs])),
                            "tokens_pct": pct(sub_tokens, total_tokens)},
        "subagents": sub_rows,
        "total_tokens": total_tokens,
        "est_total_usd": round(total_cost, 2),
        "cost_estimate_status": "partial" if any(
            r["est_usd"] is None for r in model_rows) else "complete",
        "warnings": warnings,
    }


def _codex_cost_for_usage(by_model: dict[str, dict]) -> tuple[float | None, bool]:
    costs = [estimate_codex_cost_usd(usage, model) for model, usage in by_model.items()]
    return sum(cost for cost in costs if cost is not None), all(
        cost is not None for cost in costs)


def build_codex_session_report(session: CodexSession,
                               subagents: list[CodexSession] | None = None) -> dict:
    usage = session.total_token_usage or {}
    total = int(usage.get("total_tokens") or 0)
    main_calls = session.calls
    children = subagents or []
    participants = [(session, "main")] + [(child, "subagent") for child in children]
    child_tokens = [int((child.total_token_usage or {}).get("total_tokens") or 0)
                    for child in children]
    child_calls = [child.calls for child in children]
    child_known_calls = all(value is not None for value in child_calls)
    all_tokens = total + sum(child_tokens)
    aggregate: dict[str, dict] = {}
    all_calls_known = True
    total_calls = 0
    scope_totals = {"input": 0, "cached_input": 0, "cache_write_input": 0,
                    "output": 0, "reasoning_output": 0, "total": 0}
    main_tokens = total
    subagent_tokens = sum(child_tokens)
    main_calls_total = session.calls or 0
    subagent_calls_total = 0
    all_models_attributed = True
    attribution_values = []
    for participant, group in participants:
        participant_usage = participant.total_token_usage or {}
        for total_key, usage_key in (("input", "input_tokens"),
                                     ("cached_input", "cached_input_tokens"),
                                     ("cache_write_input", "cache_write_input_tokens"),
                                     ("output", "output_tokens"),
                                     ("reasoning_output", "reasoning_output_tokens"),
                                     ("total", "total_tokens")):
            scope_totals[total_key] += int(participant_usage.get(usage_key) or 0)
        participant_models = participant.model_usage or {"unknown": participant_usage}
        attribution_values.append(participant.model_attribution)
        if participant.model_attribution == "unknown":
            all_models_attributed = False
        if participant.calls is None or participant.model_calls is None:
            all_calls_known = False
        else:
            total_calls += participant.calls
            if group == "subagent":
                subagent_calls_total += participant.calls
        for model, model_usage in participant_models.items():
            row = aggregate.setdefault(model, {
                "input": 0, "cached_input": 0, "cache_write_input": 0,
                "output": 0, "reasoning_output": 0, "total": 0,
                "calls": 0, "main_calls": 0, "subagent_calls": 0,
                "main_tokens": 0, "subagent_tokens": 0,
                "calls_known": True, "main_calls_known": True,
                "subagent_calls_known": True, "priced": True, "cost": 0.0,
            })
            for out_key, usage_key in (("input", "input_tokens"),
                                       ("cached_input", "cached_input_tokens"),
                                       ("cache_write_input", "cache_write_input_tokens"),
                                       ("output", "output_tokens"),
                                       ("reasoning_output", "reasoning_output_tokens"),
                                       ("total", "total_tokens")):
                row[out_key] += int(model_usage.get(usage_key) or 0)
            row["main_tokens" if group == "main" else "subagent_tokens"] += int(
                model_usage.get("total_tokens") or 0)
            model_call_count = ((participant.model_calls or {}).get(model)
                                if participant.model_calls is not None else None)
            if model_call_count is None:
                row["calls_known"] = False
                row[f"{group}_calls_known"] = False
            else:
                row["calls"] += model_call_count
                row[f"{group}_calls"] += model_call_count
            cost = estimate_codex_cost_usd(model_usage, model)
            if cost is None:
                row["priced"] = False
            elif row["priced"]:
                row["cost"] += cost
    total_cost = sum(row["cost"] for row in aggregate.values() if row["priced"])
    all_priced = all(row["priced"] for row in aggregate.values())
    model_rows = []
    for model, row in sorted(aggregate.items()):
        model_rows.append({
            "model": model,
            **{key: row[key] for key in ("input", "cached_input", "cache_write_input",
                                         "output", "reasoning_output", "total")},
            "calls": row["calls"] if row["calls_known"] else None,
            "calls_pct": pct(row["calls"], total_calls) if all_calls_known else None,
            "total_pct": pct(row["total"], all_tokens),
            "main_calls": row["main_calls"] if row["main_calls_known"] else None,
            "subagent_calls": row["subagent_calls"] if row["subagent_calls_known"] else None,
            "main_calls_pct": pct(row["main_calls"], main_calls_total)
            if row["main_calls_known"] and main_calls_total else None,
            "subagent_calls_pct": pct(row["subagent_calls"], subagent_calls_total)
            if row["subagent_calls_known"] and subagent_calls_total else None,
            "main_tokens": row["main_tokens"],
            "subagent_tokens": row["subagent_tokens"],
            "main_tokens_pct": pct(row["main_tokens"], main_tokens),
            "subagent_tokens_pct": pct(row["subagent_tokens"], subagent_tokens),
            "est_usd": round(row["cost"], 4) if row["priced"] else None,
            "cost_pct": pct(row["cost"], total_cost) if row["priced"] else None,
        })
    child_costs = [(_codex_cost_for_usage(child.model_usage or {
        "unknown": child.total_token_usage or {}})) for child in children]
    children_priced = all(priced for _, priced in child_costs)
    parent_usage_models = session.model_usage or {"unknown": usage}
    parent_cost, parent_priced = _codex_cost_for_usage(parent_usage_models)
    linked = bool(children) or session.parent_link_known
    agent_breakdown = {
        "status": "verified" if linked else "unknown",
        "reason": None if linked else "parent linkage metadata unavailable",
    }
    sub_rows = []
    for child, child_total, child_call_count, (child_cost, child_priced) in zip(
            children, child_tokens, child_calls, child_costs):
        child_model_values = child.model_usage or {"unknown": child.total_token_usage or {}}
        sub_rows.append({
            "session_id": child.session_id,
            "agent_role": child.agent_role or "unknown",
            "agent_nickname": child.agent_nickname,
            "models": sorted(child_model_values),
            "calls": child_call_count,
            "total_tokens": child_total,
            "tokens_pct": pct(child_total, all_tokens),
            "est_usd": round(child_cost, 4) if child_priced and child_cost is not None else None,
        })
    return {
        "kind": "codex",
        "session_id": session.session_id,
        "start": session.start.isoformat() if session.start else None,
        "models": model_rows,
        "totals": scope_totals,
        "main_totals": {"input": int(usage.get("input_tokens") or 0),
                        "cached_input": int(usage.get("cached_input_tokens") or 0),
                        "cache_write_input": int(usage.get("cache_write_input_tokens") or 0),
                        "output": int(usage.get("output_tokens") or 0),
                        "reasoning_output": int(usage.get("reasoning_output_tokens") or 0),
                        "total": total},
        "model_attribution": (attribution_values[0]
                               if all_models_attributed and len(set(attribution_values)) == 1
                               else "verified" if all_models_attributed
                               else "partial_or_unknown"),
        "calls": total_calls if all_calls_known else None,
        "est_total_usd": round(total_cost, 4),
        "cost_basis": "standard_api_list_estimate" if all_priced else "partial_api_list_estimate",
        "cost_estimate_status": "complete" if all_priced else "partial",
        "main": {"calls": main_calls, "total_tokens": total,
                 "tokens_pct": pct(total, all_tokens),
                 "est_usd": round(parent_cost, 4) if parent_priced and parent_cost is not None else None},
        "subagents_total": {"runs": len(children),
                            "calls": sum(v for v in child_calls if v is not None)
                            if child_known_calls else None,
                            "total_tokens": sum(child_tokens),
                            "tokens_pct": pct(sum(child_tokens), all_tokens),
                            "est_usd": round(sum(cost or 0 for cost, _ in child_costs), 4)
                            if children_priced else None},
        "subagents": sub_rows,
        "agent_breakdown": agent_breakdown,
    }


def _fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m {sec:02d}s" if h else f"{m}m {sec:02d}s"


def format_session_text(report: dict) -> str:
    lines = []
    if report.get("note"):
        lines.append(report["note"])
    if report["kind"] == "codex":
        lines.append(f"Codex session {report['session_id']} (experimental)")
        lines.append(f"  start: {report['start'] or '-'}")
        lines.append(f"  model attribution: {report['model_attribution']}")
        lines.append("  By model (calls, input, cached, cache-write, output, reasoning, total, "
                     "call %, token %, est. API USD*, cost %):")
        for r in report["models"]:
            calls = "-" if r["calls"] is None else f"{r['calls']:,}"
            cost = "-" if r["est_usd"] is None else f"${r['est_usd']:,.4f}"
            lines.append(f"    {r['model']:<20} {calls:>7}  {r['input']:>12,}  "
                         f"{r['cached_input']:>12,}  {r['cache_write_input']:>12,}  "
                         f"{r['output']:>10,}  {r['reasoning_output']:>10,}  "
                         f"{r['total']:>14,}  {fmt_pct(r['calls_pct']):>6}  "
                         f"{fmt_pct(r['total_pct']):>6}  {cost:>10}  "
                         f"{fmt_pct(r['cost_pct']):>6}")
        main, agents = report["main"], report["subagents_total"]
        main_calls = "-" if main["calls"] is None else str(main["calls"])
        agent_calls = "-" if agents["calls"] is None else str(agents["calls"])
        lines.append(f"  Main session: {main_calls} calls, {main['total_tokens']:,} tokens "
                     f"({fmt_pct(main['tokens_pct'])})")
        if report["agent_breakdown"]["status"] == "verified":
            lines.append(f"  Subagents: {agents['runs']} runs, {agent_calls} calls, "
                         f"{agents['total_tokens']:,} tokens ({fmt_pct(agents['tokens_pct'])})")
            if report["subagents"]:
                lines.append("  Subagent runs (role, model, calls, tokens, token %):")
                for sub in report["subagents"]:
                    calls = "-" if sub["calls"] is None else str(sub["calls"])
                    model_list = ",".join(sub["models"])
                    lines.append(f"    {sub['session_id']} {sub['agent_role']:<12} "
                                 f"{model_list:<22} {calls:>6}  "
                                 f"{sub['total_tokens']:>14,}  {fmt_pct(sub['tokens_pct']):>6}")
        else:
            lines.append("  Subagent breakdown: unknown (linkage metadata unavailable)")
        total_cost = report["est_total_usd"]
        lines.append("  Estimated standard API cost: "
                     + (f"${total_cost:,.4f}" if total_cost is not None else "unavailable")
                     + (" (partial; priced models only)"
                        if report["cost_estimate_status"] == "partial" else ""))
        lines.append("  * Standard API list-price estimate; subscription billing may differ.")
        return "\n".join(lines)
    lines.append(f"Claude session {report['session_id']}")
    lines.append(f"  project:  {report['project']}")
    lines.append(f"  start:    {report['start'] or '-'}")
    lines.append(f"  end:      {report['end'] or '-'}")
    lines.append(f"  duration: {_fmt_duration(report['duration_seconds'])}")
    lines.append("")
    lines.append("By model (calls, input, cache-write 5m, cache-write 1h, cache-read, output, "
                 "total, est. USD*, call %, token %, cost %):")
    for r in report["by_model"]:
        lines.append(f"  {r['model']:<24} {r['calls']:>6}  {r['input']:>12,}  "
                     f"{fmt_write_5m(r):>13}  {r['cache_write_1h']:>12,}  "
                     f"{r['cache_read']:>12,}  {r['output']:>10,}  "
                     f"{r['total']:>14,}  {fmt_usd(r['est_usd'])}  {fmt_pct(r['calls_pct']):>6}  "
                     f"{fmt_pct(r['total_pct']):>6}  {fmt_pct(r['cost_pct']):>6}")
    lines.append(f"  est. total*: ${report['est_total_usd']:,.2f}  "
                 f"({report['total_tokens']:,} tokens)"
                 + ("  (partial; priced models only)"
                    if report.get("cost_estimate_status") == "partial" else ""))
    for warning in report.get("warnings", []):
        lines.append(f"  WARNING: {warning}")
    lines.append("")
    m, a = report["main"], report["subagents_total"]
    lines.append(f"Main (orchestrator): {m['calls']} calls, {m['total_tokens']:,} tokens "
                 f"({fmt_pct(m['tokens_pct'])}), {fmt_usd(m['est_usd'])}")
    lines.append(f"Subagents:           {a['runs']} runs, {a['calls']} calls, "
                 f"{a['total_tokens']:,} tokens ({fmt_pct(a['tokens_pct'])}), {fmt_usd(a['est_usd'])}")
    if report["subagents"]:
        lines.append("")
        lines.append("Subagent runs (type, model, calls, total tokens, est. USD*, token %):")
        for r in report["subagents"]:
            lines.append(f"  {r['agent_type']:<20} {r['model']:<24} {r['calls']:>5}  "
                         f"{r['total_tokens']:>14,}  {fmt_usd(r['est_usd'])}  "
                         f"{fmt_pct(r['tokens_pct']):>6}")
    lines.append("")
    lines.append(PRICE_FOOTNOTE)
    return "\n".join(lines)


def run_session(projects_dir: Path, codex_dir: Path, session: str, cwd: Path | None = None,
                env=None, as_json: bool = False) -> str:
    """Report for one session: an explicit id, or ``current`` (Claude env id,
    else Codex env id, else the newest transcript of ``cwd``'s project, with a
    "guess" note). Raises SessionNotFound when nothing matches."""
    env = os.environ if env is None else env
    cwd = Path.cwd() if cwd is None else cwd
    note = None
    if session == "current":
        session = (env.get("CLAUDE_CODE_SESSION_ID") or env.get("CODEX_SESSION_ID")
                   or env.get("CODEX_THREAD_ID") or "")
        if not session:
            session = _newest_transcript(projects_dir, cwd) or ""
            if not session:
                raise SessionNotFound(f"no session id in the environment and no transcripts "
                                      f"under {projects_dir}")
            note = NO_ENV_NOTE
    found = _find_claude_session(projects_dir, session)
    if found is not None:
        report = build_claude_session_report(found)
    else:
        rollout = _find_codex_rollout(codex_dir, session)
        parsed = parse_codex_rollout(rollout) if rollout else None
        if parsed is None:
            raise SessionNotFound(f"session {session!r} not found under {projects_dir} "
                                  f"or {codex_dir}")
        children = []
        if parsed.thread_ids:
            candidates = [child for child_path in discover_codex_rollouts(codex_dir)
                          if child_path != parsed.path
                          and (child := parse_codex_rollout(child_path)) is not None]
            known_parent_ids = set(parsed.thread_ids)
            included_paths = {parsed.path}
            while True:
                linked = [child for child in candidates
                          if child.path not in included_paths
                          and child.parent_thread_id in known_parent_ids]
                if not linked:
                    break
                children.extend(linked)
                for child in linked:
                    included_paths.add(child.path)
                    known_parent_ids.update(child.thread_ids)
        report = build_codex_session_report(parsed, children)
    if note:
        report["note"] = note
    if as_json:
        return json.dumps(report, indent=2, sort_keys=False)
    return format_session_text(report)


def run(projects_dir: Path, since: date | None = None, top: int = 5,
        as_json: bool = False, codex_dir: Path | None = None) -> str:
    """Builds and formats the full report; the only entry point graph.py needs."""
    sessions = collect_sessions(projects_dir, since=since)
    report = build_report(sessions, top=top)
    if codex_dir is None:
        codex_dir = Path("~/.codex/sessions").expanduser()
    codex_sessions = collect_codex_sessions(codex_dir, since=since)
    report["codex"] = build_codex_report(codex_sessions)
    if as_json:
        return json.dumps(report, indent=2, sort_keys=False)
    text = format_report_text(report, projects_dir, since)
    text += "\n" + "\n".join(format_codex_section(report["codex"], codex_dir))
    return text
