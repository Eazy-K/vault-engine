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
- No personal data in a task (see [[data-policy]]).

## The agent doing the task
1. `git pull --rebase`, then `python tools/graph.py tasks --status open`.
2. Set the task's status to `in-progress`, commit and push it. That way other machines see the task has been picked up.
3. Do the work. Follow the project repo's own rules: branch, PR, tests.
4. Fill in the task's `## Log` section (date, what was done, PR link, test result, issues). Set the status to `done` or `blocked`.
5. Update the project's status note, then commit and push.

## Statuses
`open` → `in-progress` → `done` / `blocked`. Tasks marked `done` remain as history, but are no longer loaded into `context` output.
