#!/usr/bin/env python3
"""PermissionRequest hook for Claude Code: decides permission requests by policy.

Why: the event fires only when Claude Code is about to ask the user, or would
auto-deny a call that cannot prompt (a background subagent: "if no hook returns a
decision, it denies the tool call"). A background subagent cannot be prompted, so
the only useful answers are an allow (policy) or an explicit, logged deny with a
reason the agent can report as a blocker.

Decision:
- inside a git worktree AND an allow rule matches -> allow;
- otherwise, the request comes from a subagent (`agent_type` set) -> deny with a
  message naming the tool and the call;
- otherwise (the main session, which a human can answer) -> print nothing, so the
  normal prompt appears: the human is the gate.

Worktree detection: the nearest `.git` at or above `cwd` is a FILE (linked
worktree), or, if no `.git` is found, `git rev-parse --git-dir --git-common-dir`
(5 s timeout) reports two different paths. Any git failure means "not a worktree".

Allow rules = union of `permissions.allow` from `$CLAUDE_CONFIG_DIR` (else
~/.claude)/settings.json, `.claude/settings.json` and `.claude/settings.local.json`
under the cwd, the worktree root and its main checkout, plus the optional list
`permissions.worktree_allow` in `<VAULT_DATA>/vault.config.json` (same syntax).

Rule forms (`rule_matches`; anything else never matches):
  `Tool`                    any call of that tool (`Read` also covers Grep/Glob,
                            `Edit` also covers Write/MultiEdit/NotebookEdit)
  `Bash(exact)`             the whole command equals exact
  `Bash(prefix:*)`          command starts with prefix on a word boundary; every
                            `&&` / `||` / `;` / `|` / newline segment must match a
                            rule; a command with `$(...)` or backticks never matches
  `Bash(git * --short)`     `*` wildcard anywhere in the specifier
  `Read(glob)` `Edit(glob)` `Write(glob)` `Glob(glob)` `Grep(glob)`
                            gitignore-style glob on file_path / path / pattern
                            (`*` stays in a directory, `**` crosses); also relative
                            to the cwd; `//abs`, `~/x`, `/x` (worktree root) anchors
  `WebFetch(domain:host)`   host of tool_input.url equals host or is a subdomain
  `mcp__server__tool`, `mcp__server`   tool name starts with the rule

Logging: `<VAULT_DATA>/.graph/usage.log` (VAULT_DATA, else VAULT_HOME, must be an
existing dir), same ts/agent/session fields as subagent-log.py; event
`permission_allow` (with `rule`), `permission_deny` or `permission_prompt` (the
main-session no-decision case, so a live check can see the hook fired), with
`tool`, `summary` (first 120 chars, secrets masked), `agent_type` (if any) and
`worktree`. Check: `grep permission_ .graph/usage.log`.

Hook protocol, from https://code.claude.com/docs/en/hooks (PermissionRequest):
  output (stdout JSON, exit 0):
    {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
      "decision": {"behavior": "allow"}}}
    {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
      "decision": {"behavior": "deny", "message": "..."}}}
  No output = no decision. Exit code 2 is not honored for this event.

Never crashes: any error prints nothing and exits 0 (fails open to the normal
flow). `VAULT_PERMISSION_HOOK=off` disables it. Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime

EDIT_TOOLS = {"Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Grep", "Glob"}
SUMMARY_CHARS = 120
_RULE = re.compile(r"^\s*([^()\s]+)\s*(?:\((.*)\))?\s*$", re.S)
_SECRET = re.compile(
    r"(?i)((?:token|secret|passw(?:or)?d|passwd|authorization|bearer)[\"']?\s*[=:]?\s*)\S+"
    r"|([\w-]*key[\"']?\s*[=:]\s*)\S+"
    r"|\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,})")


def mask(text: str) -> str:
    """Replace the value of token=/key=/password=-like pairs and known token shapes."""  # guard:ignore
    return _SECRET.sub(lambda m: ((m.group(1) or m.group(2)) + "***") if (m.group(1) or m.group(2))
                       else "***", text)


def command_of(tool_input: dict) -> str | None:
    cmd = tool_input.get("command", tool_input.get("cmd"))
    if isinstance(cmd, list):
        cmd = shlex.join(str(c) for c in cmd)
    return cmd if isinstance(cmd, str) else None


def summarize(tool_name: str, tool_input: dict) -> str:
    """First 120 chars of the command / file path / url / pattern, secrets masked."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    text = command_of(tool_input)
    if text is None:
        text = str(tool_input.get("file_path") or tool_input.get("notebook_path")
                   or tool_input.get("path") or tool_input.get("url")
                   or tool_input.get("pattern") or "")
    return mask(" ".join(text.split()))[:SUMMARY_CHARS]


