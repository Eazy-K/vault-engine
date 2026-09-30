"""Keep user-level agent config (Codex, Claude Code) in step with vault templates.

The engine ships generic templates under `defaults/config/`; a file at the same
relative path in the data repo (`<VAULT_DATA>/config/...`) replaces the engine's
copy, which is where a user's own values live. `user-config --install` merges
them into the user's home config without removing anything the user added:

  config/codex/developer-instructions.md  -> block inside the top-level
      `developer_instructions` string of <CODEX_HOME>/config.toml
  config/codex/default.rules              -> block inside <CODEX_HOME>/rules/default.rules
  config/codex/config.toml                -> keys/tables upserted into <CODEX_HOME>/config.toml
      (plus config/codex/config.<windows|linux|darwin>.toml for this platform)
  config/claude/settings.json             -> deep-merged into <CLAUDE_CONFIG_DIR>/settings.json
  config/workspace/{AGENTS,CLAUDE}.md     -> `<!-- vault-engine:begin/end -->` block in the
      same-named file in {WORKSPACE}

Blocks sit between `# vault-engine:begin` and `# vault-engine:end`; text outside
is never touched. Every file is backed up before its first change.
Placeholders in templates: {VAULT_DATA}, {VAULT_ENGINE}, {WORKSPACE} (the parent
folder of the data repo), expanded to absolute paths with forward slashes, and the
same with a `_NATIVE` suffix using the OS separator (backslashes on Windows). Values
are escaped for the target format: Starlark strings in .rules, JSON strings in
settings.json; the TOML fragment is expanded after parsing, so any quoting works.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import math
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import graph as g

try:
    import tomllib
except ImportError:  # Python 3.10: no TOML parser, so config.toml is left alone
    tomllib = None

BEGIN = "# vault-engine:begin"
END = "# vault-engine:end"
INSTALL_HINT = 'python "$VAULT_ENGINE/tools/graph.py" user-config --install'
KEY = "developer_instructions"
INSTRUCTIONS_SRC = "config/codex/developer-instructions.md"
RULES_SRC = "config/codex/default.rules"
SETTINGS_SRC = "config/claude/settings.json"
CONFIG_SRC = "config/codex/config.toml"
WS_BEGIN = "<!-- vault-engine:begin -->"
WS_END = "<!-- vault-engine:end -->"
WS_FILES = ("AGENTS.md", "CLAUDE.md")


class ConfigError(Exception):
    """A target or source file cannot be merged safely."""


@dataclass
class Item:
    name: str
    target: Path
    status: str  # "ok" | "drift" | "error"
    message: str = ""
    new_bytes: bytes | None = None


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "").strip() or Path.home() / ".codex").expanduser()


# --- sources -----------------------------------------------------------------------

def resolve_source(paths: g.Paths, rel: str) -> Path | None:
    """The vault's config file, else the engine default, else None."""
    for root in (paths.data, paths.defaults_dir):
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return None


def placeholders(paths: g.Paths) -> dict[str, str]:
    """Forward-slash values, and `_NATIVE` variants with the OS separator."""
    data, engine = paths.data.resolve(), paths.engine.resolve()
    return {"{VAULT_DATA}": data.as_posix(),
            "{VAULT_ENGINE}": engine.as_posix(),
            "{WORKSPACE}": data.parent.as_posix(),
            "{VAULT_DATA_NATIVE}": os.fspath(data),
            "{VAULT_ENGINE_NATIVE}": os.fspath(engine),
            "{WORKSPACE_NATIVE}": os.fspath(data.parent)}


def starlark_escape(value: str) -> str:
    """Make value safe inside a "..." string of a Codex .rules file."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def expand(text: str, values: dict[str, str], escape=None) -> str:
    """Replace placeholder tokens; escape (optional) is applied to each value so it
    is safe in the target's string syntax."""
    for token, value in values.items():
        text = text.replace(token, escape(value) if escape else value)
    return text


