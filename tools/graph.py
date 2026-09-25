#!/usr/bin/env python3
"""Weighted knowledge-graph retrieval for the vault.

Notes are nodes and wikilinks are weighted, undirected edges. A query activates
seed notes by semantic similarity (local multilingual embeddings via Ollama)
blended with keyword match, activation spreads along edges (max-product), and
every note at or above the threshold is returned. Notes used together can be
reinforced (Hebbian learning); learned deltas live in .graph/learned.json so the
notes themselves stay untouched. Without Ollama it falls back to keywords only.

Standard library only, so any agent can run it with a stock Python.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from pathlib import Path

# This repo is the user-independent engine; notes live in a separate data
# folder (see resolve_data_dir). ENGINE never changes; DATA is resolved lazily
# so importing this module needs no environment at all (tests rely on that).
ENGINE = Path(__file__).resolve().parent.parent


def resolve_data_dir() -> Path:
    """Locate the notes folder. VAULT_DATA is the current name; VAULT_HOME is
    kept for backward compatibility with setups that predate the split."""
    raw = os.environ.get("VAULT_DATA") or os.environ.get("VAULT_HOME")
    if not raw:
        sys.exit(
            "VAULT_DATA is not set. Point it at your notes folder, e.g.\n"
            "  export VAULT_DATA=/path/to/your/vault-data\n"
            "(VAULT_HOME also works, for setups from before the engine/data split.)"
        )
    return Path(raw).expanduser().resolve()


class _LazyPath:
    """A Path that resolves on first use, not at module import.

    Used only so the personal-data guard section below (out of scope for this
    change; another agent is rewriting it) keeps compiling and working against
    a bare `VAULT` name without forcing VAULT_DATA to be set just to import
    this module.
    """

    def __init__(self, resolver):
        self._resolver = resolver
        self._cached: Path | None = None

    def _resolve(self) -> Path:
        if self._cached is None:
            self._cached = self._resolver()
        return self._cached

    def __truediv__(self, other):
        return self._resolve() / other

    def __fspath__(self) -> str:
        return str(self._resolve())

    def __str__(self) -> str:
        return str(self._resolve())


# TODO(guard): the guard section still treats "the vault" as one folder. Once
# it is rewritten it should probably scan DATA (and maybe ENGINE) explicitly
# instead of this lazy alias.
VAULT = _LazyPath(resolve_data_dir)


@dataclass
class Paths:
    """Every on-disk location the engine touches, rooted at engine + data."""

    engine: Path
    data: Path

    @property
    def defaults_dir(self) -> Path:
        return self.engine / "defaults"

    @property
    def learned_dir(self) -> Path:
        # One file per machine so two machines never edit the same file (no
        # merge conflicts); effective learned weights are the sum over all files.
        return self.data / ".graph" / "learned"

    @property
    def embed_cache(self) -> Path:
        return self.data / ".graph" / "embeddings.json"  # derived, not committed

    @property
    def usage_log(self) -> Path:
        return self.data / ".graph" / "usage.log"  # local JSON lines, not committed

    @property
    def config_file(self) -> Path:
        return self.data / "vault.config.json"


def default_paths() -> Paths:
    return Paths(ENGINE, resolve_data_dir())


OLLAMA_URL = os.environ.get("VAULT_OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("VAULT_EMBED_MODEL", "bge-m3")
KEEP_ALIVE = "30m"
EMBED_TIMEOUT = 120  # first call may load the model from disk
SKIP_DIRS = {".git", ".obsidian", ".graph", "tools", "__pycache__"}
SKIP_ROOT_FILES = {"AGENTS.md", "CLAUDE.md", "README.md"}

DEFAULT_LINK_WEIGHT = 0.7  # frontmatter link without an explicit weight
BODY_LINK_WEIGHT = 0.5  # wikilink written in the note body
LEARNING_RATE = 0.1
DECAY_RATE = 0.05
DEFAULT_THRESHOLD = 0.6  # 0.5 let two-hop neighbours of every seed in
DEFAULT_DEPTH = 3
SEED_RATIO = 0.3  # notes scoring below this share of the best match are not seeds
SEMANTIC_WEIGHT = 0.5  # share of the seed score from embeddings; rest is curated keywords
# Raw cosine scores sit close together, so they are sharpened relative to the
# best match: exp((cos - best) / T). A gap of T drops a note to ~0.37.
SEMANTIC_TEMPERATURE = 0.03
# Below this best-chunk cosine nothing in the vault is about the query (bge-m3,
# tuned on small samples: relevant >= 0.50, unrelated ~0.36).
SEMANTIC_FLOOR = 0.42
DEFAULT_BUDGET = 2000  # tokens of note content printed by `context`
CHARS_PER_TOKEN = 3  # conservative estimate for Turkish text
MIN_LEARNED = 0.005
MAX_CORE_LINES = 15  # non-empty body lines
TASK_STATUSES = ("open", "in-progress", "done", "blocked")


def machine_name() -> str:
    # A hostname can reveal an employer or a person and learned files are committed,
    # so an explicitly chosen name wins over the hostname.
    raw = os.environ.get("VAULT_MACHINE") or socket.gethostname()
    return re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-") or "unknown"


def detect_agent() -> tuple[str, str | None]:
    if os.environ.get("CLAUDECODE"):
        return "claude-code", os.environ.get("CLAUDE_CODE_SESSION_ID")
    # Unverified: Codex's environment variables have not been checked yet.
    if any(key.startswith("CODEX") for key in os.environ):
        return "codex", None
    return "unknown", None

WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
TOKEN = re.compile(r"\w+", re.UNICODE)
STOPWORDS = {
    "ve", "ile", "için", "bir", "bu", "şu", "da", "de", "mi", "ne", "nasıl",
    "the", "and", "for", "to", "of", "a", "an", "in", "on", "how", "what",
}


# --- frontmatter -------------------------------------------------------------

def _split_inline(s: str) -> list[str]:
    parts, buf, quote = [], "", None
    for ch in s:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf += ch
        elif ch == ",":
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    return parts


def _scalar(raw: str):
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    if s.startswith("[[") and s.endswith("]]"):
        return s
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_scalar(p) for p in _split_inline(inner)] if inner else []
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    for cast in (int, float):
        try:
            return cast(s)
        except ValueError:
            pass
    return s


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse the YAML subset Obsidian writes: scalars, lists, one-level maps."""
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    data: dict = {}
    key = None
    for line in text[4:end].split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] not in " \t":
            k, _, v = line.partition(":")
            key = k.strip()
            data[key] = _scalar(v) if v.strip() else None
            continue
        if key is None:
            continue
        item = line.strip()
        if item == "-" or item.startswith("- "):
            if not isinstance(data[key], list):
                data[key] = []
            data[key].append(_scalar(item[1:]))
        elif ":" in item:
            if not isinstance(data[key], dict):
                data[key] = {}
            k, _, v = item.partition(":")
            data[key][str(_scalar(k))] = _scalar(v)
    body = text[end + 4:].lstrip("\n")
    return data, body


