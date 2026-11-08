"""Worker budget classes: turn/token limits per subagent role.

Numbers live in `<data>/vault.config.json` under `budgets`; the constants below are
only fallbacks used when the key is missing or invalid:

    "budgets": {
      "context": {"warn_delta": 50000, "handoff_delta": 80000},
      "classes": {"S": {"max_turns": 20, "max_tokens": 80000}, ...},
      "roles": {"worker-low": "S", "worker-medium": "M", "worker-verify": "verify"}
    }

Used by tools/models.py (maxTurns in worker-*.md frontmatter), by the SubagentStop
hook tools/claude-hooks/subagent-log.py (logs warn at 80%, block at 100%) and by the
mid-run PreToolUse budget guard. `transcript_usage` is the single turn/token counter.
Stdlib only; never raises on bad config.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

FALLBACK_CLASSES = {
    "S": {"max_turns": 20, "max_tokens": 80000},
    "M": {"max_turns": 60, "max_tokens": 250000},
    "L": {"max_turns": 150, "max_tokens": 600000},
    "verify": {"max_turns": 25, "max_tokens": 100000},
}
MAX_TURNS_MARGIN = 1.5
FALLBACK_ROLES = {"worker-low": "S", "worker-medium": "M", "worker-verify": "verify"}


def _pos_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _fallback() -> dict:
    return {"classes": {k: dict(v) for k, v in FALLBACK_CLASSES.items()},
            "roles": dict(FALLBACK_ROLES)}


def data_dir_from_env() -> Path | None:
    """VAULT_DATA or VAULT_HOME (expanduser); None if neither is set."""
    raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
    return Path(os.path.expanduser(raw)) if raw else None


def load_budgets(data_dir) -> dict:
    """{"classes": {...}, "roles": {...}}.

    Reads `<data_dir>/vault.config.json` `budgets`. Invalid parts (non-positive or
    non-int numbers, roles pointing at unknown classes) are ignored and replaced by
    the fallbacks; a missing or unreadable file gives the fallbacks."""
    result = _fallback()
    if not data_dir:
        return result
    try:
        with open(Path(data_dir) / "vault.config.json", encoding="utf-8") as f:
            cfg = json.load(f)
        block = cfg.get("budgets") if isinstance(cfg, dict) else None
        if not isinstance(block, dict):
            return result
    except Exception:
        return result
    classes = block.get("classes")
    if isinstance(classes, dict):
        valid = {}
        for name, spec in classes.items():
            if (isinstance(name, str) and isinstance(spec, dict)
                    and _pos_int(spec.get("max_turns")) and _pos_int(spec.get("max_tokens"))):
                valid[name] = {"max_turns": spec["max_turns"], "max_tokens": spec["max_tokens"]}
        if valid:
            result["classes"] = valid
    roles = block.get("roles")
    if isinstance(roles, dict):
        valid_roles = {r: c for r, c in roles.items()
                       if isinstance(r, str) and isinstance(c, str) and c in result["classes"]}
        if valid_roles:
            result["roles"] = valid_roles
    # Roles must always point at an existing class (a classes override can drop one).
    result["roles"] = {r: c for r, c in result["roles"].items() if c in result["classes"]}
    return result


def class_for_role(budgets: dict, role: str) -> str | None:
    """Class name for a role, or None if the role has no budget class."""
    name = (budgets.get("roles") or {}).get(role)
    return name if name in (budgets.get("classes") or {}) else None


def limits_for_role(budgets: dict, role: str) -> tuple[str, int, int] | None:
    """(class_name, max_turns, max_tokens) for a role, or None if it has no class."""
    name = class_for_role(budgets, role)
    if name is None:
        return None
    spec = budgets["classes"][name]
    return name, spec["max_turns"], spec["max_tokens"]


def agent_max_turns(max_turns: int) -> int:
    """The `maxTurns` to write into an agent file for a class `max_turns`:
    ceil(MAX_TURNS_MARGIN * max_turns) (S 20 -> 30, M 60 -> 90, L 150 -> 225, verify 25 -> 38).

    Why a margin: Claude Code stops a subagent at `maxTurns` silently, without running
    any hook, so a cap equal to the class limit would cut the worker off before the
    SubagentStop / PreToolUse budget hooks could ask for a `blocked` report. The margin
    lets those hooks fire first."""
    return math.ceil(MAX_TURNS_MARGIN * max_turns)


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
