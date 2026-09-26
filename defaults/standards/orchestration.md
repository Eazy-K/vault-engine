---
keywords: [alt ajan, subagent, orkestrasyon, orchestration, paralel, parallel, worktree, sonnet, effort, delegasyon, delegate]
links:
  - "[[vault-notes]]"
weights:
  vault-notes: 0.5
---

# Subagent Orchestration

For large jobs the main agent acts as orchestrator: splits the work, distributes it to subagents, verifies the results, and merges them.

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

## Other tools
On tools without subagent support, work proceeds with a single agent.
