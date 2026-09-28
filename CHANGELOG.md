# Changelog

All notable changes to vault-engine. Versions follow [SemVer](https://semver.org/); while the version is 0.x, minor releases may include breaking changes, listed under **Upgrade notes**. A release that raises the vault data schema says so there (`schema N`) and asks to bring every computer that shares the vault to 0.3.0 or later first: 0.1.0 and 0.2.0 have no schema check.

## [0.8.0] - 2026-09-28

### Added
- Status line gains segments for the active model, thinking effort, current folder and git branch, and an estimated session cost, alongside the existing context usage.
- `claude-hooks` gains a delegation-warn hook that reminds the orchestrator to delegate to a subagent instead of doing the work itself once it has made 4 or more tool calls in a turn.
- `models` command and a single model-selection config: `agent-guard` now derives its allowed models from this config instead of a hard-coded list, and `doctor` warns when Claude Code's settings or worker files deviate from it. Without a `models` config the built-in defaults apply.
- `update --models` step and a context notice for Claude Code hooks or model settings that are stale (installed before a hooks/model change).
- `update --agents` step and a context notice for worker agent definition files (`worker-low.md`, `worker-medium.md`) that are stale.

### Changed
- Worker agent definitions (`worker-low`, `worker-medium`) gain explicit working rules (branch check before editing, commit after each logical step, never pipe test output through `tail`/`head`, commit and report when low on turns).
- `worker-medium`'s `maxTurns` raised from 60 to 100.
- The default orchestration standard (`defaults/standards/orchestration.md`) gains rules on waiting for and resuming subagents and on turn limits.

### Fixed
- Windows: `claude-hooks --install` wrote hook and status line commands with single quotes, which failed to parse in the Windows shell, so the hooks never ran; commands are now quoted so they work on Windows.

### Upgrade notes
- Run `python "$VAULT_ENGINE/tools/graph.py" update --claude-hooks --models --agents` to install or refresh the Claude Code hooks, model settings and worker agent files.
- Windows users who installed the Claude Code hooks before this release had broken (single-quoted) hook commands; the update above fixes them.
- `templates/AGENTS.md` is now template revision 7; translated or otherwise edited copies should be merged by hand, `update --apply-agents` replaces the file outright.
- `agent-guard` behavior is unchanged unless a `models` config has been set.
- No vault data schema change in this release.

## [0.7.0] - 2026-09-28

### Added
- `projects --ask`: a read-only report for the agent listing discovered projects without vault notes, with instructions to ask the user which should get notes (and whether a folder outside the list, such as one that is not a git repo, should be included), then write real overview/status notes by hand. The engine never writes project notes or skeletons.
- `projects --skip NAME...` / `--unskip NAME...`: a per-computer "don't ask" list (`skip_projects` in the gitignored `.graph/machine.json`). Unlike the shared `exclude` patterns in `vault.config.json`, which hide a project on every computer, a skipped project still shows in `projects` (marked skipped) and is only left out of `--missing`, `--ask`, `context` and `doctor` on this computer. `--json` entries gain `"skipped"`.
- `setup` gains a "Projects:" section that runs discovery and prints the next step (`projects --ask`) when projects without notes are found; `--no-projects` skips it.

### Changed
- `context` points at `projects --ask` when the current project has no notes (silent once skipped), and, outside any project, prints one line listing discovered projects without notes, read from the discovery cache only (it never scans).
- `doctor`'s "projects without notes" warning ignores skipped projects and points at `projects --ask` / `projects --skip <name>`.
- `templates/AGENTS.md` step 1 adds the project selection rule (template revision 5); the full procedure is in `defaults/standards/agents-details.md` and `docs/agent-setup.md`.

### Known limitations
- Projects are recognised by folder name: two different projects with the same folder name on two computers share one set of notes.

### Upgrade notes
- `update` runs `setup`, so the new "Projects:" step appears on its own; follow it (run `projects --ask` and answer which projects to add) or skip projects with `projects --skip <name>`.
- Run `python tools/graph.py update --apply-agents` to pick up `AGENTS.md` template revision 5; `doctor` warns while the file is behind. The `context` hint works without it.
- No vault data schema change in this release.

## [0.6.0] - 2026-09-28

### Added
- `update` offers the Claude Code hooks (agent-guard, context-warn, status line) after a successful switch, and when already up to date, if they are missing or point at an old path and this computer has a `~/.claude` folder. An interactive yes or the new `--claude-hooks` flag runs `claude-hooks --install`; `--yes` alone never writes `settings.json` and only prints the command.
- `VAULT_SKIP_DIRS` (comma-separated folder names) adds folders to skip when the engine scans notes, on top of the built-in `.git`, `.obsidian`, `.graph`, `tools` and `__pycache__`.

### Changed
- `templates/AGENTS.md` is ~40% shorter (template revision 4): the Windows variable spelling, the `$VAULT_ENGINE`-empty fallback, the full onboarding procedure and the no-Python fallback moved to a new default note, `defaults/standards/agents-details.md`. All steps and their commands are unchanged.
- The default `orchestration` note gains the token-economy delegation rules: delegate to a subagent by default, the small-job exception (one short tool call stays with the orchestrator), counting per request rather than per step, and subagents running on a cheaper model than the orchestrator's.

### Experimental
- `stats --tokens` gains a Codex section (text, and `"codex"` in `--json`): session count, token totals, totals per model and the newest rate limits, read from `~/.codex/sessions` (`--codex-dir`). Read-only, no USD estimate. Codex support is experimental: the output format and fields may change. Known gap: Codex Desktop sessions record only a total, shown under model `unknown`.

### Upgrade notes
- Run `python tools/graph.py update --apply-agents` to pick up the shorter `AGENTS.md` (revision 4); `doctor` warns while the file is behind. A translated copy should keep the template line at the end.
- If the Claude Code hooks are not installed yet, `update` now asks; in a non-interactive session run `python tools/graph.py claude-hooks --install` (or `update --claude-hooks`) and start a new Claude Code session.
- No vault data schema change in this release.

## [0.5.0] - 2026-09-27

### Added
- Worker agent templates (`worker-low`, `worker-medium`) gained token-saving rules: read large files with Grep/targeted Read first, keep test/command output and reports short, and stop to propose a split instead of continuing if a task is too large for one run.
- Worker agents now run with a restricted tool allowlist (`Read, Grep, Glob, Bash, Edit, Write`, plus `WebFetch, WebSearch` for `worker-medium`) and a `maxTurns` cap (40 for `worker-low`, 60 for `worker-medium`); neither can start subagents.
- `stats --tokens [--projects-dir] [--since YYYY-MM-DD] [--top N] [--json]` reports Claude Code transcript token usage: totals by model, main sessions vs subagents, top sessions, sessions over a high peak-context threshold, median first-call context, subagent turn distribution, and a list-price USD estimate. Read-only; prints only numbers and session/agent ids.
- `claude-hooks [--settings PATH] [--install]`: opt-in installer for two hooks. `agent-guard.py` (PreToolUse on `Agent`) denies starting a subagent that is not a `worker-*` type or that requests a model other than `sonnet`/`haiku` (`VAULT_AGENT_GUARD=off` disables it). `context-warn.py` (UserPromptSubmit) warns once context passes a threshold (`VAULT_CONTEXT_WARN`, default 150000) to save state and start a new session or `/compact`, then every +25K after. A status line script prints current context usage. `doctor` warns when the hooks are not installed.

### Upgrade notes
- After updating, run `python tools/graph.py claude-hooks --install` to enable the agent-guard and context-warn hooks and the context status line; `doctor` warns if they are still missing. Start a new Claude Code session afterwards so the updated worker agent definitions (tool allowlist, `maxTurns`) and hooks take effect.

## [0.4.0] - 2026-09-26

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