def _read_source(paths: g.Paths, rel: str, escape=None, expand_text: bool = True) -> str | None:
    """Expanded, newline-normalized source text; None if absent or blank.
    escape: see expand(); expand_text=False leaves placeholders for the caller."""
    path = resolve_source(paths, rel)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if expand_text:
        text = expand(text, placeholders(paths), escape)
    text = text.replace("\r\n", "\n").strip("\n")
    return text if text.strip() else None


# --- target text helpers -------------------------------------------------------------

def _read_target(path: Path) -> tuple[str, str, bool]:
    """(text with \\n newlines, eol, had_bom) of a target; empty if it does not exist."""
    if not path.exists():
        return "", "\n", False
    try:
        raw = path.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return text.replace("\r\n", "\n"), "\r\n" if "\r\n" in text else "\n", bom


def _encode(text: str, eol: str, bom: bool) -> bytes:
    data = text.replace("\n", eol).encode("utf-8")
    return b"\xef\xbb\xbf" + data if bom else data


def splice(body: str, block: str, begin: str = BEGIN, end: str = END) -> str:
    """Replace the marked block in body with block, or append it. Text outside
    the markers is kept exactly."""
    lines = body.split("\n")
    begins = [i for i, line in enumerate(lines) if line.strip() == begin]
    if begins:
        start = begins[0]
        ends = [i for i in range(start + 1, len(lines)) if lines[i].strip() == end]
        if not ends or len(begins) > 1:
            raise ConfigError("unbalanced vault-engine markers; fix them by hand")
        return "\n".join(lines[:start] + block.split("\n") + lines[ends[0] + 1:])
    if any(line.strip() == end for line in lines):
        raise ConfigError("unbalanced vault-engine markers; fix them by hand")
    base = body.rstrip("\n")
    return (base + "\n\n" if base.strip() else "") + block + "\n"


def _block(source: str) -> str:
    return f"{BEGIN}\n{source}\n{END}"


# --- codex config.toml ---------------------------------------------------------------

def _find_key(text: str) -> tuple[str, int]:
    """("key", offset of the value) if a root developer_instructions key exists,
    else ("table", offset of the first table header) or ("end", len(text))."""
    pos, multiline = 0, None
    for line in text.split("\n"):
        start, pos = pos, pos + len(line) + 1
        if multiline:
            if re.search(r"(?<!\\)\"\"\"" if multiline == '"""' else "'''", line):
                multiline = None
            continue
        if re.match(r"\s*\[", line):
            return "table", start
        match = re.match(rf"\s*{KEY}\s*=\s*", line)
        if match:
            return "key", start + match.end()
        opener = re.match(r"\s*[\w.\"'-]+\s*=\s*(\"\"\"|''')", line)
        if opener and opener.group(1) not in line[opener.end():]:
            multiline = opener.group(1)
    return "end", len(text)


def _value_end(text: str, start: int) -> int:
    """Offset just past the string literal that begins at start."""
    if text.startswith("'''", start):
        end = text.find("'''", start + 3)
        if end < 0:
            raise ConfigError(f"unterminated {KEY} string")
        end += 3
        for _ in range(2):  # up to two quotes may end the content
            if text.startswith("'", end):
                end += 1
        return end
    if text.startswith('"""', start):
        match = re.compile(r'(?<!\\)"""').search(text, start + 3)
        if not match:
            raise ConfigError(f"unterminated {KEY} string")
        return match.end()
    quote = text[start:start + 1]
    if quote not in ("'", '"'):
        raise ConfigError(f"{KEY} is not a string; fix it by hand")
    i = start + 1
    while i < len(text) and text[i] != "\n":
        if quote == '"' and text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i + 1
        i += 1
    raise ConfigError(f"unterminated {KEY} string")


def _emit(value: str) -> str:
    """TOML source for value: a multi-line literal string when it can hold it."""
    if "'''" not in value and value.endswith("\n"):
        return "'''\n" + value + "'''"
    return json.dumps(value, ensure_ascii=False)


