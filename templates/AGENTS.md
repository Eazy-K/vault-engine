# AGENTS.md

A model-independent second brain: information loads through a weighted graph, not by reading files one by one. Run commands from your project folder, not the vault — `context` detects the project from your cwd. Engine: `$VAULT_ENGINE`; vault data: `$VAULT_DATA` (`$env:VAULT_ENGINE`/`$env:VAULT_DATA` in PowerShell). Commands below use POSIX-shell syntax; Windows variable spelling, the `$VAULT_ENGINE`-empty fallback, onboarding steps and the no-Python fallback are in `$VAULT_ENGINE/defaults/standards/agents-details.md`.

**Orchestration:** For multi-step work, present a short plan and wait for explicit user approval. If the scope is ambiguous, ask one clarifying question. After approval, delegate to a worker subagent and stay within the approved scope. Follow `$VAULT_ENGINE/defaults/standards/orchestration.md`.

0. **First time only:** if a `profile/` note still has `<!-- vault:skeleton -->`, run onboarding first — procedure in the details note above.
1. **Starting a task:** if the vault has a remote (`git remote` prints something), run `git pull --rebase --autostash` in it, then `python "$VAULT_ENGINE/tools/graph.py" context "<task summary; keywords in the languages you use>"` and follow the notes it returns. If it reports projects without vault notes, run `projects --ask` and ask the user which to add; never create placeholder notes yourself.
2. **Task from `inbox/`:** follow the flow in the `inbox` note (`$VAULT_ENGINE/defaults/inbox/inbox.md`); list open tasks with `python "$VAULT_ENGINE/tools/graph.py" tasks --status open`.
3. **Task done:** run the command on the last line of `context`'s output: `reinforce --task <id>` followed by the notes that actually helped, or just `--task <id>` if none did. `reinforce` commits this computer's learned-links file itself.
4. **Learned something lasting:** write it to the relevant note per the `vault-notes` note (`$VAULT_ENGINE/defaults/standards/vault-notes.md`), then run `python "$VAULT_ENGINE/tools/graph.py" lint` and commit the notes you changed, in English. The guard hook blocks commits with personal data or secrets.
5. **Before you finish:** if the vault has a remote, run `git pull --rebase --autostash && git push` in it, even with no note — it sends the commits `reinforce` and other engine commands made. No remote: the commits are enough.
6. **`context` reports a new vault-engine version:** tell the user once, ask before updating.
7. **`context` reports Claude Code hooks/model settings/agent files out of date:** ask the user once, then run the `update` command it gives (`--claude-hooks --models --agents`).

<!-- vault-engine AGENTS.md template: 8. If you translate or edit this file, keep this line: `update` and `doctor` use it to tell whether the file is behind the engine's template. -->