# --- notes and graph ---------------------------------------------------------

def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def tokenize(text: str) -> list[str]:
    return [t.casefold() for t in TOKEN.findall(text)]


@dataclass
class Note:
    id: str
    path: Path
    title: str
    meta: dict
    body: str
    keywords: set[str] = field(default_factory=set)
    id_tokens: set[str] = field(default_factory=set)
    body_tokens: set[str] = field(default_factory=set)
    source: str = "data"  # "default" (shipped with the engine) or "data" (the user's own)

    @property
    def core(self) -> bool:
        return self.meta.get("core") is True

    def declared_links(self) -> dict[str, float]:
        """Link targets as written, mapped to their base weight."""
        weights = self.meta.get("weights")
        weights = weights if isinstance(weights, dict) else {}
        out: dict[str, float] = {}
        for item in _as_list(self.meta.get("links")):
            for target in WIKILINK.findall(str(item)):
                target = target.strip()
                try:
                    out[target] = float(weights.get(target, DEFAULT_LINK_WEIGHT))
                except (TypeError, ValueError):
                    out[target] = DEFAULT_LINK_WEIGHT
        for target in WIKILINK.findall(CODE.sub("", self.body)):
            out.setdefault(target.strip(), BODY_LINK_WEIGHT)
        return out


def _load_notes_from(root: Path, source: str) -> dict[str, Note]:
    notes: dict[str, Note] = {}
    if not root.is_dir():
        return notes
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        if len(rel.parts) == 1 and rel.name in SKIP_ROOT_FILES:
            continue
        if rel.name.startswith("_"):
            continue
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        heading = re.search(r"^#\s+(.+)$", body, re.M)
        note = Note(
            id=rel.with_suffix("").as_posix(),
            path=path,
            title=heading.group(1).strip() if heading else rel.stem,
            meta=meta,
            body=body,
            source=source,
        )
        words = [note.title] + [str(v) for v in _as_list(meta.get("keywords"))]
        words += [str(v) for v in _as_list(meta.get("tags"))]
        note.keywords = set(tokenize(" ".join(words)))
        note.id_tokens = set(tokenize(note.id.replace("/", " ").replace("-", " ")))
        note.body_tokens = set(tokenize(body))
        notes[note.id] = note
    return notes


def load_notes(data: Path, defaults: Path | None = None) -> dict[str, Note]:
    """Defaults (shipped generic notes) first, then data notes of the same id
    override them, so a user can customise or replace any default note."""
    notes = _load_notes_from(defaults, "default") if defaults is not None else {}
    notes.update(_load_notes_from(data, "data"))
    return notes


def pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