# --- worktree detection ---------------------------------------------------------

def find_git_root(cwd: str) -> tuple[str, str] | None:
    """(root dir, "file"|"dir") of the nearest `.git` at or above cwd."""
    cur = os.path.abspath(cwd)
    while True:
        git = os.path.join(cur, ".git")
        if os.path.isfile(git):
            return cur, "file"
        if os.path.isdir(git):
            return cur, "dir"
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _git_fallback(cwd: str) -> bool:
    try:
        out = subprocess.run(["git", "rev-parse", "--git-dir", "--git-common-dir"], cwd=cwd,
                             capture_output=True, text=True, timeout=5)
        lines = out.stdout.splitlines()
        if out.returncode != 0 or len(lines) < 2:
            return False
        a, b = (os.path.normcase(os.path.realpath(os.path.join(cwd, p.strip())))
                for p in lines[:2])
        return a != b
    except Exception:
        return False


def worktree_info(cwd: str) -> tuple[bool, str | None, str | None]:
    """(is_worktree, worktree root, main checkout root or None)."""
    found = find_git_root(cwd)
    if found is None:
        return _git_fallback(cwd), None, None
    root, kind = found
    if kind == "dir":
        return False, root, None
    main = None
    try:
        with open(os.path.join(root, ".git"), encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
        if first.startswith("gitdir:"):
            gitdir = first[len("gitdir:"):].strip().replace("\\", "/")
            if "/worktrees/" in gitdir:
                common = gitdir.rsplit("/worktrees/", 1)[0]
                if os.path.basename(common) == ".git":
                    main = os.path.dirname(common)
    except Exception:
        pass
    return True, root, main


# --- allow rules ----------------------------------------------------------------

def _data_dir() -> str | None:
    raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
    return os.path.expanduser(raw) if raw else None


def _str_list(value) -> list[str]:
    return [r for r in value if isinstance(r, str)] if isinstance(value, list) else []


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _perms(data: dict) -> dict:
    perms = data.get("permissions")
    return perms if isinstance(perms, dict) else {}


def collect_allow_rules(dirs: list[str]) -> list[str]:
    """Union of permissions.allow from user + project settings and
    permissions.worktree_allow from vault.config.json."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    paths = [os.path.join(os.path.expanduser(cfg), "settings.json")]
    for d in dict.fromkeys(x for x in dirs if x):
        paths.append(os.path.join(d, ".claude", "settings.json"))
        paths.append(os.path.join(d, ".claude", "settings.local.json"))
    rules: list[str] = []
    for p in paths:
        rules.extend(_str_list(_perms(_load_json(p)).get("allow")))
    data = _data_dir()
    if data:
        rules.extend(_str_list(_perms(_load_json(os.path.join(data, "vault.config.json")))
                               .get("worktree_allow")))
    return list(dict.fromkeys(rules))


# --- rule matching (pure) -------------------------------------------------------

def _wild(spec: str) -> "re.Pattern[str]":
    return re.compile("".join(".*" if c == "*" else re.escape(c) for c in spec), re.S)


def _bash_part_ok(part: str, spec: str) -> bool:
    part = part.strip()
    if spec.endswith(":*"):
        prefix = spec[:-2]
        if "*" in prefix:
            return bool(_wild(prefix + "*").fullmatch(part))
        return part == prefix or part.startswith(prefix + " ")
    if "*" in spec:
        return bool(_wild(spec).fullmatch(part))
    return part == spec


def bash_segments(command: str) -> list[str] | None:
    """Segments of a compound command; None when it holds `$(...)` or backticks."""
    if "$(" in command or "`" in command:
        return None
    return [p.strip() for p in re.split(r"&&|\|\||;|\||\r?\n", command) if p.strip()]


def _path_regex(pattern: str) -> "re.Pattern[str]":
    out, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
            continue
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
            continue
        out += "[^/]*" if c == "*" else ("[^/]" if c == "?" else re.escape(c))
        i += 1
    return re.compile(out + "(?:/.*)?", re.S)  # a directory pattern covers its contents


def _norm(p: str) -> str:
    return os.path.normpath(p).replace("\\", "/")


def _path_ok(path: str, spec: str, cwd: str, root: str | None) -> bool:
    target = _norm(path if os.path.isabs(path) else os.path.join(cwd, path))
    spec = spec.strip().replace("\\", "/")
    if spec.startswith("//"):
        base = spec[1:]
    elif spec.startswith("~/"):
        base = _norm(os.path.join(os.path.expanduser("~"), spec[2:]))
    elif spec.startswith("/"):
        base = _norm(os.path.join(root or cwd, spec[1:]))
    elif spec.startswith("./"):
        base = _norm(os.path.join(cwd, spec[2:]))
    else:
        base = spec if os.path.isabs(spec) else _norm(os.path.join(cwd, spec))
    return bool(_path_regex(base.replace("\\", "/")).fullmatch(target))


def _host_ok(url: str, spec: str) -> bool:
    m = re.match(r"^[a-z]+://([^/:?#@]+)", url or "", re.I)
    host = m.group(1).lower() if m else ""
    if not spec.startswith("domain:"):
        return False
    d = spec[7:].strip().lower()
    return bool(host and d and (host == d or host.endswith("." + d)))


def _split_rule(rule: str) -> tuple[str, str | None] | None:
    m = _RULE.match(rule) if isinstance(rule, str) else None
    return (m.group(1), m.group(2)) if m else None


def _tool_family(tool: str) -> set[str]:
    family = {tool}
    if tool in EDIT_TOOLS:
        family.add("Edit")
    if tool in READ_TOOLS:
        family.add("Read")
    return family


def _target_path(tool: str, tool_input: dict, cwd: str) -> str | None:
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
    if not path and tool == "Glob":
        path = tool_input.get("pattern")
    if not path and tool in READ_TOOLS:
        path = cwd
    return str(path) if path else None


def _spec_matches(spec: str, tool: str, tool_input: dict, cwd: str, root: str | None) -> bool:
    """One rule specifier (the part inside the parentheses) against a call, except Bash
    compounds, which any_rule_matches handles."""
    if tool == "Bash":
        cmd = command_of(tool_input)
        return cmd is not None and _bash_part_ok(cmd, spec)
    if tool == "WebFetch":
        return _host_ok(str(tool_input.get("url", "")), spec)
    if _tool_family(tool) & {"Read", "Edit"}:
        path = _target_path(tool, tool_input, cwd)
        return path is not None and _path_ok(path, spec, cwd, root)
    return False


def rule_matches(rule: str, tool_name: str, tool_input: dict, cwd: str,
                 root: str | None = None) -> bool:
    """Does one allow rule cover this tool call? (Claude Code rule syntax, minimal.)"""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    parsed = _split_rule(rule)
    if parsed is None or not isinstance(tool_name, str) or not tool_name:
        return False
    name, spec = parsed
    if name.startswith("mcp__"):
        return spec is None and tool_name.startswith(name)
    if name not in _tool_family(tool_name):
        return False
    if spec is None or spec.strip() in ("", "*"):
        return True
    if tool_name == "Bash":
        cmd = command_of(tool_input)
        if cmd is None:
            return False
        if cmd.strip() == spec.strip():
            return True
        segs = bash_segments(cmd)
        return bool(segs) and all(_bash_part_ok(s, spec) for s in segs)
    return _spec_matches(spec, tool_name, tool_input, cwd, root)


def any_rule_matches(rules: list[str], tool_name: str, tool_input: dict, cwd: str,
                     root: str | None = None) -> str | None:
    """The matching rule (for a compound Bash command, the distinct rules joined by
    ", ", one per segment) or None. Each Bash segment must match some rule."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Bash":
        cmd = command_of(tool_input)
        if cmd is None:
            return None
        for rule in rules:  # bare tool or whole-command rule
            parsed = _split_rule(rule)
            if parsed and parsed[0] == "Bash" and (
                    parsed[1] is None or parsed[1].strip() in ("", "*")
                    or parsed[1].strip() == cmd.strip()):
                return rule
        segs = bash_segments(cmd)
        if not segs:
            return None
        used: list[str] = []
        for seg in segs:
            hit = next((r for r in rules if rule_matches(r, "Bash", {"command": seg}, cwd, root)),
                       None)
            if hit is None:
                return None
            if hit not in used:
                used.append(hit)
        return ", ".join(used)
    return next((r for r in rules if rule_matches(r, tool_name, tool_input, cwd, root)), None)


# --- logging and output ---------------------------------------------------------

def log_event(payload: dict, event_name: str, tool: str, summary: str, agent_type: str | None,
              worktree: bool, rule: str | None = None) -> None:
    try:
        data_dir = _data_dir()
        if not data_dir or not os.path.isdir(data_dir):
            return
        graph_dir = os.path.join(data_dir, ".graph")
        os.makedirs(graph_dir, exist_ok=True)
        event = {"ts": datetime.now().isoformat(timespec="seconds"), "agent": "claude",
                 "session": str(payload.get("session_id") or ""), "event": event_name,
                 "tool": tool, "summary": summary}
        if agent_type:
            event["agent_type"] = agent_type
        event["worktree"] = worktree
        if rule:
            event["rule"] = rule
        with open(os.path.join(graph_dir, "usage.log"), "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def decision_json(behavior: str, message: str | None = None) -> str:
    decision: dict = {"behavior": behavior}
    if message:
        decision["message"] = message
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                              "decision": decision}})


