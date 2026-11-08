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
- Within a scope the user has explicitly authorized, reversible steps proceed without asking again; stay within the approved scope. Ask before destructive, irreversible or out-of-scope actions. If the scope is ambiguous, ask one clarifying question. A short plan may be shared when useful; it is not a gate.
- Two gates: the human approves the spec once at the start and the PR at the end; nothing stops in between. A task file with an acceptance command counts as explicit authorization. Design decisions the spec does not list as open questions are made by the agent and reported in the decisions line.
- Use worker-low (reading, edits, lookup) or worker-medium (code, tests, research, judgment: audits, comparisons, design); worker-verify (verification only); read-only Explore, Plan, claude-code-guide ok; never general or default agents. Ask workers for short reports.
- Verification goes to worker-verify (clean context; input: task file, diff, acceptance output; output: PASS or FAIL plus items); the writer never verifies itself. FAIL items go to a new writing worker; after two failed rounds the task becomes `blocked`. The orchestrator checks only the whole and opens the PR with the acceptance output, the verifier report and a decisions line.
- Progress goes to the task file's Log section and the PR body; the project status note is updated only when the work is done.
- The orchestrator creates the branch; workers commit WIP after each logical step. Never pipe test output to tail or head.
- Do not resume big agents; start a new subagent with short instructions. Stop a subagent before starting another on the same work.
- Per-tool mapping (Claude Code, Codex), rationale: [[orchestration-details]].

## Context-first delegation
- The orchestrator manages implementation rather than doing it: planning, context selection, delegation, coordination, integration and verification. Default to delegating implementation, code exploration and test writing, including small bounded work (a function and its test, two small file changes). Raw tool output stays in workers.
- Workers return a concise structured summary: status (SUCCESS/PARTIAL/FAILED), changed files, key decisions, verification evidence, blockers; no raw logs.
- Inline implementation is the exception, limited to a truly trivial one-line or single-step change; small, related or tightly coupled work goes to one worker together. Consecutive implementation steps by the orchestrator signal to reconsider delegating the rest; planning, coordination and verification do not count.
- Delegation is the strong default for suitable work, not a rigid rule or worker-count quota.
- **Discovery and implementation run in separate agents.** When it is not yet known which files a task touches, a read-only `Explore` agent (no `model` given; the guard sets `haiku`) looks first and returns a list of `file:line` ranges, the ranges themselves where needed, and a do-not-touch list; never a prose summary. The writing worker opens only the files on that list and does not search on its own. When the files are known up front, the discovery step is skipped. A Codex task is never dispatched without a file list.
