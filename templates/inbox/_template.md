---
type: task
project: <project>
status: open
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
The one-line command below decides pass or fail (several commands: join with `&&`); a task cannot be `ready` without it. Non-code tasks write `human` in the block.
```bash
python -I -m unittest discover -s tests -q
```

## Screenshots
Optional: only for UI/visual design tasks; delete it otherwise. One command line that writes PNG files to a folder (the project's own tool). `worker-design` runs it before reporting and `worker-design-verify` reads the PNGs; without it the verifier answers FAIL (missing input).
```bash
<command that writes PNG files> --out <folder>
```

## Human gate
Spec approval at the start and PR review at the end. List anything else that needs the human here (default: nothing).

## Delivery
Branch + PR with the acceptance output, verifier report and a decisions line; or note path for non-code work.

## Open questions
Design decisions the human must take. Anything not listed here is the agent's call and goes into the report's decisions line.

## Constraints
Areas not to touch, standards to follow, deadline.

## Log
<!-- Filled in by the agent doing the task: YYYY-MM-DD, what was done, PR link, test result, issues. -->
