---
keywords: [inbox, görev, görevler, task, tasks, kuyruk, queue, iş ver, delegate, iş ajanı]
links:
  - "[[data-policy]]"
weights:
  data-policy: 0.6
---

# Task Queue

A task written in one session or on one device (e.g. a phone) gets done by an agent in another session.

## Writing a task
- File: `inbox/<project>/NNNN-<short-name>.md`. The number increases within the project.
- Template: `inbox/_template.md`. The `type: task`, `project`, and `status` frontmatter fields are required.
- Besides Goal and Scope (Include / Exclude / Files not to touch), the template has `## Acceptance` (exactly one line, join several commands with `&&`; its exit code decides pass or fail; `human` for non-code work), `## Human gate`, `## Delivery` and `## Open questions`.
- `## Files` (optional, between Scope and Acceptance) is where Explore's file list goes: one bullet per file with path, symbol(s) and line range, plus a "Do not touch" sub-list. Worker prompts (`codex-worker`) carry it as a separate block before the task body.
- No personal data in a task (see [[data-policy]]).

## The agent doing the task
1. `git pull --rebase --autostash`, then `python "$VAULT_ENGINE/tools/graph.py" tasks --status open`.
2. Set the task to `in-progress`, commit and push it. That way other machines see the task has been picked up. When an orchestrator hands the task to a worker, it gives the worker only the task path.
3. Do the work. Follow the project repo's own rules: branch, PR, tests.
4. Fill in the task's `## Log` section (date, what was done, PR link, test result, issues). Set the status to `done` or `blocked`.
5. Update the project's status note, then commit and push.

## Statuses
`open` → `ready` → `in-progress` → `review` → `done` / `blocked`.
- `open`: written, spec may be incomplete.
- `ready`: spec complete (acceptance command present; `lint` enforces).
- `in-progress`: an agent is working on it.
- `review`: PR open, waiting for the human.
- `done` / `blocked`: finished or stuck.

Tasks marked `done` remain as history, but are no longer loaded into `context` output.
