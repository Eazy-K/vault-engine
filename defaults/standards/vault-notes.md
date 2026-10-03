---
keywords: [vault, not, yazma, güncelleme, frontmatter, lint, hafıza, memory, note, update, kural, rule]
links:
  - "[[data-policy]]"
weights:
  data-policy: 0.4
---

# Vault Note-Writing Rules

## What to write
- Write lasting, important information: preferences, decisions, project facts, things learned.
- Don't write information that only matters for that one conversation.
- Try to update an existing note first, don't create a duplicate. Delete or fix information that turns out to be wrong.

## How to write
- One note covers one topic. Notes mixing topics match poorly in search.
- Bullet points and table rows must stand on their own, because search treats each one as a separate chunk.
- Don't write placeholder or example lines — they cause false matches.
- Dates are written in absolute form (YYYY-MM-DD).
- Keep sentences short: one fact per sentence. Use at most about 25 words (`lint` warns above this).
- Use the same term for the same thing every time. Do not switch synonyms; put synonyms in `keywords`.
- Prefer active voice. Write instructions in imperative form, one instruction per sentence.
- Cut filler words.
- File names are English and kebab-case; content language is whatever the user has set in `profile/language.md`.

## Frontmatter
```
---
keywords: [english, other-language, synonyms, abbreviations]
links:
  - "[[related-note]]"
weights:
  related-note: 0.8
---
```
- `keywords` is used for keyword matching. Add abbreviations (db, api) and equivalents in other languages here.
- Weight guide: 0.8-1.0 strong relation, 0.5-0.7 related, 0.3 weak relation. Defaults to 0.7 if omitted.
- `core: true` is only for mandatory rules that apply to every task. Core notes load on every call, so they **must not exceed 15 lines**.

## Folders
- `profile/`: the user and their preferences
- `projects/<project>/`: project information
- `standards/`: conventions (the engine ships defaults in `$VAULT_ENGINE/defaults/standards/`; a vault note at the same relative path replaces one)
- `notes/`: personal notes
- `decisions/`: vault decisions (ADR)
- `inbox/`: cross-machine task queue (see [[inbox]])

## Tool
`python "$VAULT_ENGINE/tools/graph.py" <command>`, from any folder (`context` recognizes the project from the folder it runs in)
- `context`, `query`: load and list context (`--seed`, `--threshold`, `--budget`, `--no-semantic`, `--no-log`)
- `show`: shows a note's edges (`--body` prints its full text; `context` names this command for notes it truncated or left out)
- `reinforce --task <id>`, `decay`: learning. `reinforce` also runs `decay` by itself when more than 7 days have passed since this computer's last decay (the timestamp is kept per computer, not committed), so running `decay` by hand is rarely needed
- `stats`: shows what fraction of `context` calls were closed with `reinforce`, and which notes get fetched but never used, and (when the delegation hook has logged them) how often the orchestrator did multi-call work inline
- `lint`: consistency check (also warns about notes with sentences over 25 words)
- `mv <note> <new path or name>`: moves or renames a note and updates `[[links]]`, `weights` and learned edges. Don't move notes by hand.
- `index`: precomputes embeddings
- `tasks`: lists inbox tasks (`--status`, `--project`)
- `guard`: scans for personal data and secrets (hooks run it automatically)
