"""`queue-runner`: the nightly runner that turns the oldest `ready` inbox task into a PR.

One task per run, no human prompt. Flow: guard (machine.json `queue` block) -> pick the
oldest eligible `ready` task -> claim it (status `in-progress`, commit, push) -> worktree
on a new branch from origin/main -> `claude -p` with a budget -> the runner itself runs the
task's acceptance command -> green: push + `gh pr create`, status `review`; red / budget
hit / error / timeout: status `blocked` with the reason in the task Log. Merge stays with
the user. Pure stdlib; every external command is an argv list (no shell, except the task's
own acceptance command)."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -I` drops the script dir
import graph as g  # noqa: E402

DEFAULT_TIMEOUT = 5400  # seconds for the claude run
DEFAULT_TIME = "02:00"
DEFAULT_MAX_TURNS = 80
BUDGET_SUBTYPES = ("error_max_budget_usd", "error_max_turns")
TAIL_LINES = 15
ACCEPTANCE_TIMEOUT = 1800

PROMPT = """\
Work the vault task in {task_path} (task id {task_id}, project {project}).
Follow the AGENTS.md flow of this repository. Work only in this worktree ({worktree}) on the
current branch ({branch}). Commit your work on this branch. Do not push, do not open a PR, do
not merge, do not switch branches. Write progress to the task's ## Log section. Run the task's
acceptance command before you finish and end with its exit code and summary line.
"""


class QueueError(Exception):
    """Refusal or setup problem."""


def _run(argv: list[str], cwd: Path | str | None = None, timeout: int | None = None,
         shell: bool = False) -> subprocess.CompletedProcess:
    """The one place that starts processes (tests replace it)."""
    return subprocess.run(argv, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, shell=shell)


def _git(repo: Path, *args: str, timeout: int | None = 300) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(repo), *args], timeout=timeout)


# --- guard -------------------------------------------------------------------

def load_queue_config(paths: g.Paths) -> dict:
    """The validated `queue` block of .graph/machine.json; QueueError when the queue may not run.

    Shape: {"queue": {"enabled": true, "projects": ["a", "b"], "budgets_usd": 5,
    "max_turns": 80}}. `max_turns` is optional (built-in default); the dollar ceiling is not."""
    raw = g.load_state_json(paths.data / ".graph" / "machine.json")
    block = raw.get("queue") if isinstance(raw, dict) else None
    if not isinstance(block, dict) or block.get("enabled") is not True:
        raise QueueError('queue is not enabled: set "queue": {"enabled": true, ...} in '
                         ".graph/machine.json on this computer")
    projects = block.get("projects")
    if (not isinstance(projects, list) or not projects
            or not all(isinstance(p, str) and p for p in projects)):
        raise QueueError("queue.projects must be a non-empty list of project names")
    shape = ('{"queue": {"enabled": true, "projects": [...], "budgets_usd": 5, '
             '"max_turns": 80}}')
    budget = block.get("budgets_usd")
    if budget is None:
        raise QueueError("queue.budgets_usd (one dollar ceiling per task) is missing; "
                         f"the queue never runs without one. Shape: {shape}")
    if isinstance(budget, dict):
        raise QueueError("queue.budgets_usd is a per-class object (old shape); budget classes "
                         f"are gone. Use one number, e.g. {shape}")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
        raise QueueError(f"queue.budgets_usd must be a positive number, e.g. {shape}")
    turns = block.get("max_turns", DEFAULT_MAX_TURNS)
    if isinstance(turns, dict):
        raise QueueError("queue.max_turns is a per-class object (old shape); budget classes "
                         f"are gone. Use one number, e.g. {shape}")
    if isinstance(turns, bool) or not isinstance(turns, int) or turns <= 0:
        raise QueueError(f"queue.max_turns must be a positive integer, e.g. {shape}")
    return {"projects": list(projects), "budgets_usd": float(budget), "max_turns": turns}


# --- pick --------------------------------------------------------------------

def pick_task(paths: g.Paths, config: dict) -> dict | None:
    """Oldest `ready` task (by created, then id) of an allowed project that has a runnable
    acceptance command."""
    candidates = []
    for nid, note in g.load_notes(paths.data, paths.defaults_dir).items():
        meta = note.meta
        if meta.get("type") != "task" or meta.get("status") != "ready":
            continue
        project = str(meta.get("project"))
        if project not in config["projects"]:
            continue
        acceptance = g.task_acceptance_command(note.body)
        if not acceptance or acceptance == "human":
            continue
        candidates.append((str(meta.get("created") or ""), nid, {
            "id": nid, "path": note.path, "project": project,
            "acceptance": acceptance}))
    if not candidates:
        return None
    return min(candidates, key=lambda c: (c[0], c[1]))[2]


def task_names(task: dict) -> tuple[str, str]:
    """(number, slug) from the file name `0057-some-slug`; number falls back to the stem."""
    stem = Path(task["path"]).stem
    m = re.match(r"^(\d+)-(.+)$", stem)
    return (m.group(1), m.group(2)) if m else (stem, stem)


# --- task file edits ---------------------------------------------------------

def set_status(path: Path, status: str) -> None:
    """Replace only the `status:` line of the frontmatter."""
    text = Path(path).read_bytes().decode("utf-8")
    m = re.match(r"^(---\r?\n)(.*?)(\r?\n---)", text, re.S)
    if not m:
        raise QueueError(f"{path}: no frontmatter")
    head = re.sub(r"^status:.*$", f"status: {status}", m.group(2), count=1, flags=re.M)
    Path(path).write_bytes((m.group(1) + head + text[m.end(2):]).encode("utf-8"))


def append_log(path: Path, line: str) -> None:
    """Append a line at the end of the `## Log` section (created when missing)."""
    text = Path(path).read_bytes().decode("utf-8")
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    start = next((i for i, ln in enumerate(lines) if re.match(r"^##\s+Log\s*$", ln)), None)
    if start is None:
        lines = lines + ["", "## Log", line]
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        last = end
        while last > start + 1 and not lines[last - 1].strip():
            last -= 1
        lines.insert(last, line)
    Path(path).write_bytes(nl.join(lines).encode("utf-8"))


# --- data repo ---------------------------------------------------------------

def _has_remote(repo: Path) -> bool:
    cp = _git(repo, "remote")
    return cp.returncode == 0 and bool(cp.stdout.strip())


def _is_repo(repo: Path) -> bool:
    return (repo / ".git").exists()


def _rel(paths: g.Paths, task: dict) -> str:
    return Path(task["path"]).resolve().relative_to(paths.data.resolve()).as_posix()


def sync_data(paths: g.Paths) -> None:
    if _is_repo(paths.data) and _has_remote(paths.data):
        cp = _git(paths.data, "pull", "--rebase", "--autostash")
        if cp.returncode != 0:
            raise QueueError(f"data repo pull failed: {cp.stderr.strip()[:300]}")


def commit_data(paths: g.Paths, task: dict, message: str, push: bool = True) -> str | None:
    """Commit the task file (only it); push when there is a remote. Returns an error text
    when the commit or push failed, else None."""
    if not _is_repo(paths.data):
        return None
    rel = _rel(paths, task)
    _git(paths.data, "add", "--", rel)
    cp = _git(paths.data, "commit", "-m", message, "--", rel)
    if cp.returncode != 0:
        return f"commit failed: {(cp.stderr or cp.stdout).strip()[:300]}"
    if push and _has_remote(paths.data):
        cp = _git(paths.data, "push")
        if cp.returncode != 0:
            _git(paths.data, "pull", "--rebase", "--autostash")
            cp = _git(paths.data, "push")
            if cp.returncode != 0:
                return f"push failed: {cp.stderr.strip()[:300]}"
    return None


def claim(paths: g.Paths, task: dict) -> None:
    """status -> in-progress, committed and pushed. A rejected push means another machine
    claimed the task first: undo the local commit and raise."""
    set_status(task["path"], "in-progress")
    err = commit_data(paths, task, f"chore(queue): claim {task_names(task)[0]}", push=False)
    if err:
        raise QueueError(f"claim failed: {err}")
    if _is_repo(paths.data) and _has_remote(paths.data):
        cp = _git(paths.data, "push")
        if cp.returncode != 0:
            _git(paths.data, "reset", "--mixed", "HEAD~1")
            _git(paths.data, "checkout", "--", _rel(paths, task))
            raise QueueError("claim push rejected (another machine took the task?): "
                             + cp.stderr.strip()[:200])


# --- project folder and worktree ---------------------------------------------

def project_repo(paths: g.Paths, project: str) -> Path:
    """The project's git folder: <project root>/<project>, roots as for `projects`."""
    for root in g.project_roots(paths):
        candidate = root / project
        if candidate.is_dir() and _is_repo(candidate):
            return candidate.resolve()
    raise QueueError(f"no git folder for project {project} under the project roots")


