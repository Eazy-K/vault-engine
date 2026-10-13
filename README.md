# vault-engine

A second brain for AI coding agents ([Claude Code](https://claude.com/claude-code) and [Codex](https://openai.com/codex)). Your notes form a weighted graph, and the agent loads only the notes that matter for the task at hand.

The engine is public and contains no personal data. Your notes live in a separate **data repository** that you own.

*[Türkçe için README.tr.md](README.tr.md)*

## Quick start

You need an AI coding agent installed and logged in. Paste this to it:

> Install vault-engine for me: read docs/agent-setup.md in https://github.com/Eazy-K/vault-engine and follow it step by step, asking me before each change.

The agent checks what is on your computer and asks before installing anything or creating any account. It creates two folders (the engine and your notes), can set up a *private* GitHub backup of your notes, and asks a few short questions about how you like to work: language, level of detail, and when it should double-check with you. It explains each step first. You only answer yes or no.

## How daily use works

You normally type nothing. When you start a task, the agent:

1. pulls the latest notes from your data repo,
2. loads the notes that fit the task (it follows links between notes and learns which links help),
3. does the work and writes down what it learned,
4. marks the notes that were useful, so they rank higher next time,
5. commits and pushes the notes.

Unused links fade slowly, so the graph keeps up with how you work. Your notes are checked before every commit so that secrets and personal data do not leak.

## Updating, and another computer

When a new version exists, the agent tells you once. You approve, and it runs the update, which also migrates your notes, refreshes the agent setup and checks the result. On a second computer, pull your data repo and run the same update there:

```
python <engine>/tools/graph.py update --all
```

On Codex, approve changed hooks again with `/hooks` after an update.

Never update the engine by hand with `git pull`. Details: [Versions and updates](docs/commands.md#versions-and-updates).

## More

- [All commands](docs/commands.md): setup, projects, feedback, CI, versions, troubleshooting.
- [Agent setup guide](docs/agent-setup.md): the steps your agent follows.
- [Contributing](docs/commands.md#contributing-to-the-engine): enable the leak check, add tests.
- [Changelog](CHANGELOG.md).
- License: MIT, see LICENSE.
