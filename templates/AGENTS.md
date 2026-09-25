# AGENTS.md

A model-independent second brain. Information loads through a weighted graph — don't read files one by one. Commands run from the vault root. The engine tool is invoked via the `$VAULT_ENGINE` environment variable (`%VAULT_ENGINE%` in Windows cmd). The data folder's path is in `$VAULT_DATA` (`%VAULT_DATA%` in Windows cmd).

If `$VAULT_ENGINE` is empty, the terminal or agent was started before setup set it. Tell the user to restart it; until then, on Windows read both values with `reg query HKCU\Environment /v VAULT_ENGINE` (and `/v VAULT_DATA`) and use them directly.

0. **First time only:** if a `profile/` note still contains `<!-- vault:skeleton -->`, run onboarding before anything else. Get the questions with `python "$VAULT_ENGINE/tools/graph.py" onboard --questions`, ask the user a few at a time, conversationally, in their own language (offer the shown defaults). Save the answers as JSON to a temp file outside this repo, run `python "$VAULT_ENGINE/tools/graph.py" onboard --answers <file> --data "$VAULT_DATA"`, then delete the temp file and continue with step 1.
1. **Starting a task:** Run `git pull --rebase` in the vault, then `python "$VAULT_ENGINE/tools/graph.py" context "<task summary; keywords in the languages you use>"`. Follow the notes it returns.
   If Python isn't available, read `profile/working-style.md`, `profile/language.md`, `standards/vault-notes.md`, and `standards/data-policy.md`.
2. **If the task came from `inbox/`:** follow the `inbox/inbox.md` flow (`python "$VAULT_ENGINE/tools/graph.py" tasks --status open`).
3. **When the task is done:** run the command on the last line of the `context` output: `reinforce --task <id>` followed by the notes that actually helped. If no note helped, run it with no notes.
4. **If you learn something lasting:** write it to the relevant note following the rules in `standards/vault-notes.md`. Then run `python "$VAULT_ENGINE/tools/graph.py" lint`, commit in English, and push with `git pull --rebase && git push`. The guard hook blocks commits containing personal data or secrets.
5. **If `context` reports a new vault-engine version:** tell the user once and ask before updating — never update without their permission.
