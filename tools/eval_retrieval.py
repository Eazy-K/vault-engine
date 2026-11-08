"""Offline retrieval evaluation: replay a labelled query set against the vault.

    graph.py eval --set queries.jsonl [--no-semantic] [--k 3,5,10] [--split dev|holdout|all] [--json]

Each line of the set is {"task", "ts", "query", "relevant": [note ids], "split": "dev"|"holdout"}.

Ranking used for a query: exactly the list `context` builds before it applies its
token budget, i.e. `graph.retrieve(...)` with the default threshold and depth. The
budget only decides which of those notes are printed and which are listed as
"omitted"; both groups keep that order, so loaded-then-omitted is the same list.
Differences from a real `context` call, on purpose:
  - core notes are left out of the ranking and out of each query's relevant set:
    they load for every query, so they say nothing about retrieval quality;
  - no project seeding: `context` seeds projects/<name>/* from the working
    directory, which would make results depend on where `eval` is run;
  - nothing is logged: no usage.log event, no task id, no learned-links change.
The one file that can change is the embeddings cache (a derived cache that `index`
and `context` also refresh); keyword-only mode (`--no-semantic`) writes nothing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import graph as g

DEFAULT_KS = (3, 5, 10)
SPLITS = ("dev", "holdout", "all")


def load_set(path: Path) -> list[dict]:
    rows = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or not isinstance(row.get("query"), str):
                raise ValueError("needs a string \"query\"")
            relevant = row.get("relevant")
            if not isinstance(relevant, list) or not all(isinstance(r, str) for r in relevant):
                raise ValueError("\"relevant\" must be a list of note ids")
            if row.get("split", "dev") not in ("dev", "holdout"):
                raise ValueError("\"split\" must be dev or holdout")
        except ValueError as exc:
            sys.exit(f"{path}:{lineno}: {exc}")
        rows.append(row)
    return rows


def rank(graph: "g.Graph", query: str, semantic: bool) -> list[str]:
    """Note ids in the order `context` would list them (loaded, then omitted), no core notes."""
    results = g.retrieve(graph, query, [], g.DEFAULT_THRESHOLD, g.DEFAULT_DEPTH,
                         include_core=False, semantic=semantic, project_notes=())
    return [r["id"] for r in results]


def query_metrics(ranking: list[str], relevant: set[str], ks: list[int]) -> dict:
    first = next((i for i, nid in enumerate(ranking, 1) if nid in relevant), 0)
    return {"recall": {k: len(relevant & set(ranking[:k])) / len(relevant) for k in ks},
            "rr": 1 / first if first else 0.0}


def summarize(per_query: list[dict], ks: list[int]) -> dict:
    n = len(per_query)
    mean = lambda vals: round(sum(vals) / n, 4) if n else None  # noqa: E731
    return {"n": n,
            "recall": {str(k): mean([q["recall"][k] for q in per_query]) for k in ks},
            "mrr": mean([q["rr"] for q in per_query])}


def evaluate(graph: "g.Graph", rows: list[dict], semantic: bool, ks: list[int],
             split: str = "all") -> dict:
    """Metrics per split for one mode. {"available": False, "reason": ...} when the
    semantic path fell back to keyword matching (Ollama missing)."""
    core = {nid for nid, note in graph.notes.items() if note.core}
    per_split: dict[str, list[dict]] = {"dev": [], "holdout": []}
    skipped = 0
    for row in rows:
        if split != "all" and row.get("split", "dev") != split:
            continue
        relevant = set(row["relevant"]) - core
        if not relevant:
            skipped += 1
            continue
        if semantic and row["query"].strip():
            g.RETRIEVAL.update(mode="keyword", reason="not run")
        ranking = rank(graph, row["query"], semantic)
        if semantic and row["query"].strip() and g.RETRIEVAL["mode"] != "semantic":
            return {"available": False, "reason": g.RETRIEVAL["reason"]}
        per_split[row.get("split", "dev")].append(query_metrics(ranking, relevant, ks))
    chosen = per_split["dev"] + per_split["holdout"] if split == "all" else per_split[split]
    return {"available": True, "skipped": skipped, "splits": {split: summarize(chosen, ks)}}


def parse_ks(raw: str) -> list[int]:
    try:
        ks = sorted({int(x) for x in raw.split(",") if x.strip()})
    except ValueError:
        ks = []
    if not ks or ks[0] < 1:
        sys.exit(f"--k needs positive integers separated by commas, got {raw!r}")
    return ks


def format_text(result: dict) -> str:
    modes = list(result["modes"])
    split = result["split"]
    width = 12
    lines = [f"split: {split}   set: {result['set']}",
             f"{'':<10}" + "".join(f"{m:>{width}}" for m in modes)]
    data = {m: result["modes"][m] for m in modes}

    def row(label, getter):
        cells = []
        for m in modes:
            d = data[m]
            v = getter(d["splits"][split]) if d["available"] else None
            cells.append(f"{('n/a' if v is None else (v if isinstance(v, str) else f'{v:.3f}')):>{width}}")
        lines.append(f"{label:<10}" + "".join(cells))

    row("n", lambda s: str(s["n"]))
    for k in result["k"]:
        row(f"recall@{k}", lambda s, k=k: s["recall"][str(k)])
    row("MRR", lambda s: s["mrr"])
    for m in modes:
        if not data[m]["available"]:
            lines.append(f"{m}: unavailable ({data[m]['reason']})")
        elif data[m]["skipped"]:
            lines.append(f"{m}: {data[m]['skipped']} queries skipped (no non-core relevant note)")
    return "\n".join(lines)


def cmd_eval(args) -> None:
    set_path = Path(args.set)
    if not set_path.is_file():
        sys.exit(f"eval set not found: {set_path}")
    ks = parse_ks(args.k)
    rows = load_set(set_path)
    graph = g.Graph(g.default_paths())
    modes = {"keyword": evaluate(graph, rows, False, ks, args.split)}
    if not args.no_semantic:
        modes["semantic"] = evaluate(graph, rows, True, ks, args.split)
    result = {"set": str(set_path), "split": args.split, "k": ks, "modes": modes}
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_text(result))


def register(sub) -> None:
    p = sub.add_parser("eval", help="measure retrieval quality (recall@k, MRR) on a labelled query set")
    p.add_argument("--set", required=True, help="JSONL file: task, ts, query, relevant, split")
    p.add_argument("--no-semantic", action="store_true", help="keyword-only column only")
    p.add_argument("--k", default=",".join(map(str, DEFAULT_KS)), help="cutoffs, default 3,5,10")
    p.add_argument("--split", choices=SPLITS, default="all")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_eval)
