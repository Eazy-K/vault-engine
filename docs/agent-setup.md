# Agent setup guide (for the AI agent doing the install)

You are installing **vault-engine** for a non-technical user who pasted a one-sentence
request into you. They have never used git, YAML, or environment variables — do not
assume any of that knowledge. Follow this guide step by step.

## Ground rules

- **Speak the user's language.** Detect it from their message and reply in that language
  throughout, even though this guide is in English.
- **One plain sentence per step.** Before you run a command, tell the user in one simple
  sentence what it does and why — no jargon (say "a private online backup", not "a
  remote git repository").
- **Ask before you act**, specifically before: installing any software, creating a
  GitHub repository, or changing environment variables. A simple "OK to continue?" is
  enough; wait for a yes.
- **Never invent paths or answers.** If a step below asks something of the user, ask
  them — don't guess personal details (real name, email, etc.) beyond what git itself
  requires, and don't collect anything else.
- If a command fails, show the user the plain-English reason and offer to retry, skip,
  or stop — don't loop silently.

Commands below use `python`; if the user's `python` is Python 2 or missing, try
`python3` first.

---

## Step 0 — Check prerequisites

Check for each tool and, only after the user agrees, install what's missing with the
platform's package manager.

| Tool | Check | Windows (winget) | macOS (brew) | Linux (apt) |
|---|---|---|---|---|
| Python 3.10+ | `python --version` | `winget install Python.Python.3.12` | `brew install python@3.12` | `sudo apt install python3` |
| git | `git --version` | `winget install Git.Git` | `brew install git` | `sudo apt install git` |

Optional, ask separately (explain each is optional and what it buys the user):

| Tool | Why | Windows | macOS | Linux |
|---|---|---|---|---|
| GitHub CLI `gh` | lets you create a **private** backup repo for their notes | `winget install GitHub.cli` | `brew install gh` | `sudo apt install gh` |
| Ollama | better note search (semantic, not just keyword); explain it downloads a ~1.2 GB model and is entirely optional | `winget install Ollama.Ollama` | `brew install ollama` | see https://ollama.com/download |

If the user wants Ollama, after installing run:
```
ollama pull bge-m3
```

If they skip `gh` or Ollama, say plainly that everything still works — backups just
stay local, and search falls back to keyword matching.

---

## Step 1 — Pick folders

Ask the user where they keep their code/projects (the "projects folder"). Suggest a
sensible default such as `~/Dev` (or `%USERPROFILE%\Dev` on Windows) if they're unsure.

Default locations, inside that folder:
- Engine: `<projects folder>/vault-engine`
- Data (their notes): `<projects folder>/vault`

Confirm both paths with the user before creating anything.

Also ask: "Have you used vault-engine on another computer, with your notes backed up
on GitHub?" If yes, ask for that backup's address and follow **Step 3b** instead of
Steps 3 and 4.

---

## Step 2 — Clone the engine

Tell the user this downloads the vault-engine program itself (no personal data in it).

```
git clone https://github.com/Eazy-K/vault-engine.git "<projects folder>/vault-engine"
cd "<projects folder>/vault-engine"
git checkout "$(git describe --tags --abbrev=0)"
```

The last command switches to the latest released version, so the user never gets unfinished work from `main`. If the repository has no release tags yet, skip it. To update later use `python "<projects folder>/vault-engine/tools/graph.py" update`, which shows what changed and asks before switching to the new version — never `git pull` on this install (HEAD is detached at a tag, not on a branch). See "Updating (later sessions)" below.

---

## Step 3 — Create the data repo

This creates the folder that will hold the user's own notes, as a local git repository.

```
python "<projects folder>/vault-engine/tools/graph.py" init "<projects folder>/vault" --yes
```

---

## Step 3b — Use an existing data repo (second computer)

Only if the user already has a data repo from another computer. Tell them this
downloads their own notes from their private backup.

1. Check `gh auth status`; if not logged in, run `gh auth login` and let the user
   complete it (the backup is private, so git needs their login).
2. Clone it and point it at this engine's guard hooks:
   ```
   git clone <backup address> "<projects folder>/vault"
   git -C "<projects folder>/vault" config core.hooksPath "<projects folder>/vault-engine/tools/hooks"
   ```
   Use forward slashes in the `core.hooksPath` value, also on Windows.

Do **not** run `init` on it: `init` rewrites `vault.config.json` (shared settings such
as feedback and project roots) and recreates template files the user may have deleted.
If their project folder differs from the other computer's, add it to `project_roots` in
`vault.config.json` by hand instead.

Then skip Step 4, do Step 5 (git identity is per computer) and Step 6, and skip Step 7
unless `doctor` says the profile is still the unfilled skeleton.

---

## Step 4 — Optional private backup on GitHub

Ask: "Do you want your notes backed up privately on GitHub?" Only proceed if yes.

1. Check `gh` is installed and logged in: `gh auth status`. If not logged in, run
   `gh auth login` and let the user complete it interactively.
2. Create a **private** repo and push the data folder to it. Make it unmistakably clear
   to the user that this repository must stay private (it will hold their notes):
   ```
   gh repo create <name> --private --source "<projects folder>/vault" --push
   ```
3. If there's no GitHub account or the user declines, say clearly: "Your notes will
   stay only on this computer, that's fine."

---

## Step 5 — Git identity for the data repo (only if missing)

Check inside the data repo:
```
git -C "<projects folder>/vault" config user.email
git -C "<projects folder>/vault" config user.name
```
If either is empty, ask the user for a name to use for their own notes' commit history
(it never leaves their machine unless they chose the GitHub backup). Suggest GitHub's
private no-reply email format (`<username>@users.noreply.github.com`) if they pushed to
GitHub in step 4, otherwise any name/email they're comfortable with. Never ask for more
personal data than git itself needs.
```
git -C "<projects folder>/vault" config user.name "<name>"
git -C "<projects folder>/vault" config user.email "<email>"
```

---

## Step 6 — Connect the engine (environment variables + agent files)

Explain: this tells your coding agent(s) where the engine and the notes live.

```
python "<projects folder>/vault-engine/tools/graph.py" setup --data "<projects folder>/vault" --yes
```

If the user works with **Codex** (in addition to or instead of Claude Code), add
`--user-level`:
```
python "<projects folder>/vault-engine/tools/graph.py" setup --data "<projects folder>/vault" --yes --user-level
```
(`--user-level` is needed for Codex because Codex does not read `AGENTS.md` files above
the git root of the repo it's running in — this adds a pointer in `~/.codex/AGENTS.md`
so it's picked up everywhere.)

On Windows this uses `setx`, which only takes effect in a **new terminal window** —
mention this to the user.

---

## Step 7 — Get to know the user (onboarding conversation)

This fills in a short profile so the agent adapts to how the user likes to work. Ask a
few questions at a time, in the user's language, offering sensible defaults so they can
just say "that's fine."

1. Get the questions as structured data:
   ```
   python "<projects folder>/vault-engine/tools/graph.py" onboard --questions
   ```
   This prints JSON with question ids: `chat_language`, `notes_language`,
   `code_language`, `detail` (short/balanced/detailed), `explain_level`
   (beginner/intermediate/expert), `ask_before` (multiple choice: architecture,
   new-dependencies, deleting, paid-or-external-services, pushing-or-publishing),
   `extra` (free text, optional).
2. Ask the user each question conversationally (translate into their language; a couple
   at a time, not all at once), propose a default, accept their answer or the default.
3. Write the answers as JSON to a **temporary file outside any repo**, e.g. your own
   scratch/temp directory, not inside the engine or data folder.
4. Save the profile:
   ```
   python "<projects folder>/vault-engine/tools/graph.py" onboard --answers "<temp file path>"
   ```
5. Delete the temporary file afterward.

---

## Step 8 — Health check

```
python "<projects folder>/vault-engine/tools/graph.py" doctor
```
Each line is `OK`, `WARN`, `FAIL`, or `INFO`. Fix what you reasonably can (e.g. rerun a
skipped step), explain anything you can't fix (e.g. "Ollama not running" is fine to
leave — see Troubleshooting), and don't treat `INFO`/optional `WARN`s as failures.

---

## Step 9 — Summary for the user

End with a short, plain-language summary covering:
- What was installed and where (engine folder, data folder).
- Whether a private GitHub backup was set up, and its name.
- How to use it: "Just keep working with your coding agent as usual — it will
  automatically load your relevant notes for each task."
- How to add notes: edit files under `<data folder>/profile/` and `<data folder>/projects/`.
- How to undo: delete the engine and data folders; remove `VAULT_ENGINE` and
  `VAULT_DATA` from environment variables (Windows: System Properties → Environment
  Variables, or `setx VAULT_ENGINE ""`); optionally delete the GitHub backup repo with
  `gh repo delete <name>`.

---

## Updating (later sessions)

When `context` output includes the update line, or `doctor` shows a `WARN` about a
newer version, ask the user once, in plain language ("A new version of vault-engine is
available, want me to check what changed?"). If they agree:

1. Run `python "<projects folder>/vault-engine/tools/graph.py" update --check` to see
   what's new, and summarize the changelog for the user in their own language.
2. Only after the user agrees to proceed, run `update --yes` to actually switch versions.
3. If the update shows a diff for `AGENTS.md`, explain in plain language what changed
   and ask before re-running with `--apply-agents`.
4. If a command refuses to run because the vault's data schema is newer than this
   computer's engine, run `update` on this computer too.

---

## Troubleshooting

- **`python` / `python3` not found**: the user needs to install Python (step 0) and
  possibly restart their terminal.
- **Windows: env vars don't seem set after `setup`**: `setx` only applies to *new*
  terminals — close and reopen, or restart the coding agent.
- **Codex on Windows can't run commands**: Codex's sandbox may need
  `sandbox_mode = "danger-full-access"` in `~/.codex/config.toml` to execute the
  commands in this guide. Explain the trade-off plainly — this gives Codex
  unrestricted access to run commands on the machine — and only change it if the user
  agrees; otherwise offer to run the commands yourself if you're not Codex, or ask the
  user to run them manually.
- **Ollama not running / unreachable**: not a problem. `doctor` will warn, but the
  engine falls back to keyword-only search; tell the user it's optional and they can
  start Ollama (or install it) anytime later.
- **`git pull` says you are not on a branch**: stable installs are checked out at a
  release tag (detached HEAD) on purpose; use `update` instead of `git pull`.
