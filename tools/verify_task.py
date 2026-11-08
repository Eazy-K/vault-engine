"""`verify`: the mechanical part of a task verification (used by the worker-verify agent).

Checks a unified diff and an acceptance-command output against a task spec: the
acceptance output is green, and no file from the spec's exclusion lists was touched.
Pure stdlib; reads files only (no git commands, no usage.log, no learning)."""
from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path

import graph as g

EXCLUDE_HEADING_RE = re.compile(r"^#{2,3}\s+(Files not to touch|Dokunulmayacak dosyalar)\s*$", re.I)
EXCLUDE_BULLET_RE = re.compile(r"^\s*[-*]\s*(?:Exclude|Hari[cç]):\s*(.*)$", re.I)
EXIT_ZERO_RE = re.compile(r"exit(?:\s*code)?[=: ]\s*0\b", re.I)
EXIT_FAIL_RE = re.compile(r"exit(?:\s*code)?[=: ]\s*[1-9]", re.I)


def _looks_like_path(token: str) -> bool:
    return bool(token) and any(c in token for c in "/\\.")


def _norm(token: str) -> str:
    token = token.strip().strip("`\"'").replace("\\", "/")
    while token.startswith("./"):
        token = token[2:]
    return token


def _line_tokens(line: str) -> list[str]:
    ticked = re.findall(r"`([^`]+)`", line)
    candidates = ticked or [t for t in re.split(r"[\s,;]+", line.strip().lstrip("-*").strip()) if t]
    return [_norm(t) for t in candidates if _looks_like_path(t.strip().strip("`\"'"))]


def excluded_paths(body: str) -> list[str]:
    """Path patterns the task forbids touching (exclusion heading section + Exclude:/Hariç: bullets)."""
    found: list[str] = []
    in_section = False
    for line in body.splitlines():
        if re.match(r"^#{1,6}\s", line):
            in_section = bool(EXCLUDE_HEADING_RE.match(line))
            continue
        bullet = EXCLUDE_BULLET_RE.match(line)
        if bullet:
            found.extend(_line_tokens(bullet.group(1)))
        elif in_section and line.strip():
            found.extend(_line_tokens(line))
    return list(dict.fromkeys(p for p in found if p))


def diff_paths(text: str) -> list[str]:
    """Changed paths of a unified diff: `diff --git` headers (b/ side), else `+++ b/` lines."""
    paths = [m.group(2) for m in re.finditer(r"^diff --git a/(.+?) b/(.+?)\s*$", text, re.M)]
    if not paths:
        paths = [m.group(1).strip() for m in re.finditer(r"^\+\+\+ b/(.+)$", text, re.M)]
    return list(dict.fromkeys(p for p in paths if p != "/dev/null"))


def acceptance_green(text: str) -> tuple[bool, str]:
    """Judge an acceptance-command output; returns (green, summary)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False, "output is empty"
    for ln in lines:
        if ln.startswith("FAILED") or EXIT_FAIL_RE.search(ln):
            return False, ln[:100]
    last = lines[-1]
    if re.match(r"^OK\b", last) or re.search(r"\bOK$", last):
        return True, last[:100]
    for ln in lines:
        if EXIT_ZERO_RE.search(ln):
            return True, ln[:100]
    return False, f"no OK / exit 0 marker (last line: {last[:80]})"


def _matches(pattern: str, path: str) -> bool:
    if any(c in pattern for c in "*?"):
        return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, "*/" + pattern)
    if pattern.endswith("/"):
        return path.startswith(pattern) or ("/" + pattern) in ("/" + path)
    return path == pattern or path.endswith("/" + pattern)


def evaluate(body: str, diff_text: str, acceptance_text: str | None) -> tuple[bool, list[str]]:
    """Returns (passed, item lines starting with `- ok:` / `- fail:`)."""
    items: list[str] = []
    ok = True
    command = g.task_acceptance_command(body)
    if command is None or command == "human":
        items.append("- ok: acceptance: human / none")
    elif acceptance_text is None:
        ok = False
        items.append("- fail: acceptance output missing (pass --acceptance-output)")
    else:
        green, summary = acceptance_green(acceptance_text)
        ok = ok and green
        items.append(f"- {'ok' if green else 'fail'}: acceptance {'green' if green else 'not green'} ({summary})")
    changed = diff_paths(diff_text)
    patterns = excluded_paths(body)
    if not changed:
        ok = False
        items.append("- fail: empty diff (no changed files found)")
        return ok, items
    hits = [p for p in changed if any(_matches(pat, p) for pat in patterns)]
    if hits:
        ok = False
        items.append(f"- fail: excluded file touched: {', '.join(hits)}")
    else:
        items.append(f"- ok: no excluded file touched ({len(patterns)} excluded patterns, "
                     f"{len(changed)} diff files)")
    items.append(f"- ok: diff touches {len(changed)} files: {', '.join(changed)}")
    return ok, items


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        sys.exit(f"cannot read {path}: {exc}")


def _task_body(ref: str) -> str:
    path = Path(ref)
    if path.is_file():
        _, body = g.parse_frontmatter(_read(ref))
        return body
    graph = g.Graph()
    nid, error = graph.resolve(ref)
    if error:
        sys.exit(f"[[{ref}]] {error}")
    return graph.notes[nid].body


def cmd_verify(args) -> None:
    body = _task_body(args.task)
    acceptance = _read(args.acceptance_output) if args.acceptance_output else None
    passed, items = evaluate(body, _read(args.diff), acceptance)
    print("PASS" if passed else "FAIL")
    print("\n".join(items))
    sys.exit(0 if passed else 1)


def register(sub) -> None:
    p = sub.add_parser("verify", help="check a diff and acceptance output against a task spec "
                                      "(mechanical part of worker-verify)")
    p.add_argument("--task", required=True, help="task note id or path to a task file")
    p.add_argument("--diff", required=True, help="file holding the unified diff")
    p.add_argument("--acceptance-output", help="file holding the acceptance command output")
    p.set_defaults(func=cmd_verify)
