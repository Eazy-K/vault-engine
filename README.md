# vault-engine

A model-independent "second brain" engine. Notes are loaded as a weighted graph; the agent gets only the relevant context for each task. The engine is user-independent: your notes live in a separate **data repository**, and no user information ever enters this repo.

*[Türkçe kısa kurulum için README.tr.md](README.tr.md)*

## Quick start (no technical knowledge needed)

You need an AI coding agent installed and logged in — [Claude Code](https://claude.com/claude-code) or [Codex](https://openai.com/codex). Then paste this to your agent:

> Install vault-engine for me: clone https://github.com/Eazy-K/vault-engine and follow docs/agent-setup.md step by step, asking me before each change.

The agent will check what's on your computer, ask before installing anything or creating any accounts, create two folders (the engine and your personal notes), optionally set up a *private* backup of your notes on GitHub, and ask you a few short questions about how you like to work (language, level of detail, when it should double-check with you). Everything it plans to do, it explains first in plain language — you just answer yes/no and a few simple questions.

## Structure
| Path | Content |
|---|---|
| `tools/graph.py` | Search, propagation, learning, guard, leakcheck. Python standard library only. |
| `tools/feedback.py` | Optional, leak-free feedback (see below). |
| `tools/hooks/` | Guard hooks for data repos (`core.hooksPath` points here) |
| `tools/update.py` | Checks for and applies engine updates on the stable channel |
| `tools/schema.py` | Vault data schema version and migrations |
| `tools/claude-agents/` | Claude Code subagent definitions (copied into `~/.claude/agents/`) |
| `defaults/` | General notes. A note at the same path in the data repo overrides these. |
| `templates/` | Skeleton for a new data repository |
| `.githooks/` | This repo's own hooks: `leakcheck` |
| `tests/` | `python -m unittest discover -s tests` |

## Setup
1. Clone the engine. Python 3.10+ is enough. Optional: Ollama + `ollama pull bge-m3` (without it, only keyword matching is used).
2. Create a data repo: `python tools/graph.py init <data repo path>`. This copies the contents of `templates/` (without overwriting existing files), initializes a git repo (branch `main`, with an initial `chore: initialize vault` commit if git has an identity configured — otherwise `init` tells you how to set one), points `core.hooksPath` at this engine's `tools/hooks` folder, and writes the feedback preference into `vault.config.json` (feedback is `off` by default — opt-in). In a TTY, if `--yes` is not given, it prompts for missing values; they can also be set manually with `--feedback`, `--feedback-mode`, `--project-root` (repeatable), and `--yes`. Per-computer project roots (`--project-root`) are written to `<data repo>/.graph/machine.json`, not to the shared `vault.config.json` — see "Project recognition" below. On a second computer, clone your existing data repo instead and run `git config core.hooksPath <engine>/tools/hooks` in it; don't run `init` on it (it recreates template files you deleted); run `init --project-root <path>` there instead if you need this computer's own project roots.
3. Connect the engine to the data repo: `python tools/graph.py setup [--data <data repo path>]`. This sets environment variables: on Windows, persisted with `setx`; on macOS/Linux, after asking, it writes `VAULT_ENGINE`/`VAULT_DATA` into your shell startup file (`~/.zshrc`, `~/.bashrc`/`~/.bash_profile` on macOS, your fish config, or `~/.profile`) inside a `# >>> vault-engine >>>` block — restart your terminal(s) and any coding agent afterward so they pick up the new values. It also copies `tools/claude-agents/*.md` into `~/.claude/agents/`, and, for each project root that isn't itself a git repo, writes a `@<data repo path>/AGENTS.md` line into `CLAUDE.md`. Running it twice changes nothing. `--user-level` also adds a one-line pointer to `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`. `--no-env`, `--no-agents`, `--no-routing`, `--yes` skip/automate the corresponding steps.
4. Fill in the notes under `profile/`.
5. Optional: `python tools/graph.py onboard` asks a short set of profile questions (chat/notes/code language, detail level, explain level, what to always ask before doing) and writes them into `profile/`. Non-interactive equivalents: `onboard --questions` prints the questions as JSON, `onboard --answers <file.json>` writes given answers.
6. Check: `python tools/graph.py doctor` — one check per line (`OK`/`WARN`/`FAIL`); any `FAIL` sets the exit code to 1. It also warns while the profile is still the unfilled skeleton.

## Project discovery
`tools/discovery.py` scans the git repos next to the engine (or under `project_roots` in `vault.config.json`) and finds which projects have no notes yet. It only reads lightweight metadata (the README's first line, a file-extension count, the last commit date, the remote's host part only); code is never indexed and nothing leaves the machine. The `exclude` list (names or globs) in `vault.config.json` and a `.vaultignore` file in a repo exclude it from the scan. The result is cached for 1 hour in `<data repo>/.graph/projects.json` (not committed). Usage: `python tools/graph.py projects` (table), `--json`, `--missing` (only projects without notes, with a suggestion line at the end), or `--refresh` (ignore the cache).

## Project recognition
`context` finds the project from the directory it's run in and uses `projects/<name>/` notes as the starting point. Project roots are per computer, since two computers sharing one data repo can keep their code in different places. Lookup order: `<data repo>/.graph/machine.json` (`{"project_roots": ["..."]}`, gitignored, written by `init --project-root` or `init`'s interactive prompt) first; then, for old vaults, `project_roots` in the shared `vault.config.json`; if neither is set, the engine's parent folder is used. Set manually with `--project <name>` or `--no-project`.

## Feedback
`tools/feedback.py` optionally sends feedback from your setup to the engine's developers (as a GitHub issue). It is off by default; you must explicitly turn it on before anything is sent.

