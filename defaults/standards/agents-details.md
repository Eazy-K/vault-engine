---
keywords: [agents.md, update, güncelleme, onboarding, powershell, cmd, env var, vault_engine, vault_data, reg query, python yok, no python]
links:
  - "[[vault-notes]]"
weights:
  vault-notes: 0.4
---

# AGENTS.md Details

Rarely-needed detail behind `AGENTS.md`'s pointer: Windows variable spelling, the `$VAULT_ENGINE`-empty fallback, the full onboarding procedure, and the no-Python fallback.

## Variable spelling on Windows
`$VAULT_ENGINE` and `$VAULT_DATA` are POSIX-shell (bash, zsh) syntax. In Windows cmd they are `%VAULT_ENGINE%`/`%VAULT_DATA%`; in PowerShell `$env:VAULT_ENGINE`/`$env:VAULT_DATA`. Replace the variables accordingly when running the commands in `AGENTS.md`.

## If `$VAULT_ENGINE` is empty
In PowerShell check `$env:VAULT_ENGINE`; plain `$VAULT_ENGINE` is always empty there. If it is genuinely empty, the terminal or agent was started before setup set it. Tell the user to restart it; until then, on Windows read both values with `reg query HKCU\Environment /v VAULT_ENGINE` (and `/v VAULT_DATA`) in cmd or PowerShell, or `MSYS_NO_PATHCONV=1 reg query 'HKCU\Environment' /v VAULT_ENGINE` in Git Bash, and use them directly.

## Onboarding procedure
If a `profile/` note still contains `<!-- vault:skeleton -->`, run onboarding before anything else:
1. Get the questions with `python "$VAULT_ENGINE/tools/graph.py" onboard --questions`.
2. Ask the user a few at a time, conversationally, in their own language (offer the shown defaults).
3. Save the answers as JSON to a temp file outside this repo.
4. Run `python "$VAULT_ENGINE/tools/graph.py" onboard --answers <file> --data "$VAULT_DATA"` (it commits the profile notes it writes).
5. Delete the temp file and continue with the normal task flow.

## Project selection procedure
If `context`'s output includes a comment about discovered projects without vault notes (for the
current project, or for others found elsewhere), run `python "$VAULT_ENGINE/tools/graph.py"
projects --ask` first: it only reports candidates and instructions, it never writes notes itself.
1. Ask the user which of the listed projects should get vault notes, and whether there is a
   project folder not in the list (not a git repo, or elsewhere) they want included too.
2. For each project the user picks: read its README, top-level folder layout, and manifest files
   (package.json, pyproject.toml, ...), plus recent commit metadata only -- never index code, per
   `$VAULT_ENGINE/defaults/standards/data-policy.md`.
3. Write real `projects/<name>/<name>-overview.md` and `projects/<name>/<name>-status.md` notes
   (no skeleton/placeholder notes -- see `$VAULT_ENGINE/defaults/standards/vault-notes.md`), run
   `python "$VAULT_ENGINE/tools/graph.py" lint`, and commit.
4. For the projects the user does not want notes for, run `projects --skip <name> [<name> ...]`
   (this computer only; `projects --unskip <name>` asks about it again later).

## If Python isn't available
Read `profile/working-style.md` and `profile/language.md` in the vault, then the engine's `$VAULT_ENGINE/defaults/standards/vault-notes.md` and `$VAULT_ENGINE/defaults/standards/data-policy.md` (a vault note at the same relative path replaces the engine's copy).

## Updating the engine
Never update the engine by hand: no `git pull`, `merge`, `rebase` or `checkout` in the engine folder and no ad-hoc branches. Use only the engine's `update` command, run the way the hint prints it (add `--all` once the user agreed, so hooks, agent files and model settings are refreshed without prompts). It works on every channel (stable: release tag; dev: fast-forwards `main` from a clean tree), runs migrate, setup and the user-level steps, then `doctor`. If it refuses (other branch, local changes, diverged `main`), it prints the exact command that fixes that; tell the user instead of improvising.
