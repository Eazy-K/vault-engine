# Changelog

All notable changes to vault-engine. Versions follow [SemVer](https://semver.org/); while the version is 0.x, minor releases may include breaking changes, listed under **Upgrade notes**.

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
