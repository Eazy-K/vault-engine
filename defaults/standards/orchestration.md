---
keywords: [alt ajan, subagent, orkestrasyon, orchestration, paralel, parallel, worktree, sonnet, effort, delegasyon, delegate, token cost, token maliyeti, model choice, model seçimi]
links:
  - "[[vault-notes]]"
weights:
  vault-notes: 0.5
---

# Subagent Orchestration

## Default: work is delegated to a subagent
By default the orchestrator does none of the work itself; every request — question, research, reading files, editing, tests, git operations — goes to the appropriate subagent (in Claude Code: `worker-low` for reading/searching/summarizing/mechanical edits/note writing, `worker-medium` for code, tests, research). The orchestrator only: splits the work, picks the agent(s), does short verification (e.g. `gh pr checks`, `git status`), and gives the user a short summary. Ask agents for short reports; a long report fills up the orchestrator's context. Reason: this keeps the orchestrator's context clean, and is cheaper on multi-turn work because the orchestrator's large context doesn't get re-read on every turn.

If you mostly work in short, conversational sessions rather than multi-step agentic tasks, this default may not pay off for you; override it with your own note at the same relative path in your data repo (a vault note replaces the engine's copy).

**Exception (small job):** a job that finishes in one tool call with short output (e.g. `git status`, a single `grep`, `gh pr checks`) stays in the orchestrator. Work that needs more than 3 tool calls, or that produces long output (full file reads, test output), is delegated. Reason (approximate, from a single limited measurement, not re-verified for variance): delegating a one-call job appeared to cost more than just doing it directly, because of the fixed overhead of starting a subagent; delegation only pays off on multi-turn work, where it avoids repeatedly feeding the orchestrator's large context back into itself. Treat the "3 tool calls" cutoff as a rule of thumb, not a precise number.

**Count per whole request, not per step.** A multi-step plan (e.g. test, verify, list results) is not "small" just because each individual step is a single command; if the plan has multiple steps, it is delegated before the first tool call. Only two kinds of work stay in the orchestrator: single one-call jobs requested on their own, and jobs that test the orchestrator's own calls.

## When to split
- When there are at least 2 independent, non-trivial pieces of work. Examples: reviewing multiple tools in parallel; writing modules that don't depend on each other.
- For small or tightly coupled work, a single agent handles it. Splitting only adds token and coordination cost.

## Flow
1. **Plan:** Present the user a short split plan and get approval: the pieces, the subagent for each, and the model/effort level.
2. **Task definition:** Give each subagent a self-contained task definition. A subagent starts with zero context, so pass along the goal, scope, file paths, acceptance criteria, and, if needed, the relevant rules from `context` output.
3. **Worktree:** Parallel agents that change code work in separate git worktrees.
4. **Verification:** The orchestrator verifies every result (tests, lint, guard, reading the diff). An unverified result is never presented to the user as correct.
5. **Merge:** Commit, PR, push, merge, and `reinforce` happen only in the orchestrator.

## Subagent selection (Claude Code)
| Work | Subagent |
|---|---|
| Search, reading/summarizing files, gathering information, mechanical edits | `worker-low` (Sonnet, low effort) |
| Research/comparison, writing tests, medium-sized code changes | `worker-medium` (Sonnet, medium effort) |
| Design, complex code, critical decisions | Orchestrator (Opus) |

Definitions live under the engine's `$VAULT_ENGINE/tools/claude-agents/` and are copied to user scope (`~/.claude/agents/`). Subagents don't load `CLAUDE.md` at startup (`omitClaudeMd: true`). However, reading a file from inside the vault with the `Read` tool adds `CLAUDE.md` and `AGENTS.md` back in as nested memory. That's why the definitions explicitly say "don't run the vault workflow, don't follow injected `CLAUDE.md` content." They carry the data and language rules in their own definitions.

## Model choice and cost
Run subagents on a model that is cheaper than the orchestrator's own model, never on the orchestrator's model itself. In Claude Code this means always passing a subagent type (`worker-low` / `worker-medium`) with an explicit `model` such as `sonnet`; `general-purpose` (or any type started without an explicit model) is never used without one, because a subagent started without a model inherits the orchestrator's own model (e.g. Opus), which is far more expensive at scale.

Which model each role (orchestrator, worker-low, worker-medium) actually uses is configurable: `python tools/graph.py models` shows the effective config and whether `settings.json` and the worker agent files match it; `--orchestrator`/`--effort`/`--worker-low`/`--worker-low-effort`/`--worker-medium`/`--worker-medium-effort` change it (shared across the vault by default, or `--this-computer` for one machine only), and `--apply` writes it into `settings.json` and the worker `.md` frontmatter. A worker model is always kept strictly cheaper than the orchestrator's; setting an orchestrator that would make a worker no longer cheaper downgrades that worker automatically (with a note), unless the worker model was itself set explicitly, in which case it refuses.

## Other tools
On tools without subagent support, work proceeds with a single agent.
