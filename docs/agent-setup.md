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
Step 3 and Step 5.

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

Do **not** run `init` on it: it would recreate template files the user deleted on
purpose. `vault.config.json` is shared by every computer using this data repo; per-computer
project roots instead live in `<data repo>/.graph/machine.json`, written by `init
--project-root` — without it, each computer uses the folder that holds its engine, which
is why Step 1 puts the engine inside the projects folder.

Then do Step 4 (git identity is per computer), skip Step 5 (the backup already exists),
do Step 6, and skip Step 7 unless `doctor` says the profile is still the unfilled
skeleton.

---

## Step 4 — Git identity for the data repo (only if missing)

Check inside the data repo:
```
git -C "<projects folder>/vault" config user.email
git -C "<projects folder>/vault" config user.name
```
If either is empty, ask the user for a name to use for their own notes' commit history
(it never leaves their machine unless they chose the GitHub backup in the next step).
Suggest GitHub's private no-reply email format (`<username>@users.noreply.github.com`) if
they plan to back up to GitHub, otherwise any name/email they're comfortable with. Never
ask for more personal data than git itself needs.
```
git -C "<projects folder>/vault" config user.name "<name>"
git -C "<projects folder>/vault" config user.email "<email>"
```

`init` (Step 3) already tried to make an initial `chore: initialize vault` commit; if it
printed that it couldn't (no git identity yet), make that commit now that identity is set:
```
git -C "<projects folder>/vault" add -A
git -C "<projects folder>/vault" commit -m "chore: initialize vault"
```

---

## Step 5 — Optional private backup on GitHub

Ask: "Do you want your notes backed up privately on GitHub?" Only proceed if yes. This
needs a commit to push, which is why it comes after Step 4.

1. Check `gh` is installed and logged in: `gh auth status`. If not logged in, run
   `gh auth login` and let the user complete it interactively.
2. Create a **private** repo and push the data folder to it. Make it unmistakably clear
   to the user that this repository must stay private (it will hold their notes):
   ```
   gh repo create <name> --private --source "<projects folder>/vault" --push
   ```
3. If there's no GitHub account or the user declines, say clearly: "Your notes will
   stay only on this computer, that's fine." Mention that this also means later steps
   that would `git pull`/`git push` are simply skipped — the vault works fully offline.

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

On Windows this uses `setx`, which only takes effect in a **new terminal window** — mention
this to the user. On macOS/Linux, it asks first, then writes `VAULT_ENGINE`/`VAULT_DATA`
into the shell startup file it detects (`~/.zshrc`, `~/.bashrc`/`~/.bash_profile` on macOS,
the fish config, or `~/.profile`), inside a clearly marked `# >>> vault-engine >>>` block.
Either way, tell the user to restart their open terminal(s) and any coding agent
(Claude Code, Codex) afterward — they only see the new values after that restart.

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

Only 0.3.0 and later check for new versions and have `update`. On 0.1.0 and 0.2.0 you
get no update line, and `update` fails with `invalid choice: 'update'`; use the next
section instead.

### Updating from 0.1.0 or 0.2.0 (no `update` command)

Check the version first: `python "<projects folder>/vault-engine/tools/graph.py" --version`.
If it says 0.1.0 or 0.2.0, update by hand once; from then on `update` works. These steps
do what `update` does. Ask the user before step 4, like for any update. The copy of this
guide inside an 0.1.0/0.2.0 engine folder doesn't have this section yet; read it on
GitHub, or with `git -C "<projects folder>/vault-engine" show origin/main:docs/agent-setup.md`
after step 2.

1. Make sure the engine folder has no local changes: `git -C "<projects folder>/vault-engine" status --porcelain`
   must print nothing. If it does, show the user and stop.
2. Download the release list and find the newest release:
   ```
   git -C "<projects folder>/vault-engine" fetch --tags origin
   git -C "<projects folder>/vault-engine" tag --list "v*" --sort=-v:refname
   ```
   The first line is the newest release (for example `v0.4.0`).
3. Read what changed: `git -C "<projects folder>/vault-engine" show <new tag>:CHANGELOG.md`.
   Summarize every section newer than the current version for the user, above all the
   **Upgrade notes**, and ask whether to proceed.
4. Switch to it: `git -C "<projects folder>/vault-engine" checkout <new tag>`
   (a "detached HEAD" message is expected).
5. Run the new version's upgrade steps, in this order:
   ```
   python "<projects folder>/vault-engine/tools/graph.py" migrate --yes
   python "<projects folder>/vault-engine/tools/graph.py" setup --data "<projects folder>/vault" --yes
   python "<projects folder>/vault-engine/tools/graph.py" doctor
   ```
   `migrate` updates the data repo's settings and makes one commit there with only its
   own changes (it never pushes). `setup` is the same as in Step 6 (add `--user-level` if
   the user works with Codex). On macOS/Linux it now also writes the environment
   variables into the shell startup file.
6. Go through the `WARN` lines `doctor` prints; each one names its fix. For example, if
   it says the vault CI runs the engine at `@main` or an older tag, run
   `python "<projects folder>/vault-engine/tools/graph.py" update`: it says "already up to
   date" and re-pins the CI. If `doctor` has no such line but
   `<projects folder>/vault/.github/workflows/vault.yml` still says `vault-engine@main`,
   change that to `vault-engine@<new tag>` by hand.
7. Apply the rest of the Upgrade notes you read in step 3 (for example lines to copy
   from `templates/AGENTS.md` into the data repo's `AGENTS.md`). Commit what changed in
   the data repo, and push it if it has a backup (`git -C "<projects folder>/vault" push`).
8. Do the same on every other computer that uses this data repo. Engines before 0.3.0
   don't know the vault's data schema, so they never refuse to write to a vault that a
   newer engine has upgraded. Until every computer is on 0.3.0 or later, don't install a
   release whose Upgrade notes say it raises the data schema.

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
  release tag (detached HEAD) on purpose; use `update` instead of `git pull` (on 0.1.0
  or 0.2.0, see "Updating from 0.1.0 or 0.2.0").
- **`invalid choice: 'update'` (or `'migrate'`)**: the engine is 0.1.0 or 0.2.0, which
  have neither command. Follow "Updating from 0.1.0 or 0.2.0" above.