class Graph:
    def __init__(self, paths: Paths | None = None):
        self.paths = paths or default_paths()
        self.notes = load_notes(self.paths.data, self.paths.defaults_dir)
        self.problems: list[str] = []
        self.base: dict[tuple[str, str], float] = {}
        self.learned: dict[tuple[str, str], float] = {}
        by_name: dict[str, list[str]] = {}
        for nid in self.notes:
            by_name.setdefault(nid.rsplit("/", 1)[-1].casefold(), []).append(nid)
        self._by_name = by_name

        for note in self.notes.values():
            for target, weight in note.declared_links().items():
                resolved, error = self.resolve(target)
                if error:
                    self.problems.append(f"{note.id}: [[{target}]] {error}")
                    continue
                if resolved == note.id:
                    continue
                key = pair(note.id, resolved)
                self.base[key] = max(self.base.get(key, 0.0), _clamp(weight))

        self.machine = machine_name()
        self.own_learned: dict[tuple[str, str], float] = {}  # this machine's file only
        learned_dir = self.paths.learned_dir
        for path in sorted(learned_dir.glob("*.json")) if learned_dir.exists() else []:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
            for k, v in raw.items():
                a, _, b = k.partition("|")
                key = pair(a, b)
                self.learned[key] = self.learned.get(key, 0.0) + float(v)
                if path.stem == self.machine:
                    self.own_learned[key] = float(v)

        self.adjacency: dict[str, dict[str, float]] = {nid: {} for nid in self.notes}
        for a, b in set(self.base) | set(self.learned):
            if a in self.notes and b in self.notes:
                w = self.weight(a, b)
                self.adjacency[a][b] = w
                self.adjacency[b][a] = w

    def resolve(self, target: str) -> tuple[str | None, str | None]:
        t = target.strip().removesuffix(".md").strip("/")
        if t in self.notes:
            return t, None
        if "/" in t:
            matches = [i for i in self.notes if i.endswith("/" + t)]
        else:
            matches = self._by_name.get(t.casefold(), [])
        if len(matches) == 1:
            return matches[0], None
        return None, "ambiguous" if matches else "missing"

    def weight(self, a: str, b: str) -> float:
        key = pair(a, b)
        return _clamp(self.base.get(key, 0.0) + self.learned.get(key, 0.0))

    def add_learned(self, key: tuple[str, str], delta: float) -> None:
        self.own_learned[key] = self.own_learned.get(key, 0.0) + delta
        self.learned[key] = self.learned.get(key, 0.0) + delta

    def save_learned(self) -> None:
        learned_dir = self.paths.learned_dir
        learned_dir.mkdir(parents=True, exist_ok=True)
        data = {f"{a}|{b}": round(v, 4) for (a, b), v in sorted(self.own_learned.items())}
        (learned_dir / f"{self.machine}.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8", newline="\n",
        )


def _clamp(w: float) -> float:
    return max(0.0, min(1.0, w))


# --- retrieval ---------------------------------------------------------------

def _matches(q: str, t: str) -> bool:
    # Cheap stemming: a shared prefix of 4+ chars covers most Turkish suffixes.
    if q == t:
        return True
    return min(len(q), len(t)) >= 4 and (t.startswith(q) or q.startswith(t))


def score(note: Note, query_tokens: list[str], include_body: bool = True) -> float:
    fields = [(3.0, note.keywords), (2.0, note.id_tokens)]
    if include_body:
        fields.append((1.0, note.body_tokens))
    total = 0.0
    for q in query_tokens:
        total += max(
            (w for w, toks in fields if any(_matches(q, t) for t in toks)), default=0.0
        )
    return total


