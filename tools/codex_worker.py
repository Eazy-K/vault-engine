"""`codex-worker`: run one task through Codex (`codex exec`) inside an assigned git worktree.

Run directly by the orchestrator (codex-worker skill). Builds a prompt (fixed worker rules + the task body +
extra instructions), runs `codex exec -s workspace-write --json` with the prompt on
stdin, then reports from git and from the JSONL events (not from Codex's own claims):
changed files, token usage, acceptance result. Pure stdlib; never uses
`danger-full-access`."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import graph as g

PROFILES = ("worker-low", "worker-medium")
DEFAULT_PROFILE = "worker-medium"
PROFILE_DIR = Path(__file__).resolve().parent / "codex-agents"
DEFAULT_TIMEOUT = 1800
DEFAULT_BASE = "origin/main"
CONFIG_TRIMS = ("ignore-user-config", "blank-instructions", "none")
# ~/.codex/config.toml injects the vault AGENTS.md through developer_instructions on every
# run. Every run passes the profile's own developer_instructions, which replaces the
# injected text. `ignore-user-config` additionally drops the user config: cheapest, but it
# also drops the `[windows] sandbox` setting and leaves the workspace-write sandbox unable
# to write on Windows. `blank-instructions` (the default) and `none` add no trim flag now
# that the profile text is always passed; both names are kept for compatibility (measured
# numbers in docs/agent-setup.md).
DEFAULT_CONFIG_TRIM = "blank-instructions"
SECTIONS = ("Changed files", "Decisions", "Evidence", "Blockers")

RULES = """\
You are a worker running one task inside a git worktree. Rules:
- Work only inside {worktree}. Never write outside it.
- The branch is {branch}. Do not create or switch branches. {commit_rule}
- Never push, merge, rebase or delete branches.
- Run the acceptance command before finishing and quote its exit code and summary line.{acceptance}
- End your final message with exactly this block (one section each, dash bullets):
STATUS: SUCCESS|PARTIAL|FAILED
Changed files:
Decisions:
Evidence:
Blockers:
"""


class WorkerError(Exception):
    """Argument / worktree problem (exit code 2)."""


def _git(wt: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _real(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def check_worktree(wt: Path, allow_main: bool) -> str:
    """Return the current branch; raise WorkerError unless `wt` is a linked worktree on a non-main branch."""
    if not wt.is_dir():
        raise WorkerError(f"worktree not found: {wt}")
    if _git(wt, "rev-parse", "--show-toplevel").returncode != 0:
        raise WorkerError(f"not a git repository: {wt}")
    git_dir = _git(wt, "rev-parse", "--absolute-git-dir").stdout.strip()
    common = _git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    linked = (wt / ".git").is_file() or (bool(common) and _real(git_dir) != _real(common))
    if not linked:
        raise WorkerError(f"{wt} is a main checkout, not a linked git worktree (refusing)")
    branch = _git(wt, "branch", "--show-current").stdout.strip()
    if not branch:
        raise WorkerError("detached HEAD in the worktree")
    if branch in ("main", "master") and not allow_main:
        raise WorkerError(f"worktree is on {branch}; pass --allow-main to override")
    return branch


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise WorkerError(f"cannot read {path}: {exc}")


def load_task(ref: str) -> tuple[str, str]:
    """(task id, task body) from a task file path or a vault note id."""
    path = Path(ref)
    if path.is_file():
        _, body = g.parse_frontmatter(_read(ref))
        return path.stem, body
    try:
        graph = g.Graph()
    except SystemExit as exc:
        raise WorkerError(f"cannot open the vault graph: {exc}")
    nid, error = graph.resolve(ref)
    if error:
        raise WorkerError(f"[[{ref}]] {error}")
    return nid, graph.notes[nid].body


NO_COMMIT = "Do not commit; leave your changes in the working tree, the orchestrator's wrapper commits them."
WRAPPER_COMMITS = "Do not commit; the wrapper commits your changes after the run."


def _parse_profile(text: str) -> dict:
    try:
        import tomllib
    except ImportError:  # Python 3.10: minimal parser for the keys the profiles use
        tomllib = None
    if tomllib:
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(str(exc))
    out: dict = {}
    for m in re.finditer(r'^(model|model_reasoning_effort)\s*=\s*"([^"\n]*)"\s*$', text, re.M):
        out[m.group(1)] = m.group(2)
    m = re.search(r'^developer_instructions\s*=\s*"""(.*?)"""', text, re.M | re.S)
    if m:
        body = m.group(1)
        out["developer_instructions"] = body[1:] if body.startswith("\n") else body
    return out


def load_profile(name: str) -> dict:
    """model, effort and developer_instructions from tools/codex-agents/<name>.toml."""
    path = PROFILE_DIR / f"{name}.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkerError(f"cannot read profile {name}: {exc}")
    try:
        data = _parse_profile(text)
    except ValueError as exc:
        raise WorkerError(f"invalid profile {name}: {exc}")
    return {"name": name, "model": data.get("model"), "effort": data.get("model_reasoning_effort"),
            "instructions": data.get("developer_instructions", "")}


def build_prompt(worktree: str, branch: str, body: str, acceptance: str | None,
                 instructions: str = "", commit: bool = False) -> str:
    acc = f"\n  Acceptance command: {acceptance}" if acceptance and acceptance != "human" else ""
    parts = [RULES.format(worktree=worktree, branch=branch, acceptance=acc,
                                commit_rule=WRAPPER_COMMITS if commit else NO_COMMIT).rstrip()]
    files = g.task_files_section(body)
    if files:
        parts.append("## Files to work on\n\n" + files.strip())
    parts.append("## Task\n\n" + body.strip())
    if instructions.strip():
        parts.append("## Extra instructions\n\n" + instructions.strip())
    return "\n\n".join(parts) + "\n"


def trim_flags(trim: str) -> list[str]:
    return ["--ignore-user-config"] if trim == "ignore-user-config" else []


def codex_argv(codex: str, worktree: str, last: str, trim: str, model: str, effort: str,
               instructions: str = "") -> list[str]:
    exe = [sys.executable, codex] if codex.lower().endswith(".py") else [shutil.which(codex) or codex]
    return [*exe, "exec", "-C", worktree, "-s", "workspace-write", "--json", "-o", last,
            *trim_flags(trim), "-c", "developer_instructions=" + json.dumps(instructions),
            "-m", model, "-c", f'model_reasoning_effort="{effort}"', "-"]


def parse_events(text: str) -> dict:
    """thread id, turn count and summed token usage from codex JSONL events."""
    out = {"thread_id": None, "turns": 0, "input": 0, "cached": 0, "output": 0, "reasoning": 0}
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "thread.started" and out["thread_id"] is None:
            out["thread_id"] = ev.get("thread_id")
        elif ev.get("type") == "turn.completed":
            out["turns"] += 1
            usage = ev.get("usage") or {}
            for key, field in (("input", "input_tokens"), ("cached", "cached_input_tokens"),
                               ("output", "output_tokens"), ("reasoning", "reasoning_output_tokens")):
                val = usage.get(field)
                if isinstance(val, int):
                    out[key] += val
    return out


def parse_status(text: str) -> tuple[str | None, dict[str, list[str]]]:
    """The last STATUS block of Codex's final message: (status or None, sections)."""
    matches = list(re.finditer(r"^\W*STATUS:\s*(SUCCESS|PARTIAL|FAILED)\b", text, re.M | re.I))
    sections: dict[str, list[str]] = {s: [] for s in SECTIONS}
    if not matches:
        return None, sections
    m = matches[-1]
    current = None
    for line in text[m.end():].splitlines():
        head = re.match(r"^\W*(Changed files|Decisions|Evidence|Blockers)\s*:\s*(.*)$", line, re.I)
        if head:
            current = next(s for s in SECTIONS if s.lower() == head.group(1).lower())
            rest = head.group(2).strip()
            if rest:
                sections[current].append(rest.lstrip("-* ").strip())
        elif current and line.strip():
            sections[current].append(line.strip().lstrip("-* ").strip())
    return m.group(1).upper(), sections


