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
- Keep sentences short: one fact per sentence. Use at most 30 words per sentence. A backticked path or code span, a `[[link]]` and a URL each count as one word. `lint` warns above this limit, except in task notes (`type: task` and `inbox/`). It ignores fenced code, tables and headings.
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

## Project status note
- `projects/<project>/<project>-status.md` is a compiled summary, not a journal. `context` loads it outside the budget on every call.
- The body of a status or overview note stays under 5,000 characters; `lint` reports an error above that.
- Sections: purpose and decisions (short), "Where we are" (at most 5 dated items, newest first), "Open work", "Next step".
- When a sixth item arrives, move the oldest ones with the `rotate <project>` command (see Tool), never by hand. History is the raw record: append-only, loaded only when a search matches.
- Delegate the status and lessons update to a worker-low subagent. The orchestrator does not edit these notes inline.
- If your section headings are not "Where we are" and "Journal", set `status_note.recent_heading` and `status_note.journal_heading` in vault.config.json.
- "Lesson:" lines go to `<project>-lessons.md`, not to the status note: short, keyword-rich, one lesson per bullet.
- Recipes (seeds, scripts, command sequences, edit files) live in the project repository, not in the vault; the status note gives the path only.
- Every claim in the status note carries a date. Update or delete a "not yet" or "in progress" claim older than 7 days.

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
- `reinforce --task <id> [--outcome ok|partial|fail] [--rework]`, `decay`: learning. `reinforce` also runs `decay` by itself when more than 7 days have passed since this computer's last decay (the timestamp is kept per computer, not committed), so running `decay` by hand is rarely needed
- `stats`: shows what fraction of `context` calls were closed with `reinforce`, and which notes get fetched but never used, and (when the delegation hook has logged them) how often the orchestrator did multi-call work inline
- `lint`: consistency check (warns about notes with sentences over 30 words; reports an error for status/overview notes over 5,000 characters)
- `mv <note> <new path or name>`: moves or renames a note and updates `[[links]]`, `weights` and learned edges. Don't move notes by hand.
- `rotate <project>`: moves the oldest dated items of the status note to the history note until 5 remain, then commits those two notes (`--no-commit`, `--dry-run`). `reinforce` warns when a status note needs it.
- `index`: precomputes embeddings
- `tasks`: lists inbox tasks (`--status`, `--project`)
- `guard`: scans for personal data and secrets (hooks run it automatically)
