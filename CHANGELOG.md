# Changelog

All notable changes to vault-engine. Versions follow [SemVer](https://semver.org/); while the version is 0.x, minor releases may include breaking changes, listed under **Upgrade notes**.

## [0.3.0] - 2026-09-25

### Added
- `update [--check] [--to TAG] [--yes] [--apply-agents]` moves a stable install (engine checked out at a release tag) to another release. It fetches tags, shows the CHANGELOG sections in between, asks for confirmation, checks out the tag, then runs the new version's `migrate`, `setup` and `doctor`. A dirty checkout, and a downgrade below the vault's data schema, are refused. A development checkout (on a branch) is left alone.
- Release check on the stable channel: at most once a day after `reinforce`, the engine reads the newest tag with `git ls-remote` (nothing is sent). `doctor` shows a WARN and `context` one hint line when a newer version exists. Turn it off with `"updates": {"check": false}` in `vault.config.json` or `.graph/machine.json`. `doctor` also shows the channel.
- Vault data schema: `vault.config.json` gets `"schema"` (missing means 1). An engine older than the vault refuses commands that write shared data (learned edges, `mv`, `onboard`, `init`, `feedback set`), while `context` keeps working. `migrate [--yes]` upgrades an older vault and commits only the files it changed.
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
- **First update from 0.2.0 is manual** (0.2.0 has no `update` command). In the engine folder: `git fetch --tags && git checkout v0.3.0`, then `python tools/graph.py migrate --yes` (records `"schema": 1` in `vault.config.json` with one commit in the data repo) and `python tools/graph.py doctor`. Push the data repo afterwards. From 0.3.0 on, use `python tools/graph.py update`.
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
