"""Keep user-level agent config (Codex, Claude Code) in step with vault templates.

The engine ships generic templates under `defaults/config/`; a file at the same
relative path in the data repo (`<VAULT_DATA>/config/...`) replaces the engine's
copy, which is where a user's own values live. `user-config --install` merges
them into the user's home config without removing anything the user added:

  config/codex/developer-instructions.md  -> block inside the top-level
      `developer_instructions` string of <CODEX_HOME>/config.toml
  config/codex/default.rules              -> block inside <CODEX_HOME>/rules/default.rules
  config/claude/settings.json             -> deep-merged into <CLAUDE_CONFIG_DIR>/settings.json

Blocks sit between `# vault-engine:begin` and `# vault-engine:end`; text outside
is never touched. Every file is backed up before its first change.
Placeholders in templates: {VAULT_DATA}, {VAULT_ENGINE}, {WORKSPACE} (the parent
folder of the data repo), expanded to absolute paths with forward slashes.
"""
from __future__ import annotations

import argparse
import datetime
import json
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
    return {"{VAULT_DATA}": paths.data.resolve().as_posix(),
            "{VAULT_ENGINE}": paths.engine.resolve().as_posix(),
            "{WORKSPACE}": paths.data.resolve().parent.as_posix()}


def expand(text: str, values: dict[str, str]) -> str:
    for token, value in values.items():
        text = text.replace(token, value)
    return text


def _read_source(paths: g.Paths, rel: str) -> str | None:
    """Expanded, newline-normalized source text; None if absent or blank."""
    path = resolve_source(paths, rel)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    text = expand(text, placeholders(paths)).replace("\r\n", "\n").strip("\n")
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


def splice(body: str, block: str) -> str:
    """Replace the marked block in body with block, or append it. Text outside
    the markers is kept exactly."""
    lines = body.split("\n")
    begins = [i for i, line in enumerate(lines) if line.strip() == BEGIN]
    if begins:
        start = begins[0]
        ends = [i for i in range(start + 1, len(lines)) if lines[i].strip() == END]
        if not ends or len(begins) > 1:
            raise ConfigError("unbalanced vault-engine markers; fix them by hand")
        return "\n".join(lines[:start] + block.split("\n") + lines[ends[0] + 1:])
    if any(line.strip() == END for line in lines):
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
    source = _read_source(paths, RULES_SRC)
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
        items += [plan_instructions(paths, home), plan_rules(paths, home)]
    if not only_existing_homes or claude_dir.is_dir():
        items.append(plan_settings(paths, claude_dir))
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
