---
keywords: [subagent, subagents, orchestration, parallel, worktree, sonnet, effort, delegation, delegate, token cost, model choice, claude code, codex, spawn_agent, hooks, agent-guard]
links:
  - "[[orchestration]]"
  - "[[vault-notes]]"
weights:
  orchestration: 0.8
  vault-notes: 0.5
---

# Orchestration details

The core rules are in [[orchestration]] (always loaded). This note maps them to each tool and explains the reasoning.

## Why
Delegating keeps the orchestrator's context clean and is cheaper on multi-turn work, because its large context is not re-read on every turn. For a single trivial call the subagent start-up overhead outweighs that (approximate, from one limited measurement), so only a truly trivial one-line or single-step change stays inline; small bounded implementation (a function and its test, two small file changes) still goes to a worker, bundled with related tasks. If you mostly work in short conversational sessions, override this with your own note at the same relative path in your data repo (a vault note replaces the engine's copy).

## When to split
- At least 2 independent, non-trivial pieces of work (e.g. reviewing several tools in parallel, writing independent modules).
- Small or tightly coupled work goes to one worker rather than several; splitting only adds token and coordination cost.

## Flow
1. **Plan:** a short plan (pieces, subagent, model/effort) may be shared when useful; it is not a gate: within an explicitly authorized scope reversible steps proceed without asking again; ask before destructive, irreversible or out-of-scope actions.
2. **Task definition:** a subagent starts with zero context: give goal, scope, file paths, acceptance criteria and relevant rules from `context`.
3. **Worktree:** parallel agents that change code use separate git worktrees.
4. **Verification:** tests, lint, guard, reading the diff. Nothing unverified is presented as correct.
5. **Merge:** PR, push, merge and `reinforce` happen only in the orchestrator.
6. Split feature work: one agent for code and tests, another for docs and PR. Keep jobs small (one PR, narrow scope) and name the function or line range when known.
7. The orchestrator watches CI itself (`gh pr checks <no> --watch`, short output); agents are not woken for it.

## In Claude Code
- Start a subagent with the `Agent` tool: `subagent_type` `worker-low` or `worker-medium` and either omit `model` or pass the worker's own configured model (default `sonnet`). `general-purpose` is not allowed.
- `isolation: worktree` only when the orchestrator has not already created the worktree; otherwise the agent opens its own and cannot write to the prompt's folder.
- Stop a subagent with `TaskStop`. An agent that returns at `maxTurns` ("partial") may keep running background work.
- Resume only if the completion notice's `subagent_tokens` (approximate last context size) is 50K or less; otherwise start a new agent with short instructions (PR number, failing test, file/line).
- The `agent-guard` hook denies disallowed `Agent` calls (enforced); `delegation-warn` and `context-warn` hooks remind the orchestrator. Adapter note: the Claude `delegation-warn` nudges after 3 inline tool calls (Claude-specific tuning).
- Definitions live in `$VAULT_ENGINE/tools/claude-agents/` and are copied to `~/.claude/agents/`. Subagents do not load `CLAUDE.md` at startup (`omitClaudeMd: true`), but reading vault files adds `CLAUDE.md`/`AGENTS.md` as nested memory, so the definitions say not to follow it.

## In Codex
- Start a subagent with `spawn_agent` and `agent_type: worker-low` or `worker-medium`; never `default`. The worker TOML profiles set model and effort (there is no model parameter).
- Stop a subagent with `interrupt_agent`; confirm it stopped before giving the same work to a new one. Completion notices give no history size, so do not resume; start a new agent.
- The `developer_instructions` managed block (installed by `user-config --install`) carries the core rules.
- Hooks: `context-warn` on every prompt; `delegation-warn` nudges once per turn at 8 inline calls (Codex adapter). Coordination calls (`collaborationspawn_agent`/`wait_agent`/`send_message`/`followup_task`/`interrupt_agent`, verified on Codex CLI 0.160.0) are not counted. Subagent calls are detected through the rollout `session_meta` and excluded.
- `agent-guard` (PR #117 matcher): a PreToolUse deny for spawns was enforced live on Codex CLI 0.160.0 (Windows, 2026-10-14); 0.159.2 did not dispatch it (openai/codex#49736); other versions are unverified. Re-run `codex-hooks --probe-deny` after Codex updates (or dismiss the doctor notice with `--ack-deny`).
- A read-only orchestrator does not work: workers inherit the parent's sandbox (the worker profile's `sandbox_mode` is ignored; tested on 0.159.2), and it would block the orchestrator's own commits.

## Models
`python "$VAULT_ENGINE/tools/graph.py" models` shows the effective role-to-model config and whether the tool settings match; `--orchestrator`, `--effort`, `--worker-low`, `--worker-low-effort`, `--worker-medium`, `--worker-medium-effort` change it (`--this-computer` for one machine), `--apply` writes it out. This command manages Claude worker models only; Codex worker model/effort live in `tools/codex-agents/worker-*.toml` (installed copies in the `agents` folder of the Codex home; customized copies are kept and reported by doctor).

## Other tools
Without subagent support, work proceeds with a single agent.
