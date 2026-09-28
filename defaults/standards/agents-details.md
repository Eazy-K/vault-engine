---
keywords: [agents.md, onboarding, powershell, cmd, env var, vault_engine, vault_data, reg query, python yok, no python]
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

## If Python isn't available
Read `profile/working-style.md` and `profile/language.md` in the vault, then the engine's `$VAULT_ENGINE/defaults/standards/vault-notes.md` and `$VAULT_ENGINE/defaults/standards/data-policy.md` (a vault note at the same relative path replaces the engine's copy).
