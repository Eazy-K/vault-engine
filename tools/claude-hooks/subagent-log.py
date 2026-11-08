#!/usr/bin/env python3
"""SubagentStop hook for Claude Code: logs one `subagent_stop` event per finished
subagent run to `<data>/.graph/usage.log` (same file and ts/agent/session fields as
graph.py's log_usage; only written if VAULT_DATA/VAULT_HOME points at an existing
directory).

Fields come from the documented SubagentStop payload
(https://code.claude.com/docs/en/hooks): `agent_type`, `agent_id`,
`stop_hook_active`, `last_assistant_message`, `agent_transcript_path`. Logged:
agent_type, agent_id, stop_hook_active and the *length* of the final message
(not its text): `last_message_chars`, `last_message_source` (payload | transcript | none)
and `last_message_kind` (text | handback | none). Worker subagents return their report
through a `SubagentHandback` tool call (no text block, empty payload message), so the
transcript scan counts that call's `input.message` length (kind "handback") and walks
back past assistant records with neither text nor handback. The payload documents no status/outcome/duration/token field, so
none is logged. A stop with no agent_type and no meta file is a Claude Code internal
agent; it is logged with `"internal": true`. When the payload lacks agent_type, `agentType` is read from the
subagent meta file (`agent-<id>.meta.json` next to the transcript); no transcript
content is ever read.

Budget classes (tools/budgets.py; numbers from `budgets` in vault.config.json, with
fallbacks): for an agent_type that has a class (`budgets.roles`), the transcript is
streamed once (budgets.transcript_usage) for `turns` (distinct `message.id` of assistant
records; one API reply is several records) and `tokens` (input + cache_creation +
cache_read of the last assistant usage). The event then also carries `budget_class`,
`turns`, `tokens` (when the transcript has usage) and `budget_hit` (none | warn >=80% |
block >=100% of max_turns or max_tokens). On warn and on block it only prints a `systemMessage`; block is
logged as `budget_hit` but not enforced here (a SubagentStop decision comes after the
worker already handed back), the mid-run PreToolUse budget-guard hook is the gate.
`stop_hook_active` is only logged. Unknown agent types get no budget fields.

Exits 0 on any problem and fails open (prints nothing); it never blocks.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

_TAIL_BYTES = 2 * 1024 * 1024


def _text_len(content) -> tuple[int, int]:
    """(text chars, SubagentHandback report chars) of a message `content`."""
    if isinstance(content, str):
        return len(content), 0
    text = handback = 0
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if isinstance(block.get("text"), str):
                text += len(block["text"])
            elif block.get("type") == "tool_use" and block.get("name") == "SubagentHandback":
                inp = block.get("input")
                if isinstance(inp, dict):
                    handback += len(str(inp.get("message", "")))
    return text, handback


def _assistant_chars(rec) -> tuple[int, str] | None:
    """(chars, kind) of an assistant record, or None if `rec` is not one or has no
    text and no handback. kind is "handback" when the chars come from a
    SubagentHandback tool call (how worker subagents return their report), else "text".

    Shape: Claude `{"type":"assistant","message":{"content":...}}`."""
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    text, handback = _text_len(msg.get("content"))
    if handback:
        return text + handback, "handback"
    if text:
        return text, "text"
    return None


def transcript_last(payload: dict) -> tuple[int, str] | None:
    """(chars, kind) of the last assistant entry in the transcript JSONL that has text
    or a handback (reads at most the last ~2 MB); None if no readable file or none."""
    try:
        path = payload.get("agent_transcript_path") or payload.get("transcript_path")
        if not path or not os.path.isfile(str(path)):
            return None
        with open(str(path), "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - _TAIL_BYTES))
            data = f.read()
        lines = data.split(b"\n")
        if size > _TAIL_BYTES:
            lines = lines[1:]  # first line is probably cut mid-record
        for raw in reversed(lines):
            try:
                found = _assistant_chars(json.loads(raw.decode("utf-8", "replace")))
            except Exception:
                continue
            if found is not None:
                return found
    except Exception:
        pass
    return None


def last_message(payload: dict) -> tuple[int, str, str]:
    """(chars, source, kind): source payload|transcript|none, kind text|handback|none."""
    last = payload.get("last_assistant_message")
    if isinstance(last, str) and last:
        return len(last), "payload", "text"
    found = transcript_last(payload)
    if found is not None:
        return found[0], "transcript", found[1]
    return 0, "none", "none"


def meta_agent_type(payload: dict) -> tuple[str, bool]:
    """(agentType, internal) from the subagent's `.meta.json` (reads only that one key).

    `internal` is True only when a meta path could be derived and no meta file exists
    there: Claude Code internal agents have no transcript/meta file. A meta file that
    exists but lacks agentType, or no derivable path, is not internal."""
    found, checked = False, False
    try:
        agent_id = str(payload.get("agent_id") or "")
        paths = []
        atp = payload.get("agent_transcript_path")
        if atp:
            paths.append(os.path.splitext(str(atp))[0] + ".meta.json")
        tp = payload.get("transcript_path")
        if tp and agent_id:
            base = os.path.splitext(str(tp))[0]
            paths.append(os.path.join(base, "subagents", "agent-" + agent_id + ".meta.json"))
        for path in paths:
            checked = True
            if os.path.isfile(path):
                found = True
                with open(path, encoding="utf-8") as f:
                    value = json.load(f).get("agentType")
                if isinstance(value, str) and value:
                    return value, False
    except Exception:
        return "", False
    return "", checked and not found


class _FallbackBudgets:
    """Used if tools/budgets.py can't be imported; same numbers as the module."""
    CLASSES = {"S": (20, 80000), "M": (60, 250000), "L": (150, 600000), "verify": (25, 100000)}
    ROLES = {"worker-low": "S", "worker-medium": "M", "worker-verify": "verify"}

    @classmethod
    def load_budgets(cls, data_dir):
        return None

    @staticmethod
    def transcript_usage(path):
        """Same rule as budgets.transcript_usage: turns = distinct message.id of
        assistant records (id-less record = one turn); tokens of the last usage."""
        ids, anonymous, tokens = set(), 0, None
        try:
            if not path or not os.path.isfile(str(path)):
                return 0, None
            with open(str(path), "r", encoding="utf-8", errors="replace") as f:
                for line in f:
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

    @classmethod
    def limits_for_role(cls, _budgets, role):
        name = cls.ROLES.get(role)
        return (name, *cls.CLASSES[name]) if name else None


