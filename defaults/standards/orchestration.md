---
core: true
keywords: [subagent, orchestration, orchestrator, delegate, worker-low, worker-medium, plan, approval, scope]
links:
  - "[[orchestration-details]]"
weights:
  orchestration-details: 0.8
---

# Orchestration (core rules)
- The orchestrator owns triage, planning, coordination, verification and the report.
- Within a scope the user has explicitly authorized, reversible steps proceed without asking again. Ask before destructive, irreversible or out-of-scope actions. If the scope is ambiguous, ask one clarifying question. A short plan may be shared when useful; it is not a gate.
- Stay within the approved scope.
- Use only worker-low (reading, searching, mechanical edits) or worker-medium (code, tests, research); never general or default agents, never the orchestrator's own model.
- Ask for short reports.
- The orchestrator verifies every result before presenting it.
- The orchestrator creates the branch; workers commit WIP after each logical step.
- Do not resume big agents; start a new subagent with short instructions.
- Stop a subagent before starting another on the same work.
- Never pipe test output to tail or head.
- Per-tool mapping (Claude Code, Codex), rationale: [[orchestration-details]].

## Context-first delegation
- Default to delegating meaningful, independent, parallelizable or context-intensive subtasks to workers. This protects the orchestrator's context for planning, coordination, integration and verification; raw data, code exploration, trial and error, intermediate tool output and implementation details stay in worker contexts.
- Workers return only verified results or a concise structured summary: status (SUCCESS/PARTIAL/FAILED), changed files, key decisions, verification evidence, blockers. No raw logs, full transcripts or intermediate history unless explicitly needed for diagnosis.
- Small, trivial or tightly coupled tasks, or ones where briefing costs more than the context saved, may be handled inline.
- Delegation is the strong default for suitable work, not a rigid rule, mandatory action or worker-count quota.