def merge_instructions(text: str, source: str) -> str:
    """config.toml text (\\n newlines) with the developer_instructions block set."""
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config.toml is not valid TOML: {exc}") from exc
    block = _block(source)
    kind, offset = _find_key(text)
    if kind == "key":
        old = before.get(KEY)
        if not isinstance(old, str):
            raise ConfigError(f"{KEY} is not a string; fix it by hand")
        new_value = splice(old, block)
        if not new_value.endswith("\n"):
            new_value += "\n"
        if new_value == old:
            return text
        end = _value_end(text, offset)
        result = text[:offset] + _emit(new_value) + text[end:]
    else:
        new_value = block + "\n"
        entry = f"{KEY} = {_emit(new_value)}\n"
        if kind == "table":
            result = text[:offset] + entry + "\n" + text[offset:]
        else:
            lead = "" if not text else ("" if text.endswith("\n") else "\n") + "\n"
            result = text + lead + entry
    try:
        after = tomllib.loads(result)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"refusing to write invalid TOML: {exc}") from exc
    expected = dict(before)
    expected[KEY] = new_value
    if after != expected:
        raise ConfigError("config.toml would change beyond developer_instructions; not written")
    return result


def plan_instructions(paths: g.Paths, home: Path) -> Item | None:
    source = _read_source(paths, INSTRUCTIONS_SRC)
    if source is None:
        return None
    if tomllib is None:
        return Item("codex developer_instructions", home / "config.toml", "error",
                    "needs Python 3.11+ (tomllib) to edit config.toml safely")
    return _plan_text("codex developer_instructions", home / "config.toml",
                      lambda text: merge_instructions(text, source))


# --- codex rules -----------------------------------------------------------------------

def plan_rules(paths: g.Paths, home: Path) -> Item | None:
    source = _read_source(paths, RULES_SRC, escape=starlark_escape)
    if source is None:
        return None
    return _plan_text("codex rules", home / "rules" / "default.rules",
                      lambda text: splice(text, _block(source)))


def _plan_text(name: str, target: Path, merge) -> Item:
    try:
        text, eol, bom = _read_target(target)
        merged = merge(text)
    except ConfigError as exc:
        return Item(name, target, "error", str(exc))
    if merged == text and target.exists():
        return Item(name, target, "ok")
    return Item(name, target, "drift", new_bytes=_encode(merged, eol, bom))


# --- codex config.toml fragment (upsert keys and tables) -----------------------------------

_BARE = re.compile(r"[A-Za-z0-9_-]+")
_KEY_PART = r"""(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'[^']*')"""
_KEY_RE = re.compile(rf"[ \t]*({_KEY_PART}(?:[ \t]*\.[ \t]*{_KEY_PART})*)[ \t]*=[ \t]*")


def _norm(key: str) -> str:
    """Comparison form of a key: path-like keys ignore slash style, a trailing
    slash and (on Windows) case."""
    if "/" in key or "\\" in key:
        key = key.replace("\\", "/").rstrip("/")
        if sys.platform == "win32":
            key = key.casefold()
    return key


def _lookup(container: dict, key: str) -> str | None:
    """The key of container that is equivalent to key, if any."""
    if key in container:
        return key
    wanted = _norm(key)
    return next((k for k in container if _norm(k) == wanted), None)


