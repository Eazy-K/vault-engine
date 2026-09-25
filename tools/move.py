"""Move or rename a note without breaking the graph.

A note's id is its path, and other notes refer to it by name or path in
`[[links]]`, in `weights` keys and in learned edges. Moving the file alone
leaves all of those pointing at nothing; `mv` updates them together.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import graph as g


def _note_id(target: str) -> str:
    return target.replace("\\", "/").strip().removesuffix(".md").strip("/")


def _rewrite_links(text: str, old: str, new: str) -> str:
    """Replace link target `old` with `new` in [[links]] and in weights keys."""
    text = re.sub(r"\[\[" + re.escape(old) + r"(?=[\]|#])", "[[" + new, text)
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    front = re.sub(r"(?m)^(\s+)" + re.escape(old) + r"(\s*:)", r"\g<1>" + new + r"\2",
                   text[:end])
    return front + text[end:]


def _move_file(src: Path, dest: Path, data: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    moved = False
    if (data / ".git").exists():
        result = subprocess.run(["git", "mv", str(src), str(dest)], cwd=data,
                                capture_output=True, text=True)
        moved = result.returncode == 0  # untracked files fall back to a plain rename
    if not moved:
        src.rename(dest)
    parent = src.parent
    while parent != data and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def _rewrite_learned(paths: g.Paths, old: str, new: str) -> int:
    changed = 0
    learned_dir = paths.learned_dir
    for path in sorted(learned_dir.glob("*.json")) if learned_dir.exists() else []:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        out: dict[tuple[str, str], float] = {}
        for key, value in raw.items():
            a, _, b = key.partition("|")
            if old in (a, b):
                changed += 1
            a, b = (new if a == old else a), (new if b == old else b)
            k = g.pair(a, b)
            out[k] = out.get(k, 0.0) + float(value)
        data = {f"{a}|{b}": round(v, 4) for (a, b), v in sorted(out.items())}
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8", newline="\n")
    return changed


def move_note(paths: g.Paths, source: str, dest: str) -> dict:
    graph = g.Graph(paths)
    old, error = graph.resolve(source)
    if error:
        raise ValueError(f"[[{source}]] {error}")
    if graph.notes[old].source != "data":
        raise ValueError(f"{old} ships with the engine; copy it into the data folder instead")
    new = _note_id(dest)
    if new.split("/")[-1] == new and "/" in old:
        new = old.rsplit("/", 1)[0] + "/" + new  # a bare name renames in place
    dest_path = paths.data / (new + ".md")
    if dest_path.exists() or new in graph.notes:
        raise ValueError(f"{new} already exists")

    old_name, new_name = old.split("/")[-1], new.split("/")[-1]
    # Every way a note may have written the link: full id, bare name, or a path
    # suffix. Only targets that really resolve to the moved note are rewritten.
    spellings: dict[str, str] = {}
    for note in graph.notes.values():
        for target in note.declared_links():
            if graph.resolve(target)[0] == old:
                spellings[target] = new_name if target.casefold() == old_name.casefold() else new

    _move_file(graph.notes[old].path, dest_path, paths.data)

    files = 0
    for note in graph.notes.values():
        if note.source != "data":
            continue
        path = dest_path if note.id == old else note.path
        text = path.read_text(encoding="utf-8")
        updated = text
        for target, replacement in spellings.items():
            updated = _rewrite_links(updated, target, replacement)
        if updated != text:
            path.write_text(updated, encoding="utf-8", newline="\n")
            files += 1
    return {"from": old, "to": new, "notes": files, "edges": _rewrite_learned(paths, old, new)}


def cmd_mv(args) -> None:
    paths = g.default_paths()
    g._require_writable(paths)
    try:
        result = move_note(paths, args.source, args.dest)
    except ValueError as exc:
        sys.exit(f"mv: {exc}")
    print(f"moved {result['from']} -> {result['to']}; "
          f"links updated in {result['notes']} notes, {result['edges']} learned edges")
    problems = g.Graph(paths).problems
    for problem in problems:
        print(f"ERROR  {problem}")
    if problems:
        sys.exit(1)


def register(sub) -> None:
    p = sub.add_parser("mv", help="move or rename a note, updating links, weights and learned edges")
    p.add_argument("source", help="note to move: name or path, as in a [[link]]")
    p.add_argument("dest", help="new path relative to the data folder, or a new name")
    p.set_defaults(func=cmd_mv)
