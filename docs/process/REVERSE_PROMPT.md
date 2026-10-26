# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, settings for lines and projects

**Done.** On the operator's direction to take the daemon and client pair to near parity
with Claude Code, Codex and Grok Build, with settings for session lines and projects,
and with the core, platform and entry point layering in mind.

- **Layers.** Settings have a project scope and a line scope. What an operator changes
  from a client is an override, recorded in the event log by the new
  `line_overridden` and `project_overridden` events and layered over the project file,
  which the daemon never writes. The command line of `dromond` still wins, and a value
  it shadows is reported as such.
- **Pure modules.** `dromon.overrides` holds the keys, kinds and validation, and
  `dromon.configview` the layers and the entries a client shows. The client core reads
  typed text by the kinds the daemon reports, so it holds no list of settings.
- **Client.** `/config` opens a settings panel with each value's layer, and `/model`,
  `/effort`, `/set` and `/unset` change values, with completion. `/clear` starts a new
  engine session, `/create` makes a line and `/reload` applies the project file.
- **Daemon and command line.** The `settings`, `set_setting`, `clear_session` and
  `reload_project` operations, the `line_session_cleared` event, and `dromon settings`,
  `dromon set` and `dromon reload`.

**A defect found and fixed during the work.** An out-of-range `daemon.tick_seconds`
passed the first check and would have been recorded before the scheduler refused it.
The range checks now run before anything is recorded, and a test covers it.

**Verification.** The full gate passed outside the sandbox with 1169 tests. The curses
client and the new subcommands were driven against a daemon with wakes held, through
model and effort changes, a project override, a refused value, a session clear, a
line creation, the settings panel and a reload. No live turn was run. A model switch
reaching a real engine is inferred from the scripted tests, not observed.

**Decisions for the operator to confirm.** A line's strategy cannot be changed from a
client, because it decides per-token billing. MCP servers, sandbox and engine settings
documents, the teacher and the agent policy stay file-only. A reload refuses changes
to the last four of those, which apply only at a restart. External agents over MCP
have no settings tools yet.