def _same(a, b) -> bool:
    """Equality that does not treat True as 1."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def _check_scalar(value, where: str) -> None:
    if isinstance(value, list):
        for item in value:
            _check_scalar(item, where)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigError(f"{where}: inf/nan is not supported")
    elif not isinstance(value, (str, bool, int)):
        raise ConfigError(f"{where}: unsupported value type {type(value).__name__} "
                          "(only strings, numbers, booleans and arrays of them)")


def _validate_fragment(fragment: dict, prefix: str = "") -> None:
    for key, value in fragment.items():
        if isinstance(value, dict):
            _validate_fragment(value, f"{prefix}{key}.")
        else:
            _check_scalar(value, f"{prefix}{key}")


def _deep_update(base: dict, other: dict) -> dict:
    for key, value in other.items():
        found = _lookup(base, key)
        if found is not None and isinstance(base[found], dict) and isinstance(value, dict):
            _deep_update(base[found], value)
        else:
            base[found if found is not None else key] = value
    return base


def _toml_str(value: str) -> str:
    """Literal '...' string when possible (Windows paths stay readable)."""
    if "'" not in value and not any((ord(c) < 32 and c != "\t") or ord(c) == 127 for c in value):
        return f"'{value}'"
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, str):
        return _toml_str(value)
    return repr(value)


def _toml_key(key: str) -> str:
    return key if _BARE.fullmatch(key) else _toml_str(key)


@dataclass
class _Section:
    path: tuple
    aot: bool  # [[array of tables]]
    header_start: int
    body_start: int
    stmts: list  # (key path, value start, value end, offset after the statement's last line)


def _value_extent(text: str, i: int) -> tuple[int, int]:
    """(end of the value, offset after its last line) for a value starting at i."""
    n, depth, last = len(text), 0, i
    while i < n:
        c = text[i]
        triple = text[i:i + 3]
        if triple in ('"""', "'''"):
            j = i + 3
            while j < n and not text.startswith(triple, j):
                j += 2 if triple == '"""' and text[j] == "\\" else 1
            if j >= n:
                raise ConfigError("unterminated multi-line string in config.toml")
            j += 3
            for _ in range(2):
                if text.startswith(triple[0], j):
                    j += 1
            last = i = j
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if c == '"' and text[j] == "\\" else 1
            if j >= n or text[j] != c:
                raise ConfigError("unterminated string in config.toml")
            last = i = j + 1
        elif c == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif c == "\n":
            if depth <= 0:
                break
            i += 1
        else:
            if c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
            if not c.isspace():
                last = i + 1
            i += 1
    return last, min(i + 1, n)


def _walk_path(tree) -> tuple:
    path = []
    while isinstance(tree, dict) and len(tree) == 1:
        (key, tree), = tree.items()
        path.append(key)
    return tuple(path)


def _header(line: str) -> tuple[tuple, bool] | None:
    """(path, is array of tables) if the line is a table header."""
    stripped = line.lstrip(" \t")
    if not stripped.startswith("["):
        return None
    aot = stripped.startswith("[[")
    start = 2 if aot else 1
    i, quote = start, ""
    while i < len(stripped):
        c = stripped[i]
        if quote:
            if c == quote:
                quote = ""
            elif c == "\\" and quote == '"':
                i += 1
        elif c in "\"'":
            quote = c
        elif c == "]":
            break
        i += 1
    try:
        return _walk_path(tomllib.loads(f"[{stripped[start:i]}]\n")), aot
    except tomllib.TOMLDecodeError:
        return None


def _scan(text: str) -> list[_Section]:
    """The root section plus one per table header, each with its key statements."""
    sections = [_Section((), False, 0, 0, [])]
    pos, n = 0, len(text)
    while pos < n:
        nl = text.find("\n", pos)
        line_end = n if nl < 0 else nl + 1
        line = text[pos:line_end].rstrip("\n")
        head = _header(line)
        if head:
            sections.append(_Section(head[0], head[1], pos, line_end, []))
        else:
            match = _KEY_RE.match(line)
            if match:
                vend, line_end = _value_extent(text, pos + match.end())
                try:
                    kpath = _walk_path(tomllib.loads(f"{match.group(1)} = 0\n"))
                except tomllib.TOMLDecodeError:
                    kpath = ()
                sections[-1].stmts.append((kpath, pos + match.end(), vend, line_end))
        pos = line_end
    return sections


def _flatten(fragment: dict, prefix: tuple = ()) -> list[tuple[tuple, dict]]:
    """[(table path, {key: value})] for the root and every table worth writing."""
    own = {k: v for k, v in fragment.items() if not isinstance(v, dict)}
    subs = {k: v for k, v in fragment.items() if isinstance(v, dict)}
    out = [(prefix, own)] if own or not subs else []
    for key, value in subs.items():
        out += _flatten(value, prefix + (key,))
    return out


