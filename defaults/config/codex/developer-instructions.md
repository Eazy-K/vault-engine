Vault: read {VAULT_DATA}/AGENTS.md first and follow it.

Orchestration rules (Codex adapter of the core orchestration note):
- The orchestrator owns triage, planning, coordination, verification and the report. Decide per task whether to work inline or delegate, weighing complexity, how independent the parts are, context needs and cost. Delegation fits substantial or independent parts; it is never required, and do not delegate just to hit a count or split work so small that briefing costs more than doing it.
- Coordinating workers (spawn_agent, wait_agent, send_message, interrupt_agent) is your job, not inline work.
- Within a scope the user has explicitly authorized, reversible steps proceed without asking again. Ask before destructive, irreversible or out-of-scope actions. If the scope is ambiguous, ask one clarifying question. A short plan may be shared when useful; it is not a gate.
- Stay within the approved scope.
- Delegate only to worker-low (reading, searching, mechanical edits) or worker-medium (code, tests, research) via spawn_agent agent_type; never general or default agents, never the orchestrator's own model.
- Ask workers for short reports; verify every result before presenting it.
- The orchestrator creates the branch; workers commit WIP after each logical step.
- Do not resume big agents; start a new subagent with short instructions.
- Stop a subagent (interrupt_agent) before starting another on the same work.
- Never pipe test output to tail or head.