def run_acceptance(command: str | None, wt: Path) -> dict:
    if not command or command == "human":
        return {"command": command, "ran": False, "exit": None, "summary": "none (human or no command)"}
    proc = subprocess.run(command, shell=True, cwd=str(wt), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    lines = [ln.strip() for ln in (proc.stdout + "\n" + proc.stderr).splitlines() if ln.strip()]
    return {"command": command, "ran": True, "exit": proc.returncode, "summary": lines[-1] if lines else ""}


def git_info(wt: Path, base: str) -> dict:
    def lines(cp):
        return [ln for ln in cp.stdout.splitlines() if ln.strip()]
    return {"status": lines(_git(wt, "status", "--short")),
            "log": lines(_git(wt, "log", "--oneline", f"{base}..HEAD")),
            "diff_stat": lines(_git(wt, "diff", "--stat", f"{base}...HEAD")),
            "files": lines(_git(wt, "diff", "--name-only", f"{base}...HEAD"))}


def wip_commit(wt: Path, message: str) -> str:
    """Commit everything in the worktree; returns a short hash, 'none (clean)' or 'failed: <reason>'."""
    if not _git(wt, "status", "--porcelain").stdout.strip():
        return "none (clean)"
    add = _git(wt, "add", "-A")
    if add.returncode == 0:
        com = _git(wt, "commit", "-q", "-m", message)
        if com.returncode == 0:
            return _git(wt, "rev-parse", "--short", "HEAD").stdout.strip() or "unknown"
        add = com
    err = (add.stderr or add.stdout).strip().splitlines()
    return "failed: " + (err[0] if err else f"git exit {add.returncode}")


def _kill(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    proc.kill()


def run_codex(argv: list[str], prompt: str, out: Path, timeout: int) -> tuple[int | None, bool]:
    """Run codex; returns (exit code, timed_out). Raises FileNotFoundError if codex is missing."""
    with open(out / "events.jsonl", "wb") as so, open(out / "stderr.txt", "wb") as se:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=so, stderr=se)
        try:
            proc.communicate(prompt.encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            proc.communicate()
            return None, True
    return proc.returncode, False


def render(r: dict) -> str:
    acc, m, sec = r["acceptance"], r["metrics"], r["codex"]

    def bullets(items):
        return "\n".join(f"- {i}" for i in items) if items else "- none"
    changed = r["git"]["files"] or r["git"]["status"]
    acc_line = (f"- `{acc['command']}`: exit {acc['exit']}, {acc['summary']}" if acc["ran"]
                else f"- {acc['summary']}")
    return "\n".join([
        f"# Codex worker report: {r['task']}", "", f"Status: {r['status']}",
        f"Profile: {r['profile']} ({r['model']}, {r['effort']})", "",
        "## Changed files (from git)", bullets(changed), "",
        "Commits:", bullets(r["git"]["log"]), "",
        "## Codex decisions", bullets(sec["Decisions"]), "",
        "## Codex evidence", bullets(sec["Evidence"]), "",
        "## Codex blockers", bullets(r["blockers"]), "",
        "## Acceptance", acc_line, "",
        *([f"WIP commit: {r['wip_commit']}", ""] if r.get("wip_commit") else []),
        "## Metrics",
        f"- duration: {m['duration_s']} s, turns: {m['turns']}",
        (f"- tokens: UNKNOWN (events stream empty or without turn.completed)" if m.get("events_missing")
         else f"- tokens: in {m['input']}, cached {m['cached']}, out {m['output']}, reasoning {m['reasoning']}"),
        "", f"Codex thread id: {r['thread_id']}", f"Out dir: {r['out']}", ""])


def log_codex_exec(wt: Path, events: dict, codex_rc, profile: str | None = None) -> None:
    """Append one `codex_exec` event to the vault's usage.log so Codex token totals
    survive the handback (the relay's own transcript has no Codex usage).

    Written directly, not via g.log_usage, so agent is "claude" (the caller) and
    session is omitted unless the environment names one. exit_code is the codex
    process's own return code; None when the run timed out (the wrapper killed it).
    Does nothing if the vault data dir is unknown or missing. Fails open: any error,
    including SystemExit from the data-dir lookup, is swallowed."""
    try:
        paths = g.default_paths()
        if not paths.data.is_dir():
            return
        event = {"ts": datetime.now().isoformat(timespec="seconds"), "event": "codex_exec",
                 "agent": "claude", "agent_type": "codex-worker"}
        agent, session = g.detect_agent()
        if agent != "unknown" and session:
            event["session"] = session
        missing = events["turns"] == 0  # no turn.completed: zeros would be indistinguishable from real
        tok = (lambda v: None if missing else v)
        event.update({"machine": g.machine_name(paths), "cwd": str(wt),
                      "input_tokens": tok(events["input"]), "cached_input_tokens": tok(events["cached"]),
                      "output_tokens": tok(events["output"]),
                      "reasoning_output_tokens": tok(events["reasoning"]),
                      "turns": events["turns"], "exit_code": codex_rc, "profile": profile})
        if missing:
            event["events_missing"] = True
        paths.usage_log.parent.mkdir(exist_ok=True)
        with paths.usage_log.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except (Exception, SystemExit):
        pass


def _run(args) -> dict | None:
    if bool(args.task) == bool(args.prompt_file):
        raise WorkerError("give exactly one of --task or --prompt-file")
    wt = Path(args.worktree).expanduser().resolve()
    branch = check_worktree(wt, args.allow_main)
    if args.task:
        task_id, body = load_task(args.task)
        if g.task_acceptance_multiline(body) and not args.acceptance:
            raise WorkerError(f"task {task_id}: acceptance block has more than one line; "
                              f"{g.MULTILINE_ACCEPTANCE_HINT}")
        acceptance = g.task_acceptance_command(body)
    else:
        task_id, body, acceptance = Path(args.prompt_file).stem, _read(args.prompt_file), None
    if args.acceptance:
        acceptance = args.acceptance
    extra = _read(args.instructions) if args.instructions else ""
    prof = load_profile(args.profile)
    model = args.model or prof["model"]
    effort = args.effort or prof["effort"]
    if not model or not effort:
        raise WorkerError(f"profile {args.profile} has no model / model_reasoning_effort; pass --model / --effort")
    prompt = build_prompt(str(wt), branch, body, acceptance, extra, bool(args.commit))
    out = Path(args.out) if args.out else (Path(tempfile.gettempdir()) / "vault-codex-worker" /
                                           f"{task_id}-{datetime.now():%Y%m%d-%H%M%S}")
    last = out / "last.md"
    argv = codex_argv(args.codex, str(wt), str(last), args.config_trim, model, effort,
                      prof["instructions"])
    if args.dry_run:
        print(f"Profile: {prof['name']}\nModel: {model}\nEffort: {effort}")
        print("Developer instructions:\n" + prof["instructions"] + "\n")
        print("Command:\n" + " ".join(f'"{a}"' if " " in a else a for a in argv) + " < prompt.md\n")
        print("Prompt:\n" + prompt)
        return None
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompt.md").write_text(prompt, encoding="utf-8", newline="\n")
    start = time.time()
    blockers: list[str] = []
    exit_code = 0
    try:
        rc, timed_out = run_codex(argv, prompt, out, args.timeout)
    except FileNotFoundError:
        print(f"codex-worker: codex executable not found: {args.codex}", file=sys.stderr)
        sys.exit(3)
    duration = round(time.time() - start, 1)
    events = parse_events((out / "events.jsonl").read_text(encoding="utf-8", errors="replace"))
    log_codex_exec(wt, events, None if timed_out else rc, args.profile)
    events_missing = events["turns"] == 0
    if events_missing:
        warn = (f"no turn.completed in {out / 'events.jsonl'}: token usage unknown (not zero); "
                "Codex may have redirected its JSONL stdout elsewhere")
        print(f"codex-worker: WARNING: {warn}", file=sys.stderr)
        blockers.append(warn)
    final = last.read_text(encoding="utf-8", errors="replace") if last.is_file() else ""
    status, sections = parse_status(final)
    if timed_out:
        status, exit_code = "FAILED", 3
        blockers.append("timeout")
    elif status is None:
        status = "PARTIAL"
        blockers.append("no STATUS block")
    if rc not in (0, None):
        blockers.append(f"codex exited with code {rc}")
    blockers.extend(b for b in sections["Blockers"] if b.lower() != "none")
    wip = None
    if args.commit:
        wip = wip_commit(wt, args.commit_message or f"wip(codex): {task_id if args.task else 'prompt'}")
        if wip.startswith("failed"):
            blockers.append(f"WIP commit {wip}")
    git = git_info(wt, args.base)  # after the commit, so changed files / log include it
    report = {"task": task_id, "status": status, "exit": exit_code, "worktree": str(wt),
              "branch": branch, "git": git, "codex": sections,
              "blockers": blockers, "wip_commit": wip, "acceptance": run_acceptance(acceptance, wt),
              "thread_id": events["thread_id"],
              "metrics": {"duration_s": duration, "turns": events["turns"], "input": events["input"],
                          "cached": events["cached"], "output": events["output"],
                          "reasoning": events["reasoning"],
                          "events_missing": events_missing},
              "profile": args.profile, "model": model, "effort": effort,
              "config_trim": args.config_trim, "out": str(out)}
    (out / "report.md").write_text(render(report), encoding="utf-8", newline="\n")
    return report


def cmd_codex_worker(args) -> None:
    try:
        report = _run(args)
    except WorkerError as exc:
        print(f"codex-worker: {exc}", file=sys.stderr)
        sys.exit(2)
    if report is None:
        return
    print(json.dumps(report, indent=2) if args.json else render(report))
    sys.exit(report["exit"])


def register(sub) -> None:
    p = sub.add_parser("codex-worker", help="run one task through Codex (codex exec) inside a git "
                                            "worktree and report from git (codex-worker skill)")
    p.add_argument("--task", help="task note id or path to a task file")
    p.add_argument("--prompt-file", help="file with a raw prompt (smoke tests)")
    p.add_argument("--worktree", required=True, help="the linked git worktree Codex works in")
    p.add_argument("--out", help="output dir (default: <tmp>/vault-codex-worker/<task>-<time>)")
    p.add_argument("--profile", choices=PROFILES, default=DEFAULT_PROFILE,
                   help=f"tools/codex-agents/<profile>.toml: model, effort and rules (default {DEFAULT_PROFILE})")
    p.add_argument("--model", help="Codex model (default: the profile's model)")
    p.add_argument("--effort", help="reasoning effort (default: the profile's model_reasoning_effort)")
    p.add_argument("--instructions", help="file with extra text appended to the prompt")
    p.add_argument("--acceptance", help="acceptance command (overrides the task file's)")
    p.add_argument("--base", default=DEFAULT_BASE, help=f"ref for the diff summary (default {DEFAULT_BASE})")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds (default 1800)")
    p.add_argument("--config-trim", choices=CONFIG_TRIMS, default=DEFAULT_CONFIG_TRIM,
                   help="the profile's developer_instructions always replace the injected ones; "
                        "ignore-user-config also drops ~/.codex/config.toml (breaks the Windows "
                        "sandbox); blank-instructions and none add no flag")
    p.add_argument("--commit", action="store_true",
                   help="after the run, commit uncommitted worktree changes (git add -A) as a WIP commit")
    p.add_argument("--commit-message", help="WIP commit message (default: wip(codex): <task id or 'prompt'>)")
    p.add_argument("--allow-main", action="store_true", help="allow a worktree on main/master")
    p.add_argument("--dry-run", action="store_true", help="print the command and prompt, run nothing")
    p.add_argument("--json", action="store_true", help="machine-readable report")
    p.add_argument("--codex", default=os.environ.get("VAULT_CODEX_BIN", "codex"),
                   help="codex executable (a .py file runs with the current Python; env VAULT_CODEX_BIN)")
    p.set_defaults(func=cmd_codex_worker)