def _find_section(sections: list[_Section], path: tuple) -> _Section | None:
    wanted = tuple(_norm(p) for p in path)
    for sec in sections:
        if not sec.aot and tuple(_norm(p) for p in sec.path) == wanted:
            return sec
    return None


def _dig(tree: dict, path: tuple, create: bool = False):
    for part in path:
        found = _lookup(tree, part)
        if found is None:
            if not create:
                return None
            found = part
            tree[found] = {}
        tree = tree[found]
        if not isinstance(tree, dict):
            raise ConfigError(f"config.toml: {'.'.join(path)} is not a table; fix it by hand")
    return tree


def _insert_point(text: str, sections: list[_Section], sec: _Section) -> tuple[int, str]:
    """(offset, text to add after the inserted lines) for new keys in sec."""
    if sec.stmts:
        return sec.stmts[-1][3], ""
    if sec.path:
        return sec.body_start, ""
    if len(sections) == 1:
        return len(text), ""
    # new top-level keys go before the first table (and the comments above it)
    at = sections[1].header_start
    above = text[:at].split("\n")[:-1]
    while above and above[-1].lstrip().startswith("#"):
        at -= len(above.pop()) + 1
    return at, "\n"


def merge_fragment(text: str, fragment: dict) -> str:
    """config.toml text (\\n newlines) with every fragment key set to the fragment's
    value. Nothing is deleted; untouched text stays byte-identical."""
    _validate_fragment(fragment)
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config.toml is not valid TOML: {exc}") from exc
    sections = _scan(text)
    edits: list[tuple[int, int, str]] = []
    inserts: dict[int, str] = {}
    new_tables: list[str] = []
    expected = copy.deepcopy(before)
    for path, entries in _flatten(fragment):
        table = _dig(expected, path, create=True)
        live = _dig(before, path)
        sec = _find_section(sections, path)
        if sec is None:
            body = "".join(f"{_toml_key(k)} = {_toml_value(v)}\n" for k, v in entries.items())
            new_tables.append("[" + ".".join(_toml_key(p) for p in path) + "]\n" + body)
            table.update(entries)
            continue
        fresh = ""
        for key, value in entries.items():
            stmt = next((s for s in sec.stmts
                         if len(s[0]) == 1 and _norm(s[0][0]) == _norm(key)), None)
            if stmt is None:
                fresh += f"{_toml_key(key)} = {_toml_value(value)}\n"
            elif live is not None and not _same(live.get(_lookup(live, stmt[0][0])), value):
                edits.append((stmt[1], stmt[2], _toml_value(value)))
            table[_lookup(table, key) or key] = value
        if fresh:
            at, tail = _insert_point(text, sections, sec)
            if at == len(text) and text and not text.endswith("\n"):
                fresh = "\n" + fresh
            inserts[at] = inserts.get(at, "") + fresh + tail
    if not edits and not inserts and not new_tables:
        return text
    changes = edits + [(at, at, ins) for at, ins in inserts.items()]
    result = text
    for start, end, replacement in sorted(changes, key=lambda c: (c[0], c[1]), reverse=True):
        result = result[:start] + replacement + result[end:]
    if new_tables:
        if result and not result.endswith("\n"):
            result += "\n"
        result += ("\n" if result.strip() else "") + "\n".join(new_tables)
    try:
        after = tomllib.loads(result)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"refusing to write invalid TOML: {exc}") from exc
    if not _same(after, expected):
        raise ConfigError("config.toml would change beyond the fragment; not written")
    return result


def _platform_name() -> str:
    plat = sys.platform
    return "windows" if plat.startswith("win") else "darwin" if plat == "darwin" else "linux"


def _config_sources() -> tuple[str, str]:
    return CONFIG_SRC, f"config/codex/config.{_platform_name()}.toml"