def _embed(texts: list[str]) -> list[list[float]]:
    body = json.dumps({"model": EMBED_MODEL, "input": texts, "keep_alive": KEEP_ALIVE})
    req = urllib.request.Request(f"{OLLAMA_URL}/api/embed", data=body.encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
        vectors = json.load(resp)["embeddings"]
    out = []
    for v in vectors:
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([round(x / norm, 5) for x in v])
    return out


def chunks(note: Note) -> list[str]:
    """One chunk per bullet, table row or paragraph line, prefixed with its context.

    Embedding a whole note blurs mixed topics together; small chunks keep each
    statement distinct and the note scores by its best chunk.
    """
    # Keywords are left to the keyword matcher: as a bag of words they embed loosely
    # and match unrelated queries.
    out = []
    section = ""
    header: list[str] = []  # column names of the current table
    body = re.sub(r"```.*?```", "", note.body, flags=re.S).replace("`", "").replace("**", "")
    lines = [line.strip() for line in body.split("\n")]
    for i, line in enumerate(lines):
        if not line or re.fullmatch(r"[|\-:\s]+", line):
            continue
        if line.startswith("#"):
            section = line.lstrip("#").strip()
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            is_header = i + 1 < len(lines) and re.fullmatch(r"\|?[\s:|-]+\|?", lines[i + 1] or "x")
            if is_header:
                header = cells
                continue
            # Pair cells with column names so a bare row like "Backend | Java" keeps its meaning.
            text = "; ".join(f"{h}: {c}" if h else c for h, c in zip(header + [""] * len(cells), cells))
        else:
            header = []
            text = re.sub(r"^[-*]\s+|^\d+\.\s+", "", line)
        context = note.title if section in ("", note.title) else f"{note.title} / {section}"
        out.append(f"{context}: {text}")
    return out or [note.title]


def refresh_embeddings(graph: Graph, query: str | None = None) -> tuple[dict, list | None]:
    """Embed changed notes (and the query) in one call. Returns (cache, query vector)."""
    embed_cache = graph.paths.embed_cache
    cache = {"model": EMBED_MODEL, "notes": {}}
    if embed_cache.exists():
        loaded = json.loads(embed_cache.read_text(encoding="utf-8") or "{}")
        if loaded.get("model") == EMBED_MODEL:
            cache = loaded
    notes = cache["notes"]
    stale, texts = [], []
    for nid, note in graph.notes.items():
        parts = chunks(note)
        digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
        if notes.get(nid, {}).get("hash") != digest:
            stale.append((nid, digest, len(parts)))
            texts.extend(parts)
    removed = [nid for nid in notes if nid not in graph.notes]
    vectors = _embed(texts + ([query] if query else [])) if texts or query else []
    pos = 0
    for nid, digest, count in stale:
        notes[nid] = {"hash": digest, "vectors": vectors[pos:pos + count]}
        pos += count
    for nid in removed:
        del notes[nid]
    if stale or removed:
        embed_cache.parent.mkdir(exist_ok=True)
        embed_cache.write_text(json.dumps(cache), encoding="utf-8")
    return cache, (vectors[-1] if query else None)


def semantic_scores(graph: Graph, text: str) -> dict[str, float] | None:
    """Sharpened cosine similarity per note, or None when Ollama is unavailable."""
    try:
        cache, qvec = refresh_embeddings(graph, text)
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        print(f"warning: semantic search unavailable ({exc}); keyword match only",
              file=sys.stderr)
        return None
    cosines = {nid: max(sum(a * b for a, b in zip(qvec, v))
                        for v in cache["notes"][nid]["vectors"])
               for nid in graph.notes}
    best = max(cosines.values(), default=0.0)
    if best < SEMANTIC_FLOOR:
        return {}
    return {nid: math.exp((c - best) / SEMANTIC_TEMPERATURE) for nid, c in cosines.items()}


def seed(graph: Graph, text: str, explicit: list[str], semantic: bool = True) -> dict[str, float]:
    seeds: dict[str, float] = {}
    for target in explicit:
        nid, error = graph.resolve(target)
        if error:
            sys.exit(f"seed [[{target}]] {error}")
        seeds[nid] = 1.0
    query = [t for t in tokenize(text) if len(t) >= 3 and t not in STOPWORDS]
    sem = semantic_scores(graph, text) if semantic and text.strip() else None
    lexical: dict[str, float] = {}
    if query:
        # Embeddings already cover note bodies; body keyword hits (e.g. "git" in
        # "git reposu") only add noise then, so they are a fallback-only signal.
        scores = {nid: score(n, query, include_body=sem is None)
                  for nid, n in graph.notes.items()}
        best = max(scores.values(), default=0.0)
        if best > 0:
            lexical = {nid: s / best for nid, s in scores.items()}
    if sem:
        combined = {nid: SEMANTIC_WEIGHT * sem[nid] + (1 - SEMANTIC_WEIGHT) * lexical.get(nid, 0.0)
                    for nid in graph.notes}
        best = max(combined.values())
        combined = {nid: s / best for nid, s in combined.items()}
    else:
        combined = lexical
    for nid, s in combined.items():
        if s >= SEED_RATIO:
            seeds[nid] = max(seeds.get(nid, 0.0), s)
    return seeds


def spread(graph: Graph, seeds: dict[str, float], threshold: float, depth: int):
    activation = dict(seeds)
    via: dict[str, str | None] = {nid: None for nid in seeds}
    frontier = [nid for nid, a in seeds.items() if a >= threshold]
    for _ in range(depth):
        nxt = []
        for nid in frontier:
            for other, w in graph.adjacency[nid].items():
                a = activation[nid] * w
                if a >= threshold and a > activation.get(other, 0.0):
                    activation[other] = a
                    via[other] = nid
                    nxt.append(other)
        if not nxt:
            break
        frontier = nxt
    return activation, via


def retrieve(graph: Graph, text: str, seeds: list[str], threshold: float, depth: int,
             include_core: bool = True, semantic: bool = True) -> list[dict]:
    activation, via = spread(graph, seed(graph, text, seeds, semantic), threshold, depth)
    results = {
        nid: {"id": nid, "activation": round(a, 3), "via": via[nid], "core": False}
        for nid, a in activation.items() if a >= threshold
    }
    if include_core:
        for nid, note in graph.notes.items():
            if note.core:
                results[nid] = {"id": nid, "activation": 1.0, "via": None, "core": True}
    # Finished tasks stay in the graph as history but are no longer loaded as context;
    # what they taught belongs in the project's status/notes.
    for nid in [n for n in results if is_done_task(graph.notes[n])]:
        del results[nid]
    return sorted(results.values(), key=lambda r: (not r["core"], -r["activation"], r["id"]))


def is_done_task(note: Note) -> bool:
    return note.meta.get("type") == "task" and note.meta.get("status") == "done"


# --- project detection --------------------------------------------------------

def project_roots(paths: Paths) -> list[Path]:
    """Folders whose immediate subfolders are projects. vault.config.json in the
    data folder can list them explicitly; otherwise the engine's own parent
    folder is assumed (the common "sibling projects" layout)."""
    try:
        raw = json.loads(paths.config_file.read_text(encoding="utf-8"))
        roots = raw.get("project_roots")
    except (OSError, ValueError):
        roots = None
    if roots:
        return [Path(r).expanduser().resolve() for r in roots]
    return [paths.engine.resolve().parent]


def detect_project(cwd: Path, paths: Paths) -> str | None:
    """The project name for `cwd`, or None outside any project root.

    A path inside the engine or the data folder is never a project, even if it
    also happens to sit under a configured root.
    """
    cwd = cwd.resolve()
    for excluded in (paths.engine.resolve(), paths.data.resolve()):
        if cwd == excluded or excluded in cwd.parents:
            return None
    for root in project_roots(paths):
        if root in cwd.parents:
            return cwd.relative_to(root).parts[0]
    return None


# --- personal data guard -----------------------------------------------------
# KVKK personal data and secrets must never reach a vault repository.
# Checksums (TCKN, IBAN, Luhn) keep random digit runs from raising false alarms.
# Real people's names cannot be caught reliably by pattern; that part relies on
# masking by the agent (standards/data-policy.md).

GUARD_IGNORE = "guard:ignore"
EMAIL_ALLOW = re.compile(
    r"@(?:example\.(?:com|org|net)|anthropic\.com|users\.noreply\.github\.com)$", re.I)


def _tckn_ok(s: str) -> bool:
    d = [int(c) for c in s]
    return (d[0] != 0 and d[9] == (sum(d[0:9:2]) * 7 - sum(d[1:8:2])) % 10
            and d[10] == sum(d[:10]) % 10)


def _iban_ok(s: str) -> bool:
    s = re.sub(r"\s", "", s).upper()
    return int("".join(str(int(c, 36)) for c in s[4:] + s[:4])) % 97 == 1


def _luhn_ok(s: str) -> bool:
    digits = [int(c) for c in re.sub(r"\D", "", s)][::-1]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2)
                for i, d in enumerate(digits))
    return total % 10 == 0


