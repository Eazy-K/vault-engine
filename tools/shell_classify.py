"""Shared Bash/PowerShell command classifier for the Claude Code hooks
(delegation-warn.py, read-guard.py). Loaded by path from the hooks (the hook folder
is not a package). Stdlib only; heuristic and conservative, see the comments below.
"""
from __future__ import annotations

import re

# --- Bash/PowerShell command classifier (heuristic; duplicated in the Claude and Codex
# hooks, tests assert both copies agree). Conservative: anything not clearly
# read-only/verification is treated as implementation work, so the counter errs towards
# counting, never towards silently ignoring a write. Operators (`| || && ;` newline) split
# segments only OUTSIDE quotes (so `grep -E "WARN|FAIL" f` is one segment); unbalanced
# quotes make the whole command count as implementation.
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELL_WRAP_RE = re.compile(
    r"^\s*(?:bash|sh|zsh|pwsh|powershell)(?:\.exe)?\s+(?:-[A-Za-z]+\s+)*"
    r"-(?:l?c|command)\s+(.+)$", re.I | re.S)
_READ_PROGRAMS = {"cat", "head", "tail", "sed", "grep", "rg", "find", "ls", "wc", "echo",
                  "printf", "diff", "tree", "type", "which", "pwd"}
# Read-only PowerShell cmdlets/aliases (matched case-insensitively). Set-Content, Out-File,
# New-Item, Remove-Item, Move-Item, Copy-Item, Add-Content, Rename-Item are deliberately
# absent, and `>` redirection is rejected in _segment_is_read_only.
_PS_READ = {"get-childitem", "gci", "dir", "get-content", "gc", "get-item", "get-location",
            "get-date", "select-string", "sls", "test-path", "resolve-path", "get-command",
            "measure-object", "select-object", "where-object", "sort-object", "format-table",
            "format-list", "out-string", "write-output", "write-host", "get-filehash",
            "compare-object", "convertfrom-json", "convertto-json"}
_GIT_READ = {"status", "diff", "log", "show", "branch", "worktree", "remote", "fetch",
             "rev-parse", "ls-files", "blame", "describe", "shortlog", "rev-list", "ls-tree",
             "cat-file", "name-rev", "merge-base", "tag", "config", "stash"}
_GIT_BRANCH_WRITE = ("-d", "-D", "-m", "-M", "-c", "-C", "-f", "-u", "-t", "--delete",
                     "--move", "--copy", "--force", "--set-upstream-to", "--unset-upstream",
                     "--track", "--edit-description")
_GIT_CONFIG_READ = ("--get", "--get-all", "--get-regexp", "--list", "-l")
_GIT_CONFIG_WRITE = ("--add", "--unset", "--unset-all", "--replace-all", "--edit", "-e",
                     "--rename-section", "--remove-section")
_GH_READ = {"pr": {"view", "checks", "status", "list", "diff"}, "run": {"view", "list"}}
_GH_API_WRITE_PREFIXES = ("-X", "--method", "-f", "-F", "--field", "--raw-field", "--input")
# `context` stays read-only; `reinforce`, `init`, `migrate` (and anything unlisted) count.
_GRAPH_READ = {"context", "stats", "show", "search", "tasks", "lint", "leakcheck", "doctor",
               "models", "--version", "--help"}
_PYTHONS = {"python", "python3", "py"}
_REDIRECT_OK_RE = re.compile(r"\d?>\s*&\d|\d?>\s*/dev/null|\d?>\s*NUL\b", re.I)


def _split_segments(cmd: str):
    """Split on `&&`, `||`, `|`, `;` and newlines outside quotes; None if quotes are unbalanced.

    Single quotes are literal; backslash escapes the next char outside single quotes.
    """
    segments, buf, quote, i, n = [], [], None, 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if quote == "'":
            buf.append(ch)
            if ch == "'":
                quote = None
        elif quote == '"':
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                i += 1
                buf.append(cmd[i])
            elif ch == '"':
                quote = None
        elif ch == "\\" and i + 1 < n:
            buf.append(ch)
            i += 1
            buf.append(cmd[i])
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch in "|&;\n":
            if ch in "|&" and cmd[i + 1:i + 2] == ch:
                i += 1  # `||` / `&&`
            elif ch == "&":
                buf.append(ch)  # lone `&` (e.g. `2>&1`) is not a separator
                i += 1
                continue
            segments.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if quote is not None:
        return None
    segments.append("".join(buf))
    return segments


def _tokens(segment: str) -> list:
    """Whitespace split outside quotes; surrounding quotes are dropped, backslashes kept."""
    out, buf, quote, started = [], [], None, False
    for ch in segment:
        if quote:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        elif ch in "'\"":
            quote, started = ch, True
        elif ch.isspace():
            if started or buf:
                out.append("".join(buf))
            buf, started = [], False
        else:
            buf.append(ch)
    if started or buf:
        out.append("".join(buf))
    return out


def _program(token: str) -> str:
    base = token.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base[:-4] if base.endswith(".exe") else base