def _fragment(paths: g.Paths) -> dict | None:
    """The base fragment with this platform's overlay applied, placeholders expanded
    in keys and values after parsing (so any quoting style is safe); None if no source."""
    result: dict | None = None
    for rel in _config_sources():
        source = _read_source(paths, rel, expand_text=False)
        if source is None:
            continue
        try:
            part = tomllib.loads(source)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{rel} is not valid TOML: {exc}") from exc
        result = _deep_update(result or {}, _expand_json(part, placeholders(paths)))
    if result is not None:
        _validate_fragment(result)
    return result


def plan_config_toml(paths: g.Paths, home: Path, prior: Item | None = None) -> Item | None:
    """Fragment item for config.toml. If prior (developer_instructions) has pending
    changes to the same file, the result includes them."""
    name, target = "codex config.toml", home / "config.toml"
    if tomllib is None:
        if not any(resolve_source(paths, rel) for rel in _config_sources()):
            return None
        return Item(name, target, "error", "needs Python 3.11+ (tomllib) to edit config.toml safely")
    try:
        fragment = _fragment(paths)
        if fragment is None:
            return None
        disk, eol, bom = _read_target(target)
        start = disk
        if prior is not None and prior.status == "drift" and prior.new_bytes is not None:
            start = prior.new_bytes.decode("utf-8-sig").replace("\r\n", "\n")
        merged = merge_fragment(start, fragment)
    except ConfigError as exc:
        return Item(name, target, "error", str(exc))
    if merged == disk and target.exists():
        return Item(name, target, "ok")
    return Item(name, target, "drift", new_bytes=_encode(merged, eol, bom))


# --- workspace files -----------------------------------------------------------------------

def plan_workspace(paths: g.Paths, filename: str) -> Item | None:
    source = _read_source(paths, f"config/workspace/{filename}")
    if source is None:
        return None
    target = paths.data.resolve().parent / filename

    def merge(text: str) -> str:
        has_markers = any(line.strip() in (WS_BEGIN, WS_END) for line in text.split("\n"))
        if not has_markers and text.strip() == source.strip():
            return text  # already holds exactly this content: adopt it as is
        return splice(text, f"{WS_BEGIN}\n{source}\n{WS_END}", WS_BEGIN, WS_END)

    return _plan_text(f"workspace {filename}", target, merge)


# --- claude settings ---------------------------------------------------------------------

def deep_merge(base, fragment):
    """fragment merged into base: objects recursively, lists unioned (base order,
    missing items appended), anything else taken from fragment. Removes nothing."""
    if isinstance(base, dict) and isinstance(fragment, dict):
        result = dict(base)
        for key, value in fragment.items():
            result[key] = deep_merge(base[key], value) if key in base else value
        return result
    if isinstance(base, list) and isinstance(fragment, list):
        return base + [item for item in fragment if item not in base]
    return fragment


def _expand_json(value, values: dict[str, str]):
    if isinstance(value, str):
        return expand(value, values)
    if isinstance(value, list):
        return [_expand_json(v, values) for v in value]
    if isinstance(value, dict):
        return {expand(k, values): _expand_json(v, values) for k, v in value.items()}
    return value


