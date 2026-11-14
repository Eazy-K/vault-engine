"""Worker turn caps: one number per subagent role.

Numbers live in `<data>/vault.config.json` under `budgets`; the constants below are
only fallbacks used when the key is missing or invalid:

    "budgets": {
      "roles": {"worker-low": 20, "worker-medium": 60, "worker-verify": 25},
      "context": {"warn_delta": 50000, "handoff_delta": 80000}
    }

Transition: the old shape (`classes` {name: {max_turns, max_tokens}} plus `roles`
{role: class name}) is still read; role -> class -> max_turns, max_tokens is ignored.

Used by tools/models.py (maxTurns in worker-*.md frontmatter, ceil(cap * 1.5)), by the
SubagentStop hook tools/claude-hooks/subagent-log.py (logs warn at 80%, block at 100%
of the turn cap) and by the mid-run PreToolUse budget guard. Tokens are only measured.
`transcript_usage` is the single turn/token counter. Stdlib only; never raises on bad config.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

FALLBACK_ROLES = {"worker-low": 20, "worker-medium": 60, "worker-verify": 25,
                  "worker-design": 60, "worker-design-verify": 25}
MAX_TURNS_MARGIN = 1.5


def _pos_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def data_dir_from_env() -> Path | None:
    """VAULT_DATA or VAULT_HOME (expanduser); None if neither is set."""
    raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
    return Path(os.path.expanduser(raw)) if raw else None


def _read_block(data_dir) -> dict | None:
    if not data_dir:
        return None
    try:
        with open(Path(data_dir) / "vault.config.json", encoding="utf-8") as f:
            cfg = json.load(f)
        block = cfg.get("budgets") if isinstance(cfg, dict) else None
        return block if isinstance(block, dict) else None
    except Exception:
        return None


def is_old_shape(block) -> bool:
    """True if a `budgets` block uses the retired class shape (`classes`, or a role
    mapped to a class name instead of a number)."""
    if not isinstance(block, dict):
        return False
    if "classes" in block:
        return True
    roles = block.get("roles")
    return isinstance(roles, dict) and any(isinstance(v, str) for v in roles.values())


def old_shape_in_config(data_dir) -> bool:
    return is_old_shape(_read_block(data_dir))


def load_budgets(data_dir) -> dict:
    """{"roles": {role: turn cap}}.

    Reads `<data_dir>/vault.config.json` `budgets.roles`. Invalid values (non-positive or
    non-int) fall back per role; a missing or unreadable file gives the fallbacks.
    Old shape (role -> class name, `classes` with max_turns) is resolved to a number."""
    roles = dict(FALLBACK_ROLES)
    block = _read_block(data_dir)
    if block is None:
        return {"roles": roles}
    classes = block.get("classes") if isinstance(block.get("classes"), dict) else {}
    cfg_roles = block.get("roles")
    if isinstance(cfg_roles, dict):
        for role, value in cfg_roles.items():
            if not isinstance(role, str):
                continue
            if isinstance(value, str):
                spec = classes.get(value)
                value = spec.get("max_turns") if isinstance(spec, dict) else None
            if _pos_int(value):
                roles[role] = value
    return {"roles": roles}


def role_turn_cap(budgets: dict, role: str) -> int | None:
    """Turn cap for a role, or None if the role has none."""
    value = (budgets.get("roles") or {}).get(role)
    return value if _pos_int(value) else None


def agent_max_turns(turn_cap: int) -> int:
    """The `maxTurns` to write into an agent file for a role turn cap:
    ceil(MAX_TURNS_MARGIN * cap) (20 -> 30, 60 -> 90, 25 -> 38).

    Why a margin: Claude Code stops a subagent at `maxTurns` silently, without running
    any hook, so a cap equal to the role limit would cut the worker off before the
    SubagentStop / PreToolUse budget hooks could ask for a `blocked` report. The margin
    lets those hooks fire first."""
    return math.ceil(MAX_TURNS_MARGIN * turn_cap)


def transcript_usage(path) -> tuple[int, int | None]:
    """(turns, tokens) of a Claude transcript JSONL (one record per line).

    turns = distinct `message.id` among `{"type":"assistant"}` records (one API reply is
    written as several records sharing an id); a record whose message has no id counts as
    one turn on its own. tokens = input_tokens + cache_creation_input_tokens +
    cache_read_input_tokens of the last assistant record's usage, or None without usage.
    Missing/unreadable file or bad lines give (0, None) / are skipped."""
    ids: set = set()
    anonymous = 0
    tokens = None
    try:
        if not path or not os.path.isfile(str(path)):
            return 0, None
        with open(str(path), "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "assistant":
                    continue
                msg = obj.get("message")
                mid = msg.get("id") if isinstance(msg, dict) else None
                if isinstance(mid, str) and mid:
                    ids.add(mid)
                else:
                    anonymous += 1
                usage = msg.get("usage") if isinstance(msg, dict) else None
                if isinstance(usage, dict):
                    tokens = sum(int(usage.get(k) or 0) for k in (
                        "input_tokens", "cache_creation_input_tokens",
                        "cache_read_input_tokens"))
    except Exception:
        pass
    return len(ids) + anonymous, tokens
