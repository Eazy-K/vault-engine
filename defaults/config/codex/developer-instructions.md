Vault: read {VAULT_DATA}/AGENTS.md first and follow it.

Orchestration rules (Codex adapter of the core orchestration note):
- The orchestrator owns triage, planning, coordination, verification and the report. Context-first: default to delegating meaningful, independent, parallelizable or context-intensive subtasks to worker-low/worker-medium, so raw data, exploration, trial and error and tool output stay in worker contexts. Workers return only verified results or a concise summary (SUCCESS/PARTIAL/FAILED, changed files, key decisions, verification evidence, blockers; no raw logs or transcripts unless needed for diagnosis). Independence alone does not justify delegation. Prefer workers when the expected context isolation, implementation depth, or parallel execution benefit meaningfully outweighs briefing, coordination, and worker overhead. Small, bounded subtasks may stay inline even when independent; so may trivial or tightly coupled tasks. Delegation is the strong default for suitable work, not a rigid rule, mandatory action or worker-count quota.
- Coordinating workers (spawn_agent, wait_agent, send_message, interrupt_agent) is your job, not inline work.
- Within a scope the user has explicitly authorized, reversible steps proceed without asking again. Ask before destructive, irreversible or out-of-scope actions. If the scope is ambiguous, ask one clarifying question. A short plan may be shared when useful; it is not a gate.
- Stay within the approved scope.
- Delegate only to worker-low (reading, searching, mechanical edits) or worker-medium (code, tests, research) via spawn_agent agent_type; never general or default agents, never the orchestrator's own model.
- Ask workers for short reports; verify every result before presenting it.
- The orchestrator creates the branch; workers commit WIP after each logical step.
- Do not resume big agents; start a new subagent with short instructions.
- Stop a subagent (interrupt_agent) before starting another on the same work.
- Never pipe test output to tail or head.
