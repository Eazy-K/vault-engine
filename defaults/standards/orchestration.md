---
core: true
keywords: [subagent, orchestration, orchestrator, delegate, worker-low, worker-medium, plan, approval, scope]
links:
  - "[[orchestration-details]]"
weights:
  orchestration-details: 0.8
---

# Orchestration (core rules)
- Delegate by default: hand every request to a subagent; the orchestrator only splits, verifies and summarizes.
- Small-task exception: a single short tool call (e.g. `git status`) stays inline.
- Count the whole request, not each step: a multi-step plan is delegated before the first tool call.
- For multi-step work, present a short plan and wait for explicit user approval; if the scope is ambiguous, ask one clarifying question.
- Stay within the approved scope.
- Use only worker-low (reading, searching, mechanical edits) or worker-medium (code, tests, research); never general or default agents, never the orchestrator's own model.
- Ask for short reports.
- The orchestrator verifies every result before presenting it.
- The orchestrator creates the branch; workers commit WIP after each logical step.
- Do not resume big agents; start a new subagent with short instructions.
- Stop a subagent before starting another on the same work.
- Never pipe test output to tail or head.
- Per-tool mapping (Claude Code, Codex), rationale: [[orchestration-details]].
