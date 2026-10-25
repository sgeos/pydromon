# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, configuration parity with the settings in use

**Done.** On the operator's direction to review the settings actually used on this
machine with Claude Code, Codex and Grok Build, and to reach functional parity for them,
configurable per line and per project.

- **Fact.** The user and project configuration of the three harnesses was read with
  credential values redacted and credential files left unread. The settings found, by
  key, and how Dromon provides each, are tabled in `docs/protocol/SETTINGS.md`. Paths,
  project names and values are not recorded in tracked files.
- New engine settings, in Claude Code's key names, cover permission rules, command
  hooks, `env` and `attribution`. They layer per project, profile and line, or come from
  `--settings-config FILE`, a Claude Code settings file, and reach Claude Code through
  its `--settings` flag.
- A line's `effort` replaces its driver's, matching Codex's reasoning effort setting.
- MCP servers may refer to the daemon's environment as `${NAME}`, matching Codex's
  bearer token variable, so a token can stay out of the event log.

**Decisions made by this line, open to the operator's review.**

- `ask` rules deny, because a question never pauses a line. Grok Build's `ask` rules
  therefore refuse rather than prompt.
- Every line's built-in file tools are denied `.dromon` and the runtime directory,
  whether or not its sandbox is enabled.
- `env` may not set credential or provider variables, and MCP references may not name
  them.
- Hooks are accepted from the committed project file, on the reasoning that it can
  already declare stdio MCP servers, which run commands too.

**Not at parity.** Claude Code's `auto` mode and Codex's automatic reviewer, plugins and
language servers, and MCP OAuth. Each is explained in `SETTINGS.md`.

**Verification.** 720 tests passing outside the sandbox with warnings treated as errors,
and all 6 live tests passing on Haiku 5.5. The two new live tests showed that a hook
runs with the line's `env` and reaches the model, and that an `ask` rule refuses a Bash
call while an unmatched one runs. Ruff and pyright strict clean. Linux was not exercised
live.