def decide(payload: dict) -> str | None:
    """The hook's stdout JSON, or None for no decision. Logs the outcome."""
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return None
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and payload.get("cwd") \
        else os.getcwd()
    agent_type = payload.get("agent_type")
    agent_type = agent_type if isinstance(agent_type, str) and agent_type else None
    is_wt, root, main = worktree_info(cwd)
    summary = summarize(tool, tool_input)
    if is_wt:
        rules = collect_allow_rules([cwd, root or "", main or ""])
        rule = any_rule_matches(rules, tool, tool_input, os.path.abspath(cwd), root)
        if rule:
            log_event(payload, "permission_allow", tool, summary, agent_type, True, rule)
            return decision_json("allow")
    if agent_type:
        log_event(payload, "permission_deny", tool, summary, agent_type, is_wt)
        return decision_json("deny", (
            f"permission-request: '{tool}' {summary} not in the allow policy for subagent "
            f"{agent_type} (worktree={is_wt}); report it as a blocker instead of retrying"))
    log_event(payload, "permission_prompt", tool, summary, None, is_wt)
    return None


def main() -> None:
    try:
        if os.environ.get("VAULT_PERMISSION_HOOK") == "off":
            return
        raw = sys.stdin.buffer.read()
        text = raw.decode("utf-8", "replace")
        payload = json.loads(text) if text.strip() else {}
        if not isinstance(payload, dict):
            return
        out = decide(payload)
        if out:
            sys.stdout.write(out + "\n")
    except Exception:
        return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