def make_worktree(repo: Path, number: str, slug: str) -> tuple[Path, str]:
    branch = f"feat/queue-{slug}"
    wt = repo / ".claude" / "worktrees" / f"queue-{number}"
    cp = _git(repo, "fetch", "origin")
    if cp.returncode != 0:
        raise QueueError(f"git fetch failed: {cp.stderr.strip()[:300]}")
    wt.parent.mkdir(parents=True, exist_ok=True)
    cp = _git(repo, "worktree", "add", "-b", branch, str(wt), "origin/main")
    if cp.returncode != 0:
        raise QueueError(f"worktree add failed: {cp.stderr.strip()[:300]}")
    return wt, branch


# --- claude ------------------------------------------------------------------

def claude_argv(claude: str, prompt: str, max_turns: int, budget_usd: float) -> list[str]:
    exe = [sys.executable, claude] if claude.lower().endswith(".py") else [claude]
    return [*exe, "-p", prompt, "--permission-mode", "auto", "--max-turns", str(max_turns),
            "--max-budget-usd", f"{budget_usd:g}", "--output-format", "json"]


def parse_claude_result(stdout: str) -> dict:
    """Tolerant parse of `--output-format json`; always returns every key."""
    out = {"parsed": False, "result": "", "subtype": "", "is_error": False, "usd": 0.0,
           "turns": 0, "tokens": 0, "duration_ms": 0}
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return out
    if isinstance(data, list):  # verbose form: a list of events, the last is the result
        data = next((e for e in reversed(data)
                     if isinstance(e, dict) and e.get("type") == "result"), {})
    if not isinstance(data, dict):
        return out

    def num(key, kind):
        v = data.get(key)
        return kind(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else kind(0)

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    tokens = sum(int(usage[k]) for k in ("input_tokens", "output_tokens",
                 "cache_creation_input_tokens", "cache_read_input_tokens")
                 if isinstance(usage.get(k), (int, float)))
    out.update(parsed=True, result=str(data.get("result") or ""),
               subtype=str(data.get("subtype") or ""), is_error=bool(data.get("is_error")),
               usd=num("total_cost_usd", float), turns=num("num_turns", int), tokens=tokens,
               duration_ms=num("duration_ms", int))
    return out


def run_claude(argv: list[str], wt: Path, timeout: int):
    """(CompletedProcess | None, timed_out)"""
    try:
        return _run(argv, cwd=wt, timeout=timeout), False
    except subprocess.TimeoutExpired:
        return None, True


def run_acceptance(command: str, wt: Path) -> dict:
    """Run the task's acceptance command (a shell string) in the worktree."""
    try:
        cp = _run([command], cwd=wt, timeout=ACCEPTANCE_TIMEOUT, shell=True)
        text, code = (cp.stdout or "") + (cp.stderr or ""), cp.returncode
    except subprocess.TimeoutExpired:
        text, code = "acceptance command timed out", 124
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return {"command": command, "exit": code, "tail": "\n".join(lines[-TAIL_LINES:])}


def create_pr(wt: Path, branch: str, title: str, body: str) -> tuple[str | None, str]:
    cp = _git(wt, "push", "-u", "origin", branch)
    if cp.returncode != 0:
        return None, f"git push failed: {cp.stderr.strip()[:300]}"
    cp = _run(["gh", "pr", "create", "--base", "main", "--head", branch, "--title", title,
               "--body", body], cwd=wt, timeout=300)
    if cp.returncode != 0:
        return None, f"gh pr create failed: {(cp.stderr or cp.stdout).strip()[:300]}"
    url = next((ln.strip() for ln in reversed(cp.stdout.splitlines())
                if ln.strip().startswith("http")), "")
    return (url or None), ("" if url else "gh returned no PR url")


# --- the run -----------------------------------------------------------------

def _one_line(text, limit: int = 300) -> str:
    return " ".join(str(text).split())[:limit]


def _finish(paths: g.Paths, event: dict, started: float) -> dict:
    event.setdefault("duration_s", round(time.time() - started, 1))
    g.log_usage(paths, {"event": "queue_run", **event})
    return event


def execute(args, paths: g.Paths | None = None) -> dict:
    """Run the queue once; returns the usage event (outcome pr|blocked|empty|refused)."""
    paths = paths or g.default_paths()
    started = time.time()
    base = {"task": "", "project": "", "turns": 0, "tokens": 0, "usd": 0.0, "reason": "", "pr": ""}
    try:
        config = load_queue_config(paths)
    except QueueError as exc:
        _finish(paths, {**base, "outcome": "refused", "reason": str(exc)}, started)
        raise
    if not args.dry_run:
        sync_data(paths)
    task = pick_task(paths, config)
    if task is None:
        print("queue empty")
        if args.dry_run:
            return {**base, "outcome": "empty"}
        return _finish(paths, {**base, "outcome": "empty"}, started)
    number, slug = task_names(task)
    max_turns, budget = config["max_turns"], config["budgets_usd"]
    claude = args.claude or shutil.which("claude") or "claude"
    event = {**base, "task": task["id"], "project": task["project"]}
    repo = project_repo(paths, task["project"])
    wt_path = repo / ".claude" / "worktrees" / f"queue-{number}"
    branch = f"feat/queue-{slug}"
    prompt = PROMPT.format(task_path=task["path"], task_id=task["id"], project=task["project"],
                           worktree=wt_path, branch=branch)
    argv = claude_argv(claude, prompt, max_turns, budget)
    if args.dry_run:
        print(f"task:     {task['id']} (project {task['project']})")
        print(f"repo:     {repo}")
        print(f"worktree: {wt_path} on {branch} from origin/main")
        print(f"accept:   {task['acceptance']}")
        print("argv:     " + " ".join(shlex.quote(a) for a in argv))
        return {**event, "outcome": "dry-run"}
    claim(paths, task)
    reason, wt, result, accept, pr_url = "", None, None, None, ""
    try:
        wt, branch = make_worktree(repo, number, slug)
        cp, timed_out = run_claude(argv, wt, args.timeout)
        if timed_out:
            reason = f"claude timed out after {args.timeout}s"
        else:
            result = parse_claude_result(cp.stdout or "")
            event.update(turns=result["turns"], tokens=result["tokens"], usd=result["usd"])
            if result["subtype"] in BUDGET_SUBTYPES:
                reason = f"budget hit: {result['subtype']}"
            elif cp.returncode != 0 or result["is_error"]:
                reason = (f"claude error: exit {cp.returncode}, subtype {result['subtype'] or '-'}"
                          + ("" if result["parsed"] else
                             f", unparsable output: {_one_line(cp.stderr or cp.stdout, 150)}"))
        accept = run_acceptance(task["acceptance"], wt)
        if not reason and accept["exit"] != 0:
            reason = f"acceptance red (exit {accept['exit']})"
        if not reason:
            body = (f"Task: {task['id']}\n\nAcceptance (run by the queue runner): "
                    f"`{task['acceptance']}` exit 0\n```\n{accept['tail']}\n```\n\n"
                    f"Cost: ${result['usd']:.2f}, {result['turns']} turns\n\n"
                    f"Orchestrator result:\n\n{result['result']}\n")
            pr_url, err = create_pr(wt, branch, f"{task['project']}: {slug.replace('-', ' ')}", body)
            if not pr_url:
                reason = err
    except QueueError as exc:
        reason = str(exc)
    except Exception as exc:  # after the claim every failure ends as blocked, never a stuck task
        reason = f"{exc.__class__.__name__}: {_one_line(exc, 200)}"
    today = date.today().isoformat()
    cost = f"${event['usd']:.2f}, {event['turns']} turns"
    if pr_url:
        set_status(task["path"], "review")
        append_log(task["path"], f"{today} queue-runner: PR {pr_url} ({cost}); acceptance green")
        event.update(outcome="pr", pr=pr_url)
    else:
        set_status(task["path"], "blocked")
        evidence = [f"branch {branch}" + (" (WIP commits kept)" if wt else ""), cost]
        if result and result["subtype"]:
            evidence.append(f"subtype {result['subtype']}")
        if accept:
            evidence.append(f"acceptance exit {accept['exit']}: {_one_line(accept['tail'])}")
        append_log(task["path"], f"{today} queue-runner: blocked, {reason}. " + "; ".join(evidence))
        event.update(outcome="blocked", reason=reason)
    err = commit_data(paths, task, f"chore(queue): {event['outcome']} {number}")
    if err:
        print(f"queue-runner: data repo: {err}", file=sys.stderr)
    print(f"{event['outcome']}: {task['id']}" + (f" {pr_url}" if pr_url else f" ({reason})"))
    return _finish(paths, event, started)


# --- schedule ----------------------------------------------------------------

def schedule_text(paths: g.Paths, at: str) -> str:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", at)
    if not m:
        raise QueueError(f"--time must be HH:MM, got {at!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    script = (paths.engine / "tools" / "queue_runner.py").as_posix()
    python, data = sys.executable, str(paths.data)
    win = (f'schtasks /Create /SC DAILY /ST {hour:02d}:{minute:02d} /TN "vault-queue-runner" /F '
           f'/TR "cmd /c set \\"VAULT_DATA={data}\\"&& \\"{python}\\" -I \\"{script}\\" run"')
    posix = (f"{minute} {hour} * * * VAULT_DATA={shlex.quote(data)} {shlex.quote(python)} -I "
             f"{shlex.quote(script)} run")
    return ("# Windows (Task Scheduler); prints only, run it yourself:\n" + win +
            "\n\n# POSIX (add with `crontab -e`):\n" + posix + "\n\n"
            "# The permission mode is passed by flag on every run, so it cannot fall back to Manual.")


# --- CLI ---------------------------------------------------------------------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", nargs="?", choices=["run"], default="run",
                   help="run the queue once (default)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the pick, plan and argv; change nothing")
    p.add_argument("--print-schedule", action="store_true",
                   help="print a schtasks command and a cron line (nightly); installs nothing")
    p.add_argument("--time", default=DEFAULT_TIME,
                   help=f"HH:MM for --print-schedule (default {DEFAULT_TIME})")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                   help=f"seconds for the claude run (default {DEFAULT_TIMEOUT})")
    p.add_argument("--claude", default=os.environ.get("VAULT_CLAUDE_BIN"),
                   help="claude executable (default: found on PATH; env VAULT_CLAUDE_BIN)")


def cmd_queue_runner(args) -> None:
    try:
        if args.print_schedule:
            print(schedule_text(g.default_paths(), args.time))
            return
        execute(args)
    except QueueError as exc:
        print(f"queue-runner: {exc}", file=sys.stderr)
        sys.exit(2)


def register(sub) -> None:
    p = sub.add_parser("queue-runner", help="nightly runner: oldest ready task -> claude -p -> PR "
                                            "(one task per run; needs a queue block in machine.json)")
    add_arguments(p)
    p.set_defaults(func=cmd_queue_runner)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="queue_runner.py", description=__doc__.split("\n")[0])
    add_arguments(parser)
    cmd_queue_runner(parser.parse_args(argv))


if __name__ == "__main__":
    main()
