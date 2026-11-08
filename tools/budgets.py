"""Worker budget classes: turn/token limits per subagent role.

Numbers live in `<data>/vault.config.json` under `budgets`; the constants below are
only fallbacks used when the key is missing or invalid:

    "budgets": {
      "context": {"warn_delta": 50000, "handoff_delta": 80000},
      "classes": {"S": {"max_turns": 20, "max_tokens": 80000}, ...},
      "roles": {"worker-low": "S", "worker-medium": "M", "worker-verify": "verify"},
      "task_total_tokens": 600000
    }

Used by tools/models.py (maxTurns in worker-*.md frontmatter) and by the SubagentStop
hook tools/claude-hooks/subagent-log.py (warn at 80%, block at 100%).
Stdlib only; never raises on bad config.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

FALLBACK_CLASSES = {
    "S": {"max_turns": 20, "max_tokens": 80000},
    "M": {"max_turns": 60, "max_tokens": 250000},
    "verify": {"max_turns": 25, "max_tokens": 100000},
}
FALLBACK_ROLES = {"worker-low": "S", "worker-medium": "M", "worker-verify": "verify"}
FALLBACK_TASK_TOTAL = 600000


def _pos_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _fallback() -> dict:
    return {"classes": {k: dict(v) for k, v in FALLBACK_CLASSES.items()},
            "roles": dict(FALLBACK_ROLES), "task_total_tokens": FALLBACK_TASK_TOTAL}


def data_dir_from_env() -> Path | None:
    """VAULT_DATA or VAULT_HOME (expanduser); None if neither is set."""
    raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
    return Path(os.path.expanduser(raw)) if raw else None


def load_budgets(data_dir) -> dict:
    """{"classes": {...}, "roles": {...}, "task_total_tokens": int}.

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
    if _pos_int(block.get("task_total_tokens")):
        result["task_total_tokens"] = block["task_total_tokens"]
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
