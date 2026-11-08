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
EXIT_ZERO_RE = re.compile(r"^exit(?:\s*code)?\s*[=:]?\s*0\s*$", re.I)
EXIT_FAIL_RE = re.compile(r"^exit(?:\s*code)?\s*[=:]?\s*[1-9]\d*\s*$", re.I)
PLAIN_FILE_RE = re.compile(r"^[\w.-]+\.\w{1,5}$")
STOPLIST = {"etc.", "e.g.", "i.e.", "vs."}


def _looks_like_path(token: str) -> bool:
    return bool(token) and any(c in token for c in "/\\.")


def _plain_is_path(token: str) -> bool:
    if token.lower() in STOPLIST:
        return False
    token = token.lstrip("(").rstrip(".,;:)")
    return bool(token) and ("/" in token or "\\" in token or bool(PLAIN_FILE_RE.match(token)))


def _norm(token: str) -> str:
    token = token.strip().strip("`\"'").replace("\\", "/")
    while token.startswith("./"):
        token = token[2:]
    return token


def _line_tokens(line: str) -> list[str]:
    ticked = re.findall(r"`([^`]+)`", line)
    if ticked:
        return [_norm(t) for t in ticked if _looks_like_path(t.strip().strip("`\"'"))]
    plain = [t for t in re.split(r"[\s,;]+", line.strip().lstrip("-*").strip()) if t]
    return [_norm(t.strip("\"'").lstrip("(").rstrip(".,;:)")) for t in plain if _plain_is_path(t.strip("\"'"))]


def excluded_paths(body: str) -> list[str]:
    """Path patterns the task forbids touching (exclusion heading section + Exclude:/Hariç: bullets)."""
    found: list[str] = []
    in_section = False
    in_fence = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and re.match(r"^#{1,6}\s", line):
            in_section = bool(EXCLUDE_HEADING_RE.match(line))
            continue
        bullet = EXCLUDE_BULLET_RE.match(line)
        if bullet:
            found.extend(_line_tokens(bullet.group(1)))
        elif in_section and line.strip():
            found.extend(_line_tokens(line))
    return list(dict.fromkeys(p for p in found if p))


def diff_paths(text: str) -> list[str]:
    """Changed paths of a unified diff: both sides of `diff --git` headers, else `+++ b/` lines."""
    side_a = r'(?:"a/((?:[^"\\]|\\.)*)"|a/(.+?))'
    side_b = r'(?:"b/((?:[^"\\]|\\.)*)"|b/(.+?))'
    header = re.compile(rf"^diff --git {side_a} {side_b}\s*$", re.M)
    paths: list[str] = []
    for m in header.finditer(text):
        for quoted, plain in ((m.group(1), m.group(2)), (m.group(3), m.group(4))):
            if quoted is not None:
                paths.append(re.sub(r"\\(.)", r"\1", quoted))
            elif plain:
                paths.append(plain)
    if not paths:
        paths = [m.group(1).strip() for m in re.finditer(r"^\+\+\+ b/(.+)$", text, re.M)]
    return list(dict.fromkeys(p for p in paths if p and p != "/dev/null"))


def acceptance_green(text: str) -> tuple[bool, str]:
    """Judge an acceptance-command output; returns (green, reason)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False, "output is empty"
    for ln in lines:
        if ln.startswith("FAILED") or EXIT_FAIL_RE.match(ln):
            return False, f"failure marker: {ln[:80]}"
    if any(re.match(r"^Ran 0 tests?\b", ln) for ln in lines):
        return False, "no tests ran"
    last = lines[-1]
    if last == "OK" or last.startswith("OK ("):
        return True, last[:100]
    for ln in lines:
        if EXIT_ZERO_RE.match(ln):
            return True, ln[:100]
    return False, "no OK / exit 0 marker"


def _matches(pattern: str, path: str) -> bool:
    if path == pattern or path.startswith(pattern.rstrip("/") + "/"):
        return True
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, "*/" + pattern)
    return path.endswith("/" + pattern)


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
        green, reason = acceptance_green(acceptance_text)
        ok = ok and green
        if green:
            items.append(f"- ok: acceptance green ({reason})")
        else:
            nonblank = [ln.strip() for ln in acceptance_text.splitlines() if ln.strip()]
            last = nonblank[-1][:80] if nonblank else ""
            items.append(f"- fail: acceptance not green ({reason}; last line: {last})")
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
