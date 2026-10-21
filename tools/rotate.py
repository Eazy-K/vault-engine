"""Rotate a project's status note: move its oldest dated items to the history note.

The status note keeps at most `MAX_STATUS_ITEMS` dated items under its recent
section; older ones belong in the append-only journal of `<project>-history`.
Doing that by hand is error-prone, so `rotate` does it deterministically.
"""
from __future__ import annotations

import sys

import graph as g


def _history_text(project: str, journal: str, items: list[str]) -> str:
    return (f"---\nkeywords: [{project}, history, journal]\n"
            f"links:\n  - \"[[{project}-status]]\"\n"
            f"weights:\n  {project}-status: 0.6\n---\n"
            f"# {project}: history\n\n## {journal}\n" + "\n".join(items) + "\n")


def _append_to_journal(text: str, journal: str, items: list[str]) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the trailing newline; added back below
    span = g.section_span(lines, journal)
    block = "\n".join(items).split("\n")
    if span is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"## {journal}"] + block
    else:
        at = span[1]
        while at > span[0] and not lines[at - 1].strip():
            at -= 1  # after the last content line, before the blank lines that follow
        lines[at:at] = block
    return "\n".join(lines) + "\n"


def cmd_rotate(args) -> None:
    paths = g.default_paths()
    project = args.project
    status_id = g.status_note_id(project)
    history_id = f"projects/{project}/{project}-history"
    status_path = paths.data / (status_id + ".md")
    history_path = paths.data / (history_id + ".md")
    if not status_path.is_file():
        sys.exit(f"no status note {status_id}")
    config = g.status_note_config(paths)
    recent, journal = config["recent_heading"], config["journal_heading"]
    text = status_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    lines = text.split("\n")
    span = g.section_span(lines, recent)
    if span is None:
        sys.exit(f'{status_id} has no "## {recent}" section; set status_note.recent_heading '
                 f"in vault.config.json to the heading your status note uses")
    items = g.dated_item_lines(lines[span[0]:span[1]])
    if len(items) <= g.MAX_STATUS_ITEMS:
        print(f"nothing to rotate: {status_id} has {len(items)} dated items "
              f"(max {g.MAX_STATUS_ITEMS})")
        return
    # Oldest first; among equal dates the item lower in the section is the older one.
    order = sorted(range(len(items)), key=lambda i: (items[i][0], -i))
    moving = order[:len(items) - g.MAX_STATUS_ITEMS]
    drop: set[int] = set()
    moved: list[str] = []
    for i in moving:
        date, start, item_lines = items[i]
        drop.update(range(span[0] + start, span[0] + start + len(item_lines)))
        moved.append("\n".join(item_lines))
    new_status = "\n".join(line for n, line in enumerate(lines) if n not in drop)
    dates = [items[i][0] for i in moving]
    summary = (f"rotated {len(moving)} item(s) from {status_id} to {history_id} "
               f"({dates[0]}..{dates[-1]}); {g.MAX_STATUS_ITEMS} remain")
    if args.dry_run:
        print("dry-run: " + summary)
        return
    g._require_writable(paths)
    if history_path.is_file():
        new_history = _append_to_journal(history_path.read_text(encoding="utf-8"), journal, moved)
    else:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        new_history = _history_text(project, journal, moved)
    g.atomic_write_text(history_path, new_history)
    g.atomic_write_text(status_path, new_status)
    print(summary)
    _, body = g.parse_frontmatter(new_status)
    if len(body) > g.MAX_ENTRY_CHARS:
        print(f"{status_id} still has {len(body)} chars (max {g.MAX_ENTRY_CHARS}); "
              f"move older sections to {history_id} by hand")
    if not args.no_commit:
        files = [status_id + ".md", history_id + ".md"]
        g.report_commit(paths.data, files,
                        g.commit_own_files(paths.data, files, f"chore: rotate {project} status note"))


def register(sub) -> None:
    p = sub.add_parser("rotate", help="move the oldest dated items of a project's status note "
                                      "to its history note")
    p.add_argument("project", help="project name (the folder under projects/)")
    p.add_argument("--no-commit", action="store_true",
                   help="leave the two changed notes uncommitted")
    p.add_argument("--dry-run", action="store_true", help="show what would move; write nothing")
    p.set_defaults(func=cmd_rotate)
