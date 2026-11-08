---
name: codex-worker
description: Hand one vault task to Codex through the codex-worker wrapper. Use on `/codex-worker <task>` and on requests like "Codex'e yaptır" or "have Codex do it".
---

Run the wrapper directly in the background (Bash `run_in_background`), with the task file and the worktree you created. No relay subagent.

```
python "$VAULT_ENGINE/tools/graph.py" codex-worker --task <id|path> --worktree <dir> --commit
```

Wait for it to finish, then read its report (status, changed files, evidence, blockers). Then run worker-verify on the diff and open the PR.

Full procedure (worktree and branch setup, `--profile`, `--prompt-file` only for smoke tests, verification): the "Codex'e yaptır" bullet in the `orchestration-details` standard note.
