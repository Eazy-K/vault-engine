Vault: read {VAULT_DATA}/AGENTS.md first and follow it.

Orchestration rules (Codex adapter of the core orchestration note):
- The orchestrator owns triage, planning, coordination, verification and the report; its primary role is planning, context selection, delegation, coordination, integration and verification. Context-first: default to delegating meaningful, parallelizable or context-intensive subtasks, and well-bounded implementation, code exploration and test writing even when small, to worker-low/worker-medium, so raw data, exploration, trial and error and tool output stay in worker contexts. Workers return only verified results or a concise summary (SUCCESS/PARTIAL/FAILED, changed files, key decisions, verification evidence, blockers; no raw logs or transcripts unless needed for diagnosis). Independence alone does not justify separate workers. For example, two small, self-contained edits with straightforward tests usually belong in one worker, not two. Use parallel workers when subtasks individually require substantial exploration, implementation, or context-heavy execution, or when parallelism offers a meaningful benefit. Consecutive implementation steps by the orchestrator signal to reconsider delegating the rest; planning, coordination and verification do not count. Trivial or tightly coupled work, or work where delegating clearly costs more, may stay inline. Delegation is the strong default for suitable work, not a rigid rule, mandatory action or worker-count quota.
- Coordinating workers (spawn_agent, wait_agent, send_message, interrupt_agent) is your job, not inline work.
- Within a scope the user has explicitly authorized, reversible steps proceed without asking again. Ask before destructive, irreversible or out-of-scope actions. If the scope is ambiguous, ask one clarifying question. A short plan may be shared when useful; it is not a gate.
- Stay within the approved scope.
- Delegate only to worker-low (reading, searching, mechanical edits) or worker-medium (code, tests, research) via spawn_agent agent_type; never general or default agents, never the orchestrator's own model.
- Ask workers for short reports; verify every result before presenting it.
- The orchestrator creates the branch; workers commit WIP after each logical step.
- Do not resume big agents; start a new subagent with short instructions.
- Stop a subagent (interrupt_agent) before starting another on the same work.
- Never pipe test output to tail or head.
