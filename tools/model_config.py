"""Single source of truth for Claude model ranking (haiku < sonnet < opus) and
for layering the `models` config across the shared `vault.config.json` and a
computer's own `.graph/machine.json` (machine wins per key, like
`project_roots` -- see graph.py's `project_roots`).

Used by `graph.py models`, `doctor`, and (via a relative import guarded with
try/except, since that script runs standalone) `agent-guard.py`. Stdlib only.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# Cheapest to most expensive. A model not listed here is unknown: it never
# counts as "cheaper than" anything, so it is never offered to a worker.
MODEL_RANK = {"haiku": 0, "sonnet": 1, "opus": 2}

ROLES = ("orchestrator", "worker-low", "worker-medium")

# Built-in defaults: exactly today's behaviour (before this config existed).
DEFAULT_MODELS = {
    "orchestrator": {"model": "opus", "effort": "medium"},
    "worker-low": {"model": "sonnet", "effort": "low"},
    "worker-medium": {"model": "sonnet", "effort": "medium"},
}


_VARIANT_SUFFIX = re.compile(r"\[[^\]]*\]$")


def has_variant(model: str | None) -> bool:
    """True if `model` carries a trailing [variant] suffix, e.g. "opus[1m]"."""
    return isinstance(model, str) and bool(_VARIANT_SUFFIX.search(model.strip()))


def base_model(model: str | None) -> str | None:
    """`model` without a trailing [variant] suffix (e.g. "opus[1m]" -> "opus"),
    stripped and lowercased. None if `model` isn't a string."""
    if not isinstance(model, str):
        return None
    return _VARIANT_SUFFIX.sub("", model.strip()).strip().lower()


def rank(model: str | None) -> int | None:
    """This model's rank (0 = cheapest), or None if it isn't a known model.
    A [variant] suffix such as "[1m]" is ignored."""
    base = base_model(model)
    if base is None:
        return None
    return MODEL_RANK.get(base)


def is_cheaper(model: str | None, than: str | None) -> bool:
    """True if `model` is strictly cheaper than `than`; False if either is unknown."""
    a, b = rank(model), rank(than)
    return a is not None and b is not None and a < b


def cheaper_than(model: str | None) -> list[str]:
    """Known models strictly cheaper than `model`, cheapest first. Empty if
    `model` is unknown or already the cheapest known model."""
    r = rank(model)
    if r is None:
        return []
    return [name for name, n in sorted(MODEL_RANK.items(), key=lambda kv: kv[1]) if n < r]


def _load_json_dict(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_layered(shared_path: Path, machine_path: Path) -> tuple[dict, dict]:
    """(effective, sources).

    effective: role -> {"model": ..., "effort": ...}, starting from
    DEFAULT_MODELS, then overlaid by `shared_path`'s "models" object, then by
    `machine_path`'s "models" object -- machine wins, per key, per role (a
    machine.json that only sets orchestrator.model leaves worker-low as the
    shared config left it).

    sources: role -> {"model": <where that value came from>, "effort": ...},
    one of "default" / "vault.config.json" / "machine.json", for `graph.py
    models` to explain the effective config."""
    effective = {role: dict(values) for role, values in DEFAULT_MODELS.items()}
    sources = {role: {"model": "default", "effort": "default"} for role in DEFAULT_MODELS}
    shared = _load_json_dict(shared_path).get("models")
    machine = _load_json_dict(machine_path).get("models")
    for source_name, raw in (("vault.config.json", shared), ("machine.json", machine)):
        if not isinstance(raw, dict):
            continue
        for role, values in raw.items():
            if role not in effective or not isinstance(values, dict):
                continue
            for key in ("model", "effort"):
                value = values.get(key)
                if isinstance(value, str) and value.strip():
                    effective[role][key] = value.strip()
                    sources[role][key] = source_name
    return effective, sources


def allowed_worker_models(orchestrator_model: str | None) -> list[str]:
    """Models a worker subagent may be started with (or overridden to): every
    known model strictly cheaper than the configured orchestrator model.
    Falls back to today's default (["haiku", "sonnet"], from the built-in
    default orchestrator "opus") if `orchestrator_model` is unknown."""
    models = cheaper_than(orchestrator_model)
    return models if models else cheaper_than(DEFAULT_MODELS["orchestrator"]["model"])
