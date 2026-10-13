---
core: true
keywords: [subagent, orchestration, orchestrator, delegate, worker-low, worker-medium, plan, approval, scope]
links:
  - "[[orchestration-details]]"
weights:
  orchestration-details: 0.8
---

# Orchestration (core rules)
- The orchestrator owns triage, planning, coordination, verification and the report. Do small, bounded work inline; delegate substantial or independent work (multi-file changes, research, test writing, parallel parts) to a worker. Do not delegate just to satisfy a count, and do not split work so small that briefing a worker costs more than doing it.
- Before multi-step work that changes files, present a short plan and wait for explicit user approval unless the user already approved this scope; if the scope is ambiguous, ask one clarifying question. Always ask before destructive, irreversible or out-of-scope actions.
- Stay within the approved scope.
- Use only worker-low (reading, searching, mechanical edits) or worker-medium (code, tests, research); never general or default agents, never the orchestrator's own model.
- Ask for short reports.
- The orchestrator verifies every result before presenting it.
- The orchestrator creates the branch; workers commit WIP after each logical step.
- Do not resume big agents; start a new subagent with short instructions.
- Stop a subagent before starting another on the same work.
- Never pipe test output to tail or head.
- Per-tool mapping (Claude Code, Codex), rationale: [[orchestration-details]].
