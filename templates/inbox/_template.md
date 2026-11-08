---
type: task
project: <project>
status: open
budget: M
created: YYYY-MM-DD
keywords: [keywords]
links:
  - "[[<project>-overview]]"
---

# NNNN — Task title

## Goal
What needs to be done, and why?

## Scope
- Include:
- Exclude:
- Files not to touch:

## Files
<!-- Filled from Explore output. One bullet per file: path, symbol(s), line range. Then a "Do not touch" sub-list. Optional; worker prompts carry this section. -->
-
- Do not touch:
  -

## Acceptance criteria
- [ ]

## Acceptance
The command below decides pass or fail; a task cannot be `ready` without it. Non-code tasks write `human` in the block.
```bash
python -I -m unittest discover -s tests -q
```

## Human gate
Spec approval at the start and PR review at the end. List anything else that needs the human here (default: nothing).

## Budget
Class in frontmatter `budget:` (S / M / L); the engine maps the class to turn and token caps.

## Delivery
Branch + PR with the acceptance output, verifier report and a decisions line; or note path for non-code work.

## Open questions
Design decisions the human must take. Anything not listed here is the agent's call and goes into the report's decisions line.

## Constraints
Areas not to touch, standards to follow, deadline.

## Log
<!-- Filled in by the agent doing the task: YYYY-MM-DD, what was done, PR link, test result, issues. -->
