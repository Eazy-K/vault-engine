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
- Use only worker-low (reading, searching, mechanical edits) or worker-medium (code, tests, research); never general or default agents, never the orchestrator's own model at the same or higher effort.
- Ask for short reports.
- The orchestrator verifies every result before presenting it.
- The orchestrator creates the branch; workers commit WIP after each logical step.
- Do not resume big agents; start a new subagent with short instructions.
- Stop a subagent before starting another on the same work.
- Never pipe test output to tail or head.
- Per-tool mapping (Claude Code, Codex), rationale: [[orchestration-details]].

## Context-first delegation
- The orchestrator manages implementation rather than doing it: planning, context selection, delegation, coordination, integration and verification. Default to delegating implementation, code exploration and test writing, including small bounded work (a function and its test, two small file changes). Raw tool output stays in workers.
- Workers return a concise structured summary: status (SUCCESS/PARTIAL/FAILED), changed files, key decisions, verification evidence, blockers; no raw logs unless needed for diagnosis.
- Inline implementation is the exception, limited to a truly trivial one-line or single-step change; small, related or tightly coupled work goes to one worker together. Consecutive implementation steps by the orchestrator signal to reconsider delegating the rest; planning, coordination and verification do not count.
- Delegation is the strong default for suitable work, not a rigid rule or worker-count quota.