def _git_is_read_only(args: list) -> bool:
    args = list(args)
    while args and args[0] in ("-C", "-c"):
        args = args[2:]  # `git -C <dir>` / `git -c k=v` take a value
    rest = [a for a in args if not a.startswith("-")]
    if not rest or rest[0] not in _GIT_READ:
        return False
    sub, flags = rest[0], [a for a in args if a.startswith("-")]
    if sub == "branch":
        if any(f in _GIT_BRANCH_WRITE for f in flags):
            return False
        # `git branch <name>` creates a branch; patterns are fine with --list.
        return len(rest) == 1 or any(f in ("--list", "-l") for f in flags)
    if sub == "tag":
        return any(f in ("-l", "--list") for f in flags)
    if sub == "config":
        return (any(f in _GIT_CONFIG_READ for f in flags)
                and not any(f in _GIT_CONFIG_WRITE for f in flags))
    if sub == "worktree":
        return rest[1:2] == ["list"]
    if sub == "remote":
        return rest[1:2] in ([], ["show"], ["get-url"])
    if sub == "stash":
        return rest[1:2] in (["list"], ["show"])
    return True


def _segment_is_read_only(segment: str) -> bool:
    segment = _REDIRECT_OK_RE.sub(" ", segment)
    if ">" in segment or "`" in segment or "$(" in segment:
        return False  # file redirection or command substitution: not provably read-only
    tokens = _tokens(segment)
    while tokens and _ENV_RE.match(tokens[0]):
        tokens.pop(0)
    if not tokens:
        return True  # empty segment (e.g. trailing separator)
    prog, args = _program(tokens[0]), tokens[1:]
    if prog == "cd":
        return True  # moving around changes no files
    if prog in _PS_READ:
        return True
    if prog in _READ_PROGRAMS:
        if prog == "sed" and any(a.startswith("-i") or a.startswith("--in-place") for a in args):
            return False
        if prog == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok", "-fprint")
                                  for a in args):
            return False
        return True
    if prog == "git":
        return _git_is_read_only(args)
    if prog == "gh":
        rest = [a for a in args if not a.startswith("-")]
        if rest[:1] == ["api"]:
            return not any(a.startswith(_GH_API_WRITE_PREFIXES) for a in args)
        return len(rest) >= 2 and rest[1] in _GH_READ.get(rest[0], ())
    if prog == "pytest":
        return True
    if prog in _PYTHONS:
        rest = list(args)
        while rest and rest[0].startswith("-") and rest[0] not in ("-m", "-c"):
            rest.pop(0)  # interpreter flags such as -u / -I / -B
        if not rest or rest[0] == "-c":
            return False
        if rest[0] == "-m":
            return len(rest) > 1 and rest[1] in ("unittest", "pytest")
        script = _program(rest[0])
        if script == "run_shard.py":
            return True
        if script == "graph.py":
            sub = [a for a in rest[1:] if not a.startswith("-")]
            if sub:
                return sub[0] in _GRAPH_READ
            return any(a in ("--version", "--help") for a in rest[1:])
        return False
    return False


def is_verification_command(cmd) -> bool:
    """True when every segment of a shell command is read-only or verification.

    Splits on && ; || | and newlines outside quotes, ignores `cd` and env assignments.
    An empty or unparsable (e.g. unbalanced quotes) command is NOT verification (it
    counts as implementation).
    """
    if isinstance(cmd, (list, tuple)):
        cmd = " ".join(str(c) for c in cmd)
    if not isinstance(cmd, str) or not cmd.strip():
        return False
    try:
        m = _SHELL_WRAP_RE.match(cmd)
        if m:  # bash -lc "..." / powershell -Command "..."
            inner = m.group(1).strip()
            if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
                q = inner[0]
                inner = inner[1:-1]
                if q == '"':
                    inner = inner.replace('\\"', '"')
            return is_verification_command(inner)
        parts = _split_segments(cmd)
        if parts is None:
            return False
        segments = [s for s in parts if s.strip()]
        return bool(segments) and all(_segment_is_read_only(s) for s in segments)
    except Exception:
        return False


# Programs whose main purpose is browsing files (what read-guard budgets). git, gh,
# python/pytest and friends are never browsing, even though they may be read-only.
_BROWSE_PROGRAMS = {"cat", "head", "tail", "sed", "grep", "rg", "find", "ls", "tree", "type"}
_PS_BROWSE = {"get-childitem", "gci", "dir", "get-content", "gc", "select-string", "sls"}


def is_browse_command(cmd) -> bool:
    """True when a shell command only reads files and at least one segment browses them
    (cat, head, tail, sed, grep, rg, find, ls, Get-Content, Select-String, ...).

    Any git/gh/python/pytest segment, any write, or an unparsable command is not
    browsing (so verification such as `git status`, `pytest`, `graph.py context` never
    counts)."""
    if isinstance(cmd, (list, tuple)):
        cmd = " ".join(str(c) for c in cmd)
    if not isinstance(cmd, str) or not cmd.strip():
        return False
    try:
        m = _SHELL_WRAP_RE.match(cmd)
        if m:
            inner = m.group(1).strip()
            if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
                q = inner[0]
                inner = inner[1:-1]
                if q == '"':
                    inner = inner.replace('\\"', '"')
            return is_browse_command(inner)
        if not is_verification_command(cmd):
            return False
        browse = False
        for seg in _split_segments(cmd) or []:
            tokens = _tokens(_REDIRECT_OK_RE.sub(" ", seg))
            while tokens and _ENV_RE.match(tokens[0]):
                tokens.pop(0)
            if not tokens:
                continue
            prog = _program(tokens[0])
            if prog in ("git", "gh", "pytest") or prog in _PYTHONS:
                return False
            if prog in _BROWSE_PROGRAMS or prog in _PS_BROWSE:
                browse = True
        return browse
    except Exception:
        return False
