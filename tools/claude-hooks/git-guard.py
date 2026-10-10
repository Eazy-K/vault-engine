#!/usr/bin/env python3
"""PreToolUse hook for the `Bash` tool: denies a force `git push` and any
`--no-verify` / `git commit -n` bypass of git hooks, wherever the flag sits in
the command (permission prefix rules only match flags placed right after
`git push`). Compound commands (`&&`, `||`, `;`, `|`, newlines, `bash -c "..."`)
are split and every `git` segment is checked.

Denied: `git push` with -f, --force, --force-with-lease[=...], --force-if-includes,
a short-flag cluster holding f (-fu), or a `+refspec`; `git push`/`git commit`
with --no-verify; `git commit` with -n (also inside a cluster such as -an).
Values of -m/-F/-C/-c/... are skipped, so a message text never trips it.

Same JSON contract as agent-guard.py: deny = hookSpecificOutput JSON on stdout,
exit 0. Fails open on any error; `VAULT_GIT_GUARD=off` disables it. The Codex
counterpart (tools/codex-hooks/git-guard.py) reuses `decide()` from this file.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

OPERATORS = set(";&|()")
WRAPPERS = {"sudo", "env", "command", "exec", "nohup", "time", "nice"}
GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
COMMIT_VALUE_OPTS = {"-m", "-F", "-C", "-c", "-t", "--message", "--file", "--author",
                     "--date", "--template", "--reuse-message", "--reedit-message",
                     "--cleanup", "--fixup", "--squash"}
VALUE_LETTERS = set("mFCct")  # in a short cluster these swallow the rest as their value
FORCE_LONG = ("--force", "--force-with-lease", "--force-if-includes")
HINT = " Ask the user to run it themselves if it is really needed."


def _split(command: str) -> list[list[str]]:
    """Segments (token lists) of a shell command, split on control operators."""
    lex = shlex.shlex(command.replace("\r", "\n").replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    segments: list[list[str]] = [[]]
    for tok in lex:
        if tok and set(tok) <= OPERATORS:
            segments.append([])
        else:
            segments[-1].append(tok)
    return [s for s in segments if s]


def _basename(tok: str) -> str:
    return tok.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _git_args(tokens: list[str]) -> tuple[str, list[str]] | None:
    """(subcommand, args) if the segment runs git, else None."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tok) or _basename(tok) in WRAPPERS \
                or (tok.startswith("-") and i and _basename(tokens[i - 1]) in WRAPPERS):
            i += 1
            continue
        break
    if i >= len(tokens) or _basename(tokens[i]) not in ("git", "git.exe"):
        return None
    i += 1
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 2 if tokens[i] in GIT_VALUE_OPTS else 1
    if i >= len(tokens):
        return None
    return tokens[i], tokens[i + 1:]


def _check_segment(tokens: list[str]) -> str | None:
    for tok in tokens[1:]:  # bash -c "git push -f", sh -lc '...'
        if " " in tok and "git" in tok:
            reason = decide(tok)
            if reason:
                return reason
    found = _git_args(tokens)
    if found is None:
        return None
    sub, args = found
    if sub not in ("push", "commit"):
        return None
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg == "--no-verify":
            return f"git-guard: `git {sub} --no-verify` skips the git hooks.{HINT}"
        if sub == "commit":
            if arg in COMMIT_VALUE_OPTS:
                skip = True
            elif re.fullmatch(r"-[A-Za-z]+", arg):
                for pos, letter in enumerate(arg[1:], 1):
                    if letter == "n":
                        return f"git-guard: `git commit -n` skips the git hooks.{HINT}"
                    if letter in VALUE_LETTERS:
                        skip = pos == len(arg) - 1  # value is the next token
                        break
            continue
        # push
        if arg in ("--repo", "-o", "--push-option", "--receive-pack", "--exec"):
            skip = True
        elif arg.split("=", 1)[0] in FORCE_LONG:
            return f"git-guard: force push is not allowed.{HINT}"
        elif re.fullmatch(r"-[A-Za-z]+", arg) and "f" in arg[1:]:
            return f"git-guard: force push is not allowed.{HINT}"
        elif (arg.startswith("+") or ":+" in arg) and len(arg) > 1:
            return f"git-guard: `+refspec` is a force push, not allowed.{HINT}"
    return None


def decide(command: str) -> str | None:
    """A deny reason for a shell command string, or None to let it through."""
    if not isinstance(command, str) or "git" not in command:
        return None
    try:
        segments = _split(command)
    except ValueError:  # unbalanced quotes: plain split per operator
        segments = [s.split() for s in re.split(r"[;&|\n]+", command)]
    for tokens in segments:
        reason = _check_segment(tokens)
        if reason:
            return reason
    return None


def command_of(payload: object, tool_names: tuple[str, ...]) -> str | None:
    """The shell command of a PreToolUse payload for one of tool_names, else None."""
    if not isinstance(payload, dict) or payload.get("tool_name") not in tool_names:
        return None
    args = payload.get("tool_input")
    if not isinstance(args, dict):
        return None
    cmd = args.get("command", args.get("cmd"))
    if isinstance(cmd, list):
        cmd = shlex.join(str(c) for c in cmd)
    return cmd if isinstance(cmd, str) else None


def deny_json(reason: str) -> str:
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }})


def main() -> None:
    try:
        if os.environ.get("VAULT_GIT_GUARD") == "off":
            return
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        command = command_of(payload, ("Bash",))
        reason = decide(command) if command else None
        if reason:
            print(deny_json(reason))
    except Exception:
        return


if __name__ == "__main__":
    main()
