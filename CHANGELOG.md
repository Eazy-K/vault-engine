# Changelog

All notable changes to vault-engine. Versions follow [SemVer](https://semver.org/); while the version is 0.x, minor releases may include breaking changes, listed under **Upgrade notes**. A release that raises the vault data schema says so there (`schema N`) and asks to bring every computer that shares the vault to 0.3.0 or later first: 0.1.0 and 0.2.0 have no schema check.

## [0.4.0] - 2026-09-26

### Changed
- `tools/claude-agents/worker-low.md` and `worker-medium.md` gain two token-saving rules (read large files with Grep/targeted Read first; keep test/command output and reports short) so they ship with the template instead of only living in hand-edited local copies.

### Added
- `machine --project-root` registers this computer's project root in `.graph/machine.json`, replacing the earlier `init --project-root` on a second computer; `init` now refuses to run inside another git repository.
- `init` and `setup` scopes are split: `update` no longer touches shell/env files (equivalent to `setup --yes --no-env`), and `setup` preserves the user's own `worker-*.md` files.
- Guard also catches foreign (non-Turkish) IBAN, phone and SSN-like formats; the keyword fallback used without Ollama loads fewer notes, and an Ollama model hint plus an embedding timeout were added.
- `update.channel()` gains a `"prerelease"` value, backward compatible with the existing stable/dev channels.
- `templates/AGENTS.md` now points at real `defaults/` paths, adds a working note per folder, a `template: N` revision line, and (revision 2) `pull --autostash` plus an end-of-task push step; `update --apply-agents` diffs and applies template changes while keeping the last line of translated/customized copies.
- Windows installation hardening: a learned `VAULT_DATA` fallback with a restart notice, and NUL-stdin handling for `onboard`/`update`/`migrate`.
- Engine commands (`onboard`, `reinforce`, `migrate`, etc.) commit the files they write themselves instead of leaving them staged; a `.gitattributes` (`* text=auto eol=lf`) is added to new and existing vaults to avoid CRLF diffs.

### Fixed
- `migrate` and project-root detection hardened (root detection edge cases, hook Python selection actually finds a working interpreter, `init --force` for a previously initialized folder, the master→main doctor suggestion adapts to the current branch, `update`/`migrate --data`).
- Embedding hang, PowerShell quoting note, additional guard format coverage, and a projects-list cache fix from the Windows installation retest.
- NUL-stdin exit codes for `onboard`/`update`/`migrate` corrected; an existing vault's `AGENTS.md` env-var line can be moved by hand.

### Upgrade notes
- First update from 0.1.0 or 0.2.0 is still manual (see "Updating from 0.1.0 or 0.2.0" in the README); it runs the old updater's one-time `setup --yes`, then `python tools/graph.py migrate --yes`, which records `"schema": 1` and commits once in the data repo, then `doctor`. Push the data repo afterwards. From 0.3.0 on, use `update`.
- If `vault.config.json` still has a computer-specific `project_roots` entry, move it to `.graph/machine.json` on that computer with `machine --project-root <path>` (not `init --project-root`, which no longer exists) and remove it from the shared file. Also make sure every computer sharing the vault, and the data repo's CI pin (`@v0.3.0` or later), are on 0.3.0+ before continuing.
- `update` no longer touches shell/env files; if you relied on it re-running `setup`'s environment step, run `setup --yes` yourself once.
- `setup` now leaves your own `worker-*.md` files alone; the learned machine file name is a neutral `pc-xxxx` instead of the hostname, and existing `pc-<hostname>.json` files can be renamed or left in place.
- If guard now flags previously-accepted foreign IBAN/phone/SSN-like text in existing notes, mask it or add `guard:ignore`.
- `templates/AGENTS.md` in an existing data repo is not updated automatically; run `update --apply-agents` (or diff manually) to pick up the real `defaults/` paths, the `template: N` line, and the `pull --autostash` / end-of-task push steps, keeping the last line of any translated or customized copy.
- No vault data schema change in this release (schema stays as introduced in 0.3.0).

## [0.3.0] - 2026-09-25

### Added
- `update [--check] [--to TAG] [--yes] [--apply-agents]` moves a stable install (engine checked out at a release tag) to another release. It fetches tags, shows the CHANGELOG sections in between, asks for confirmation, checks out the tag, then runs the new version's `migrate`, `setup` and `doctor`. A dirty checkout, and a downgrade below the vault's data schema, are refused. A development checkout (on a branch) is left alone.
- Release check on the stable channel: at most once a day after `reinforce`, the engine reads the newest tag with `git ls-remote` (nothing is sent). `doctor` shows a WARN and `context` one hint line when a newer version exists. Turn it off with `"updates": {"check": false}` in `vault.config.json` or `.graph/machine.json`. `doctor` also shows the channel.
- Vault data schema: `vault.config.json` gets `"schema"` (missing means 1). An engine older than the vault refuses commands that write shared data (learned edges, `mv`, `onboard`, `init`, `feedback set`), while `context` keeps working. This check starts with 0.3.0: 0.1.0 and 0.2.0 write to any vault. `migrate [--yes]` upgrades an older vault and commits only the files it changed.
- On macOS/Linux, `setup` asks and then writes `VAULT_ENGINE`/`VAULT_DATA` into the shell startup file (`~/.zshrc`, `~/.bashrc` or `~/.bash_profile` on macOS, fish config, or `~/.profile`) inside a `# >>> vault-engine >>>` block. `doctor` tells "set there, restart needed" apart from "not set".
- Setup guide: using an existing data repo on a second computer (Step 3b), and an "Updating" section for agents.

### Fixed
- `context` counted core notes and the project's status/overview against the token budget even though it always prints them. In a project with a long status note, no task-relevant note was loaded.
- `init` overwrote an existing `vault.config.json`. It now keeps existing keys.
- Project roots are per computer: they are read from `.graph/machine.json` first, then `vault.config.json`, then the engine's parent folder. `init` no longer writes this computer's absolute path into the shared `vault.config.json`.
- A new data repo starts on branch `main`, the branch its CI watches, with an initial commit when git has an identity. The setup guide asks for the git identity before the GitHub backup, because the push needs a commit.
- The data repo CI (`.github/workflows/vault.yml`) is pinned to the installed engine tag instead of `@main`; `update` moves the pin.
- The `AGENTS.md` template only runs `git pull`/`git push` when the vault has a remote.

### Upgrade notes
- **First update from 0.1.0 or 0.2.0 is manual** (neither has an `update` command; see "Updating from 0.1.0 or 0.2.0" in the README for the full steps). In the engine folder: `git fetch --tags && git checkout v0.3.0`, then `python tools/graph.py migrate --yes` (records `"schema": 1` in `vault.config.json` with one commit in the data repo) and `python tools/graph.py doctor`. Push the data repo afterwards. From 0.3.0 on, use `python tools/graph.py update`.
- Do the same on every computer that shares the vault.
- If `vault.config.json` has `project_roots` with a computer-specific path, move it to `.graph/machine.json` on each computer (same key), and remove it from the shared file.
- If your data repo's `.github/workflows/vault.yml` uses `<owner>/vault-engine@main`, change it to `@v0.3.0`; later updates move it automatically.
- `AGENTS.md` in an existing data repo is not updated automatically. Optionally copy two changes from `templates/AGENTS.md`: step 5 (ask before updating) and the remote check in steps 1 and 4.

## [0.2.0] - 2026-09-25

### Added
- `mv <note> <dest>` moves or renames a note and updates `[[links]]`, `weights` keys and learned edges together (`git mv` inside a repo).

### Fixed
- `context` in a project always loads `<project>-status` and `<project>-overview`, outside the token budget like core notes. The project's other notes are ordered by relevance to the task instead of by name, so a long note no longer pushes the status note out.
- The engine or data folder counts as a project when the data repo has `projects/<folder>/` notes, so people developing the engine get its project context and discovery.
- A session started before `setup` does not see `VAULT_ENGINE`: `doctor` now says so and asks for a restart, `setup` asks to restart open terminals and agents, and the `AGENTS.md` template tells agents how to read the values meanwhile.

### Upgrade notes
- No config or data layout changes.
- `AGENTS.md` in an existing data repo is not updated automatically. Optionally copy the new paragraph about an empty `$VAULT_ENGINE` from `templates/AGENTS.md`.

## [0.1.0] - 2026-09-25

First public release.

### Added
- Weighted note graph: `context` / `query` load the notes relevant to a task (keyword + optional local embeddings via Ollama), `reinforce` / `decay` learn from use, `stats` shows whether agents follow the workflow.
- Engine / data split: notes live in the user's own data repo (`VAULT_DATA`); generic notes ship in `defaults/`, a data note with the same path overrides them.
- Project recognition from the working directory and project discovery (`projects`).
- Setup commands: `init` (new data repo from `templates/`), `setup` (environment variables, Claude Code subagents, routing files), `doctor` (health check), `onboard` (profile from a few questions, for terminals and agents).
- Agent-led installation guide for non-technical users (`docs/agent-setup.md`), Turkish quick start (`README.tr.md`).
- Personal-data guard (`guard`, git hooks) and engine `leakcheck`; both also run on GitHub Actions, the guard as a reusable action for data repos.
- Opt-in feedback (`feedback`, off by default).
- Claude Code subagent definitions (`tools/claude-agents/`); Codex detection.
