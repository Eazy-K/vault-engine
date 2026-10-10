"""Run a deterministic, time-balanced slice of the test suite.

    python tests/run_shard.py --shard 2 --of 3     # run one shard (CI)
    python tests/run_shard.py --jobs 3             # all 3 shards in parallel, locally
    python tests/run_shard.py --list --of 3        # show the partition

Test modules (tests/test_*.py) are split with a greedy longest-processing-time
assignment over a per-module weight. Weights come from the checked-in table
tests/shard_weights.json (seconds measured on a Windows machine); a module
missing from the table gets the median weight, so new modules still land in
exactly one shard. The table only affects balance, never coverage.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
WEIGHTS_FILE = TESTS_DIR / "shard_weights.json"


def discover_modules(tests_dir: Path = TESTS_DIR) -> list[str]:
    return sorted(p.stem for p in tests_dir.glob("test_*.py"))


def load_weights(path: Path = WEIGHTS_FILE) -> dict[str, float]:
    try:
        return {k: float(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError):
        return {}


def partition(modules: list[str], count: int, weights: dict[str, float] | None = None) -> list[list[str]]:
    """Split modules into `count` shards: every module in exactly one shard."""
    weights = load_weights() if weights is None else weights
    known = [weights[m] for m in modules if m in weights]
    default = statistics.median(known) if known else 1.0
    cost = {m: weights.get(m, default) for m in modules}
    shards: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for m in sorted(modules, key=lambda m: (-cost[m], m)):
        i = min(range(count), key=lambda k: (loads[k], k))
        shards[i].append(m)
        loads[i] += cost[m]
    return [sorted(s) for s in shards]


def run_shard(modules: list[str]) -> int:
    sys.path.insert(0, str(TESTS_DIR))
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def run_parallel(count: int) -> int:
    start = time.perf_counter()
    procs = []
    for i in range(1, count + 1):
        cmd = [sys.executable, str(Path(__file__).resolve()), "--shard", str(i), "--of", str(count)]
        procs.append((i, subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          text=True, encoding="utf-8", errors="replace")))
    failed = []
    for i, proc in procs:
        out, _ = proc.communicate()
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if proc.returncode != 0:
            failed.append(i)
            print(f"--- shard {i} output (failed) ---")
            print(out)
        ran = next((ln for ln in lines if ln.startswith("Ran ")), "no 'Ran' line")
        verdict = next((ln for ln in reversed(lines) if ln.startswith(("OK", "FAILED"))), "no verdict line")
        print(f"shard {i}/{count}: exit={proc.returncode} {ran} | {verdict}")
    print(f"total wall time: {time.perf_counter() - start:.1f}s")
    if failed:
        print(f"FAILED shards: {failed}")
        return 1
    print("all shards OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--shard", type=int, help="1-based shard index to run")
    ap.add_argument("--of", type=int, help="total number of shards")
    ap.add_argument("--jobs", type=int, help="run this many shards in parallel subprocesses")
    ap.add_argument("--list", action="store_true", help="print the partition and exit")
    args = ap.parse_args(argv)
    count = args.of or args.jobs
    if not count or count < 1:
        ap.error("give --of N (with --shard I) or --jobs N")
    if args.list:
        for i, mods in enumerate(partition(discover_modules(), count), 1):
            print(f"shard {i}/{count}: {' '.join(mods)}")
        return 0
    if args.shard is None:
        if not args.jobs:
            ap.error("--of needs --shard (or use --jobs)")
        return run_parallel(args.jobs)
    if not 1 <= args.shard <= count:
        ap.error("--shard must be between 1 and --of")
    return run_shard(partition(discover_modules(), count)[args.shard - 1])


if __name__ == "__main__":
    sys.exit(main())