Levels (in `vault.config.json` under `"feedback": {"level": ..., "mode": ..., "repo": "owner/name"}`):
- `off` (default): nothing is sent.
- `metrics`: only counts and fixed categories are sent -- engine version, OS, Python version, note-count bucket (`<25`, `25-100`, ...), `context`/`reinforce` usage counters, per-agent distribution. Note IDs, query text, file paths, and machine names are **never** sent. Sent at most once every 7 days.
- `reports`: in addition to the above, short free-text notes queued with `feedback add --kind friction|bug|idea --command <command> --summary "..." [--details "..."]` are also sent (summary max 200, details max 1000 characters).

`mode`: `ask` (default; shows a preview first, the agent asks the user, confirmed with `feedback send --yes`) or `auto` (the engine sends in the background at most once a day after `reinforce`).

The target repo is taken from the `feedback.repo` field, or otherwise from the engine's `origin` remote; if neither is set, sending is disabled (no error).

Free text (summary/details/command) is filtered twice, once when queued and once right before sending: it's scanned against the same guard patterns used for notes (national ID numbers, IBAN, card numbers, phone, email, tokens, passwords), the home directory path, the git identity, terms from `.git/info/vault-denylist`, and project names in the data repo. If anything matches, the text is **never** sent or printed; only the match type (e.g. "national ID") is reported, and the content is moved to `.graph/feedback/quarantine/`.

Commands:
- `feedback status` -- active settings, target repo, queue/quarantine counts, last send time.
- `feedback list` -- shows exactly what would be sent, as JSON (does not send).
- `feedback send [--yes]` -- sends what's queued.
- `feedback set --level ... [--mode ...] [--repo ...] [--machine]` -- writes settings. With `--machine`, writes to a machine-local, gitignored `.graph/machine.json` (overrides the shared setting in the data repo) — useful on a sensitive machine.

To turn off: `feedback set --level off` (or with `--machine` for a specific machine).

## CI (GitHub Actions)
- Engine repo: on every push and PR, tests run (Ubuntu: Python 3.10 and 3.13, Windows: 3.13) along with `leakcheck` (files + commit messages).
- Data repo: the `.github/workflows/vault.yml` shipped by `init` uses the engine as a composite action (`uses: <owner>/vault-engine@<ref>`): guard, commit messages, and note lint. This way commits pushed from the cloud or a phone are checked too. `init` pins `<ref>` to the installed engine's release tag on the stable channel, or `main` otherwise; `update` moves that pin to the new tag when it switches versions, and prints that the changed `.github/workflows/vault.yml` must be committed and pushed in the data repo.
- The engine is public, so this works as-is. If you instead use a private fork of the engine, do this once in that repo: Settings > Actions > General > Access → "Accessible from repositories owned by the user". Or via the CLI: `gh api -X PUT repos/<owner>/vault-engine/actions/permissions/access -f access_level=user`

## Versions and updates
- Releases follow [SemVer](https://semver.org/) and are listed in [CHANGELOG.md](CHANGELOG.md). While the version is 0.x, a minor bump (0.1 → 0.2) may change config, commands or the data layout; its changelog entry then has **Upgrade notes**.
- Current version: `python tools/graph.py --version` (also the first line of `doctor`).
- **Stable channel**: the engine checked out at a release tag, as the install guide does. Don't run `git pull` there — HEAD is detached at a tag, not on a branch.
- **Dev channel**: the engine on a branch (e.g. `main`). There `update` does nothing; use `git pull`, then `migrate`.
- On the stable channel the engine checks the newest remote tag with `git ls-remote` (nothing is sent) at most once a day, after `reinforce`; `doctor` checks every run. If a newer version exists, `doctor` shows a `WARN` and `context` prints one line telling the agent to ask the user once. Turn this off with `"updates": {"check": false}` in `vault.config.json`, or in `.graph/machine.json` for a single computer.
- Update: `python tools/graph.py update` fetches tags, shows the CHANGELOG sections between the current and the new version (including Upgrade notes), asks for confirmation, then checks out the new tag and runs `migrate --yes`, `setup --yes` and `doctor` with the new version.
  - `--check`: only reports whether a newer version exists.
  - `--to <tag>`: switches to a specific version, including an older one; going back is refused if the vault was already migrated to a newer data schema.
  - `--yes`: skips the confirmation prompt.
  - `--apply-agents`: if `templates/AGENTS.md` differs from the data repo's `AGENTS.md`, `update` shows the diff and only replaces the file with this flag (or a yes on the interactive prompt).
- `vault.config.json` has a `"schema": <int>` field (the vault's data layout version; missing means `1`). An engine older than the vault's schema refuses commands that write shared data (`reinforce`, `decay`, `mv`, `onboard`, `init`, `feedback set`) but `context` still works, and it asks to run `update` on that computer — useful when two computers share one vault and update at different times. `python tools/graph.py migrate [--yes]` upgrades an older vault to the current schema and makes one commit in the data repo (never pushes).
- Installs on v0.2.0 have no `update` command. The first update from there is manual: `git fetch --tags && git checkout <new tag>` in the engine folder, then `python tools/graph.py migrate --yes` and `doctor`.
- Maintainers: bump `__version__` in `tools/graph.py`, add a CHANGELOG entry, merge, then tag `vX.Y.Z` and create a GitHub release. A test fails if HEAD is tagged `vX.Y.Z` but `__version__` differs.

## Contributing to the engine
- Run `git config core.hooksPath .githooks` in this repo. `leakcheck` blocks commits containing user content (`profile/`, `projects/`, ...), the home directory path, the git identity, or terms from `.git/info/vault-denylist`.
- New code should come with tests.

## License
MIT, see LICENSE.