GUARD_PATTERNS = [
    ("TCKN", re.compile(r"(?<!\d)[1-9]\d{10}(?!\d)"), _tckn_ok),
    ("IBAN", re.compile(r"\bTR\d{2}(?: ?\d{4}){5} ?\d{2}\b", re.I), _iban_ok),
    ("card number", re.compile(r"(?<![\d.])\d(?:[ -]?\d){12,18}(?![\d.])"), _luhn_ok),
    ("phone", re.compile(r"(?<![\d+])(?:\+90|0)[ -]?\(?[2-5]\d{2}\)?[ -]?\d{3}[ -]?\d{2}[ -]?\d{2}(?!\d)"), None),
    ("phone", re.compile(r"(?<!\d)5\d{2}[ -]\d{3}[ -]\d{2}[ -]\d{2}(?!\d)"), None),
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), lambda m: not EMAIL_ALLOW.search(m)),
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"), None),
    ("token", re.compile(r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36,}|github_pat_\w{22,}"
                         r"|sk-(?:ant-)?[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,})"), None),
    ("credential in URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"), None),
    ("password", re.compile(r"(?i)\b(?:password|passwd|pwd|parola|şifre)\s*[:=]\s*(?![<*{$]|x{3})\S{4,}"), None),
]


def scan_line(line: str) -> list[str]:
    if GUARD_IGNORE in line:
        return []
    found = []
    for label, pattern, valid in GUARD_PATTERNS:
        for m in pattern.finditer(line):
            if valid is None or valid(m.group(0)):
                found.append(label)
    return found


def _repo_root() -> Path:
    """Git top-level of the current working directory, so guard/leakcheck work on
    whichever repo (engine or a data repo) they are invoked from, not just VAULT."""
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, encoding="utf-8", check=True).stdout
    return Path(out.strip())


def _git_dir(root: Path) -> Path:
    """Real .git directory for root, resolved via git so a worktree's shared
    common dir (where .git/info lives) is found rather than its private one."""
    out = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=root,
                          capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()
    p = Path(out)
    return p if p.is_absolute() else root / p