def plan_settings(paths: g.Paths, claude_dir: Path) -> Item | None:
    src = resolve_source(paths, SETTINGS_SRC)
    if src is None:
        return None
    name, target = "claude settings", claude_dir / "settings.json"
    try:
        fragment = json.loads(src.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        return Item(name, target, "error", f"{src} is not valid JSON: {exc}")
    if not isinstance(fragment, dict):
        return Item(name, target, "error", f"{src} must hold a JSON object")
    fragment = _expand_json(fragment, placeholders(paths))
    try:
        text, eol, bom = _read_target(target)
        current = json.loads(text) if text.strip() else {}
    except (ConfigError, ValueError) as exc:
        return Item(name, target, "error", f"{target} is not valid JSON: {exc}")
    if not isinstance(current, dict):
        return Item(name, target, "error", f"{target} must hold a JSON object")
    merged = deep_merge(current, fragment)
    if merged == current and target.exists():
        return Item(name, target, "ok")
    out = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    return Item(name, target, "drift", new_bytes=_encode(out, eol, bom))


# --- orchestration -------------------------------------------------------------------------

def evaluate(paths: g.Paths, home: Path | None = None, claude_dir: Path | None = None,
             only_existing_homes: bool = False) -> list[Item]:
    """Every item that has a source. With only_existing_homes, items whose config
    dir does not exist on this computer are left out (doctor/context)."""
    home = home or codex_home()
    claude_dir = claude_dir or g.claude_config_dir()
    items: list[Item | None] = []
    if not only_existing_homes or home.is_dir():
        inst = plan_instructions(paths, home)
        frag = plan_config_toml(paths, home, inst)
        if frag is not None and inst is not None and inst.status == "drift" \
                and frag.status == "drift":
            inst = None  # the fragment item already carries the instructions change
        items += [inst, frag, plan_rules(paths, home)]
    if not only_existing_homes or claude_dir.is_dir():
        items.append(plan_settings(paths, claude_dir))
    if not only_existing_homes or paths.data.resolve().parent.is_dir():
        items += [plan_workspace(paths, name) for name in WS_FILES]
    return [item for item in items if item is not None]


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    n = 1
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{n}")
    shutil.copy2(path, candidate)
    return candidate


def install(items: list[Item]) -> list[tuple[Item, Path | None]]:
    done = []
    for item in items:
        if item.status != "drift" or item.new_bytes is None:
            continue
        saved = backup(item.target)
        item.target.parent.mkdir(parents=True, exist_ok=True)
        item.target.write_bytes(item.new_bytes)
        done.append((item, saved))
    return done


def statuses(paths: g.Paths) -> list[tuple[str, str]]:
    """("OK"|"WARN", message) per item for `doctor`."""
    out = []
    for item in evaluate(paths, only_existing_homes=True):
        if item.status == "ok":
            out.append(("OK", f"{item.name} matches the config template ({item.target})"))
        elif item.status == "drift":
            out.append(("WARN", f"{item.name} differs from the config template ({item.target}); "
                                f"run: {INSTALL_HINT}"))
        else:
            out.append(("WARN", f"{item.name}: {item.message}"))
    return out


def has_drift(paths: g.Paths) -> bool:
    return any(item.status == "drift" for item in evaluate(paths, only_existing_homes=True))


def cmd_user_config(args: argparse.Namespace) -> None:
    data = Path(args.data).expanduser().resolve() if args.data else g.resolve_data_dir()
    paths = g.Paths(g.ENGINE, data)
    items = evaluate(paths)
    if not items:
        print("user-config: no config templates (nothing in defaults/config or <data>/config)")
        return
    if args.install:
        failed = [i for i in items if i.status == "error"]
        for item in failed:
            print(f"ERROR {item.name}: {item.message}")
        for item, saved in install(items):
            print(f"updated {item.name}: {item.target}" + (f" (backup: {saved})" if saved else ""))
        for item in items:
            if item.status == "ok":
                print(f"up to date {item.name}: {item.target}")
        sys.exit(1 if failed else 0)
    drift = False
    for item in items:
        if item.status == "ok":
            print(f"OK   {item.name} ({item.target})")
        else:
            drift = True
            detail = item.message or f"differs from the config template; run: {INSTALL_HINT}"
            print(f"WARN {item.name} ({item.target}): {detail}")
    sys.exit(1 if drift else 0)


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("user-config", help="sync Codex/Claude user config from vault templates")
    parser.add_argument("--check", action="store_true",
                        help="report drift, exit 1 if any (default)")
    parser.add_argument("--install", action="store_true",
                        help="apply the templates, backing up each file before its first change")
    parser.add_argument("--data", help="data dir (default: resolve_data_dir())")
    parser.set_defaults(func=cmd_user_config)