def _budgets_module():
    """tools/budgets.py (this script's parent folder), or the inline fallback."""
    try:
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent / "budgets.py"
        spec = importlib.util.spec_from_file_location("budgets", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return _FallbackBudgets


def _data_dir():
    raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
    if not raw:
        return None
    path = os.path.expanduser(raw)
    return path if os.path.isdir(path) else None


def budget_check(payload: dict, agent_type: str, data_dir) -> dict | None:
    """{"budget_class", "turns", "tokens"?, "budget_hit", "max_turns", "max_tokens"} for a
    role with a budget class, else None. budget_hit: block (>=100%), warn (>=80%), none."""
    try:
        mod = _budgets_module()
        limits = mod.limits_for_role(mod.load_budgets(data_dir), agent_type)
        if not limits:
            return None
        name, max_turns, max_tokens = limits
        path = payload.get("agent_transcript_path") or payload.get("transcript_path")
        turns, tokens = mod.transcript_usage(path)
        ratio = max(turns / max_turns, (tokens or 0) / max_tokens)
        hit = "block" if ratio >= 1.0 else "warn" if ratio >= 0.8 else "none"
        info = {"budget_class": name, "turns": turns, "budget_hit": hit,
                "max_turns": max_turns, "max_tokens": max_tokens}
        if tokens is not None:
            info["tokens"] = tokens
        return info
    except Exception:
        return None


def log_event(payload: dict) -> dict | None:
    """Write the usage.log event; return the budget info (see budget_check) or None.
    The budget is evaluated even without a data dir (fallback numbers)."""
    if not payload:
        return None
    info = None
    try:
        data_dir = _data_dir()
        event = {"ts": datetime.now().isoformat(timespec="seconds"), "agent": "claude",
                 "session": str(payload.get("session_id") or ""), "event": "subagent_stop"}
        for key in ("agent_type", "agent_id"):
            if payload.get(key):
                event[key] = str(payload[key])
        if "agent_type" not in event:
            fallback, internal = meta_agent_type(payload)
            if fallback:
                event["agent_type"] = fallback
            elif internal:
                event["internal"] = True
        if "stop_hook_active" in payload:
            event["stop_hook_active"] = bool(payload["stop_hook_active"])
        if event.get("agent_type"):
            info = budget_check(payload, event["agent_type"], data_dir)
        if data_dir is None:
            return info
        graph_dir = os.path.join(data_dir, ".graph")
        os.makedirs(graph_dir, exist_ok=True)
        (event["last_message_chars"], event["last_message_source"],
         event["last_message_kind"]) = last_message(payload)
        if info:
            for key in ("budget_class", "turns", "tokens", "budget_hit"):
                if key in info:
                    event[key] = info[key]
        with open(os.path.join(graph_dir, "usage.log"), "a", encoding="utf-8",
                  newline="\n") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return info


def _budget_output(payload: dict, agent_type: str, info: dict) -> dict | None:
    """The hook's stdout JSON for a warn/block result, or None."""
    hit = info.get("budget_hit")
    if hit not in ("warn", "block"):
        return None
    usage = (f"{info['turns']}/{info['max_turns']} turns and "
             f"{info.get('tokens', 0)}/{info['max_tokens']} tokens")
    if hit == "warn":
        return {"systemMessage": f"subagent-log: {agent_type} used {usage} "
                                 f"(class {info['budget_class']}, >=80%)"}
    return {"systemMessage": f"subagent-log: budget class {info['budget_class']} exceeded "
                             f"({info['turns']}/{info['max_turns']} turns, "
                             f"{info.get('tokens', 0)}/{info['max_tokens']} tokens); "
                             "the mid-run budget-guard hook is the gate, this is only logged"}


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return
    except Exception:
        return
    info = log_event(payload)
    try:
        if info:
            agent_type = str(payload.get("agent_type") or "") or meta_agent_type(payload)[0]
            out = _budget_output(payload, agent_type, info)
            if out:
                print(json.dumps(out, ensure_ascii=False))
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