def _staged_lines(root: Path) -> list[tuple[str, int, str]]:
    """Added lines of the staged diff as (path, line number, text)."""
    diff = subprocess.run(
        ["git", "diff", "--cached", "-U0", "--no-color", "--diff-filter=ACMR"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", check=True).stdout
    out, path, lineno = [], "", 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("@@"):
            lineno = int(re.search(r"\+(\d+)", line).group(1))
        elif line.startswith("+") and path:
            out.append((path, lineno, line[1:]))
            lineno += 1
    return out


def cmd_guard(args) -> None:
    root = _repo_root()
    if args.staged:
        lines = _staged_lines(root)
    elif args.message_file:
        text = Path(args.message_file).read_text(encoding="utf-8", errors="replace")
        lines = [("commit message", i, l) for i, l in enumerate(text.splitlines(), 1)
                 if not l.startswith("#")]
    else:
        paths = args.paths or subprocess.run(
            ["git", "ls-files"], cwd=root, capture_output=True, text=True,
            encoding="utf-8", check=True).stdout.splitlines()
        lines = []
        for p in paths:
            full = Path(p) if Path(p).is_absolute() else root / p
            try:
                text = full.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            lines += [(p, i, l) for i, l in enumerate(text.splitlines(), 1)]
    # Report only where and what kind: echoing the value would leak it into logs.
    findings = [(p, n, label) for p, n, text in lines for label in scan_line(text)]
    for p, n, label in findings:
        print(f"{p}:{n}: possible {label}")
    if findings:
        sys.exit(f"blocked: {len(findings)} possible personal data / secret match(es). Mask them "
                 f"(standards/data-policy.md) or, if a false alarm, add '{GUARD_IGNORE}' to the line.")
    print("guard: clean")


# --- engine leak check ---------------------------------------------------------
# The ENGINE repo (this one) must never contain a user's content or identity;
# that belongs in per-user DATA repos. leakcheck runs the guard patterns above
# plus path- and identity-based checks scoped to whichever repo it runs in.

CONTENT_TOP_DIRS = {"profile", "projects", "notes", "inbox", "decisions", ".graph"}
CONTENT_TOP_FILES = {"vault.config.json"}
# Example/scaffold content shipped by the engine lives under these, not the repo top level.
CONTENT_EXEMPT_PREFIXES = ("defaults/", "templates/")


def _is_user_content_path(p: str) -> bool:
    """True for a path that is user content and must not live in the engine repo."""
    norm = p.replace("\\", "/")
    if norm.startswith(CONTENT_EXEMPT_PREFIXES):
        return False
    first, sep, rest = norm.partition("/")
    if sep and first in CONTENT_TOP_DIRS:
        return True
    return not sep and norm in CONTENT_TOP_FILES


def _leak_terms(root: Path) -> list[tuple[str, str]]:
    """(term, kind) pairs that would identify the user: home path, git identity,
    and an optional local (never committed) denylist."""
    terms: list[tuple[str, str]] = []
    home = str(Path.home())
    for variant in {home, home.replace("\\", "/"), home.replace("/", "\\")}:
        if len(variant) >= 4:
            terms.append((variant, "home path"))
    for key in ("user.name", "user.email"):
        val = subprocess.run(["git", "config", key], cwd=root, capture_output=True,
                              text=True, encoding="utf-8").stdout.strip()
        if len(val) >= 4:
            terms.append((val, "git identity"))
    try:
        deny = (_git_dir(root) / "info" / "vault-denylist").read_text(encoding="utf-8")
    except OSError:
        deny = ""
    for line in deny.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and len(line) >= 4:
            terms.append((line, "denylist term"))
    return terms


def _scan_leak(line: str, terms: list[tuple[str, str]]) -> list[str]:
    if GUARD_IGNORE in line:
        return []
    found = scan_line(line)
    low = line.lower()
    found += [kind for term, kind in terms if term.lower() in low]
    return found


def cmd_leakcheck(args) -> None:
    root = _repo_root()
    terms = _leak_terms(root)
    findings: list[tuple[str, int, str]] = []
    if args.message_file:
        text = Path(args.message_file).read_text(encoding="utf-8", errors="replace")
        lines = [("commit message", i, l) for i, l in enumerate(text.splitlines(), 1)
                 if not l.startswith("#")]
    elif args.staged:
        paths = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=root, capture_output=True, text=True, encoding="utf-8", check=True).stdout.splitlines()
        findings += [(p, 0, "user content path") for p in paths if _is_user_content_path(p)]
        lines = _staged_lines(root)
    else:
        paths = args.paths or subprocess.run(
            ["git", "ls-files"], cwd=root, capture_output=True, text=True,
            encoding="utf-8", check=True).stdout.splitlines()
        findings += [(p, 0, "user content path") for p in paths if _is_user_content_path(p)]
        lines = []
        for p in paths:
            full = Path(p) if Path(p).is_absolute() else root / p
            try:
                text = full.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            lines += [(p, i, l) for i, l in enumerate(text.splitlines(), 1)]
    # Report only where and what kind: echoing the value would leak it into logs.
    findings += [(p, n, label) for p, n, text in lines for label in _scan_leak(text, terms)]
    for p, n, label in findings:
        print(f"{p}:{n}: possible {label}" if n else f"{p}: {label}")
    if findings:
        sys.exit(f"blocked: {len(findings)} leak risk(s). The engine repo must not contain user content "
                 f"(it belongs in a data repo) or secrets (standards/data-policy.md); add "
                 f"'{GUARD_IGNORE}' to a line if it's a false alarm.")
    print("leakcheck: clean")


# --- usage log ---------------------------------------------------------------

def log_usage(paths: Paths, event: dict) -> None:
    # Telemetry must never break retrieval, so write errors are ignored.
    agent, session = detect_agent()
    event = {"ts": datetime.now().isoformat(timespec="seconds"), "agent": agent,
             "session": session, "machine": machine_name(), **event}
    try:
        usage_log = paths.usage_log
        usage_log.parent.mkdir(exist_ok=True)
        with usage_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_usage(paths: Paths) -> list[dict]:
    usage_log = paths.usage_log
    if not usage_log.exists():
        return []
    events = []
    for line in usage_log.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


# --- commands ----------------------------------------------------------------

def cmd_query(args, content: bool) -> None:
    paths = default_paths()
    graph = Graph(paths)
    project = args.project or (None if args.no_project else detect_project(Path.cwd(), paths))
    project_seeds = (sorted(nid for nid in graph.notes if nid.startswith(f"projects/{project}/"))
                     if project else [])
    results = retrieve(graph, args.text, args.seed + project_seeds, args.threshold, args.depth,
                       include_core=content or args.core, semantic=not args.no_semantic)
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return
    if content and project and not project_seeds:
        print(f"<!-- project {project} has no notes: ask the user whether to create "
              f"projects/{project}/{project}-overview.md and "
              f"projects/{project}/{project}-status.md -->")
    if not results:
        print("No notes above threshold.")
        return
    if not content:
        for r in results:
            origin = "core" if r["core"] else (f"via {r['via']}" if r["via"] else "seed")
            print(f"{r['activation']:.3f}  {r['id']}  ({origin})")
        return
    # Core notes always go in; the rest follow in activation order and stop at the
    # first note that does not fit, so a less relevant small note never displaces
    # a more relevant large one. If not even the top note fits, it is truncated.
    budget = args.budget * CHARS_PER_TOKEN
    used, omitted, loaded = 0, [], []
    for r in results:
        note = graph.notes[r["id"]]
        if omitted:
            omitted.append(note.id)
            continue
        origin = "core" if r["core"] else (f"via {r['via']}" if r["via"] else "seed")
        header = f"<!-- note: {note.id} | activation: {r['activation']:.3f} | {origin} -->\n"
        body = note.body.rstrip()
        if not r["core"] and used + len(header) + len(body) > budget:
            room = budget - used - len(header)
            if loaded or room < 200:
                omitted.append(note.id)
                continue
            body = body[:room].rsplit("\n", 1)[0] + "\n<!-- truncated -->"
        used += len(header) + len(body)
        if not r["core"]:
            loaded.append(note.id)
        print(header + body + "\n")
    if omitted:
        print(f"<!-- omitted over budget: {', '.join(omitted)} -->")
    if not args.no_log:
        task = secrets.token_hex(3)
        log_usage(paths, {"event": "context", "task": task, "query": args.text,
                          "notes": loaded, "omitted": omitted})
        # Absolute and quoted so the hint works when pasted from any cwd, not just this repo's.
        print(f"<!-- when done: python \"{Path(__file__).resolve()}\" reinforce --task {task} "
              f"<notes you actually used, or none> -->")


def cmd_index(_args) -> None:
    graph = Graph()
    try:
        cache, _ = refresh_embeddings(graph)
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        sys.exit(f"embedding failed: {exc}")
    count = sum(len(n["vectors"]) for n in cache["notes"].values())
    print(f"{len(cache['notes'])} notes, {count} chunks embedded with {EMBED_MODEL}")


def cmd_reinforce(args) -> None:
    paths = default_paths()
    graph = Graph(paths)
    ids = []
    for target in args.notes:
        nid, error = graph.resolve(target)
        if error:
            sys.exit(f"[[{target}]] {error}")
        if nid not in ids:
            ids.append(nid)
    if not ids and not args.task:
        sys.exit("reinforce needs notes, or --task to record that none were useful")
    # Fewer than two notes strengthens nothing but still closes the task in the
    # usage log: "only one note / no note helped" is a useful signal too.
    log_usage(paths, {"event": "reinforce", "task": args.task, "notes": ids})
    if len(ids) < 2:
        print(f"recorded {len(ids)} used note(s); no edge to strengthen")
        return
    for a, b in combinations(ids, 2):
        before = graph.weight(a, b)
        graph.add_learned(pair(a, b), args.rate * (1.0 - before))
        print(f"{a} <-> {b}: {before:.3f} -> {graph.weight(a, b):.3f}")
    graph.save_learned()


def cmd_stats(_args) -> None:
    events = read_usage(default_paths())
    contexts = [e for e in events if e.get("event") == "context"]
    reinforces = [e for e in events if e.get("event") == "reinforce"]
    closed = {e["task"] for e in reinforces if e.get("task")}
    done = sum(1 for c in contexts if c.get("task") in closed)
    rate = f"{done / len(contexts):.0%}" if contexts else "-"
    print(f"context calls:       {len(contexts)}")
    print(f"closed by reinforce: {done} ({rate})")
    # Per agent: tools without hooks (e.g. Codex) must comply from instructions alone.
    for agent in sorted({c.get("agent", "unknown") for c in contexts}):
        mine = [c for c in contexts if c.get("agent", "unknown") == agent]
        ok = sum(1 for c in mine if c.get("task") in closed)
        print(f"  {agent:<18} {ok}/{len(mine)} ({ok / len(mine):.0%})")
    print(f"reinforce w/o task:  {sum(1 for e in reinforces if not e.get('task'))}")
    print(f"no useful notes:     {sum(1 for e in reinforces if not e.get('notes'))}")
    # Retrieved-but-never-used notes point at retrieval noise.
    retrieved = Counter(n for c in contexts for n in c.get("notes", []))
    used = Counter(n for e in reinforces for n in e.get("notes", []))
    if retrieved:
        print("\nnote                              retrieved  used")
        for nid, count in retrieved.most_common():
            print(f"{nid:<34}{count:>9}{used.get(nid, 0):>6}")


def cmd_decay(args) -> None:
    # Each machine decays only its own file, so decay never causes merge conflicts.
    graph = Graph()
    kept = {}
    for key, delta in graph.own_learned.items():
        delta *= 1.0 - args.rate
        if abs(delta) >= MIN_LEARNED:
            kept[key] = delta
    dropped = len(graph.own_learned) - len(kept)
    print(f"decayed {len(graph.own_learned)} learned edges on {graph.machine}, dropped {dropped}")
    graph.own_learned = kept
    graph.save_learned()


def cmd_tasks(args) -> None:
    graph = Graph()
    rows = []
    for nid, note in graph.notes.items():
        if note.meta.get("type") != "task":
            continue
        status, project = str(note.meta.get("status")), str(note.meta.get("project"))
        if (args.status and status != args.status) or (args.project and project != args.project):
            continue
        rows.append((project, TASK_STATUSES.index(status) if status in TASK_STATUSES else 9,
                     status, nid, note.title))
    if not rows:
        print("No tasks.")
    for project, _, status, nid, title in sorted(rows):
        print(f"{status:<12} {nid}  {title}")


def cmd_show(args) -> None:
    graph = Graph()
    nid, error = graph.resolve(args.note)
    if error:
        sys.exit(f"[[{args.note}]] {error}")
    note = graph.notes[nid]
    print(f"{nid}  ({note.title}){'  [core]' if note.core else ''}")
    for other, w in sorted(graph.adjacency[nid].items(), key=lambda kv: -kv[1]):
        key = pair(nid, other)
        print(f"  {w:.3f}  {other}  (base {graph.base.get(key, 0.0):.3f}, "
              f"learned {graph.learned.get(key, 0.0):+.3f})")


def cmd_lint(_args) -> None:
    graph = Graph()
    errors = list(graph.problems)
    for a, b in graph.learned:
        for nid in (a, b):
            if nid not in graph.notes:
                errors.append(f"learned edge {a}|{b}: {nid} missing")
    warnings = [f"{nid}: orphan (no edges)" for nid, adj in graph.adjacency.items()
                if not adj and not graph.notes[nid].core]
    warnings += [f"{nid}: no frontmatter" for nid, n in graph.notes.items() if not n.meta]
    for nid, note in graph.notes.items():
        if note.meta.get("type") != "task":
            continue
        if note.meta.get("status") not in TASK_STATUSES:
            errors.append(f"{nid}: task status must be one of {', '.join(TASK_STATUSES)}")
        if not note.meta.get("project"):
            errors.append(f"{nid}: task has no project")
    # Core notes ride along with every context call, so their size is a fixed cost.
    for nid, note in graph.notes.items():
        lines = [line for line in note.body.splitlines() if line.strip()]
        if note.core and len(lines) > MAX_CORE_LINES:
            warnings.append(f"{nid}: core note has {len(lines)} lines (max {MAX_CORE_LINES})")
    for line in errors:
        print(f"ERROR  {line}")
    for line in warnings:
        print(f"WARN   {line}")
    edges = len({k for k in set(graph.base) | set(graph.learned)})
    print(f"{len(graph.notes)} notes, {edges} edges, {len(errors)} errors, {len(warnings)} warnings")
    sys.exit(1 if errors else 0)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (("query", "list notes activated by a query"),
                            ("context", "print core notes + activated notes for an agent")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("text", nargs="?", default="")
        p.add_argument("--seed", action="append", default=[], help="note to activate at 1.0")
        p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
        p.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
        p.add_argument("--json", action="store_true")
        p.add_argument("--no-semantic", action="store_true", help="keyword match only")
        p.add_argument("--project", help="seed projects/<name>/* notes (default: detect from cwd)")
        p.add_argument("--no-project", action="store_true", help="disable project detection")
        if name == "query":
            p.add_argument("--core", action="store_true", help="include core notes")
        else:
            p.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                           help="max tokens of note content (core notes always included)")
            p.add_argument("--no-log", action="store_true", help="skip the usage log")

    p = sub.add_parser("reinforce", help="strengthen edges between notes used together")
    p.add_argument("notes", nargs="*")
    p.add_argument("--task", help="task id printed by `context`")
    p.add_argument("--rate", type=float, default=LEARNING_RATE)

    p = sub.add_parser("decay", help="weaken all learned edges")
    p.add_argument("--rate", type=float, default=DECAY_RATE)

    p = sub.add_parser("show", help="show a note's edges")
    p.add_argument("note")

    sub.add_parser("lint", help="check broken links, orphans and learned state")
    sub.add_parser("index", help="embed changed notes ahead of time")
    sub.add_parser("stats", help="how often context calls are closed by reinforce")

    p = sub.add_parser("tasks", help="list inbox tasks")
    p.add_argument("--status", choices=TASK_STATUSES)
    p.add_argument("--project")

    p = sub.add_parser("guard", help="block personal data (KVKK) and secrets")
    p.add_argument("paths", nargs="*", help="files to scan (default: all tracked files)")
    p.add_argument("--staged", action="store_true", help="scan added lines of the staged diff")
    p.add_argument("--message-file", help="scan a commit message file")

    p = sub.add_parser("leakcheck", help="block user content and secrets from leaking into the engine repo")
    p.add_argument("paths", nargs="*", help="files to scan (default: all tracked files)")
    p.add_argument("--staged", action="store_true", help="scan the staged diff and staged paths")
    p.add_argument("--message-file", help="scan a commit message file")

    args = parser.parse_args()
    if args.command in ("query", "context"):
        cmd_query(args, content=args.command == "context")
    else:
        {"reinforce": cmd_reinforce, "decay": cmd_decay, "show": cmd_show,
         "lint": cmd_lint, "index": cmd_index, "stats": cmd_stats,
         "tasks": cmd_tasks, "guard": cmd_guard,
         "leakcheck": cmd_leakcheck}[args.command](args)


if __name__ == "__main__":
    main()
