# Protocol

> **Navigation**: [Documentation Root](../README.md)

The specification shared by every implementation of Dromon. It is drafted here in the
Python prototype and its mature parts are later ported to the lasting `dromon`
repository, as recorded in [`../decisions/NAMING_AND_PORTS.md`](../decisions/NAMING_AND_PORTS.md).
Documents here describe behaviour and formats, never Python details.

- [`EVENT_LOG.md`](EVENT_LOG.md), the data model as events, their JSON encoding and their SQLite storage. Draft, version 1.
- [`CLIENT.md`](CLIENT.md), the requests between `dromon` and `dromond` over the project's Unix domain socket, with the command exit statuses. Draft, version 1.
- [`PRIORITY.md`](PRIORITY.md), wake mute, priority mail with its safe point on each engine and its rate limit, and self mail. Draft, part of version 1, with one hypothesis about Claude Code owed a live check.
- [`RESTART.md`](RESTART.md), restart to upgrade, which drains turns, ends them at a safe point when asked, and starts a successor that resumes every session. Draft, part of version 1, not verified against a live daemon.
- [`DRIVER.md`](DRIVER.md), the interface between a per-model Keleusma driver and its host. Draft, interface 1.
- [`LINE_CONFIG.md`](LINE_CONFIG.md), a session line's built-in tools, MCP servers and allow-all or allowlist tool policy. Draft, version 1.
- [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md), the `.dromon/dromon.toml` file that declares a project's lines, limits and wake policy. Draft, version 1.
- [`AGENTS.md`](AGENTS.md), external agents that reach the daemon and its lines over MCP, with access from off to full trust. Draft, part of version 1.
- [`TEACHER.md`](TEACHER.md), a project's local teacher model served by Ollama, and how Dromon brings it up and releases it. Draft, part of version 1.
- [`GROK.md`](GROK.md), how a line runs on Grok Build over the Agent Client Protocol, with its sandbox profiles and line bridge. Draft, not verified against a live turn.
- [`PI.md`](PI.md), how a line runs on Pi in its RPC mode, with its dedicated agent directory, its extension and the configurations a Pi line refuses. Draft, not verified against a live turn.
- [`CURSOR.md`](CURSOR.md), how a line would run on Cursor Agent in print mode, with the configuration Dromon writes for each turn. Draft, refused in a daemon, and run only by `dromon probe --accept-partial-isolation`.
- [`ANTIGRAVITY.md`](ANTIGRAVITY.md), how a line would run on Antigravity's ACP runtime, `agy_acp_server`, from the protocol's schema and a second implementation. Draft, refused in a daemon since a tool the runtime runs without asking can be stopped only after it starts, and every probe turn refused on its unverified credential unless the operator confirms it by hand.
- [`CODEX.md`](CODEX.md), how a line runs on Codex through its app-server, with its isolation, credentials and the configurations a Codex line may have. Draft.
- [`SETTINGS.md`](SETTINGS.md), a line's engine settings, permission rules, hooks, environment and attribution, with the review of the settings in use that motivated them. Draft, part of version 1.
- [`SANDBOX.md`](SANDBOX.md), the Claude Code sandbox document of a line and a project, its layers, unattended defaults and protected directories. Draft, part of version 1.
- [`ENGINES.md`](ENGINES.md), each engine's capabilities and minimum version as data, and the probe's recordings of the frames it exchanged, which tests replay through the real adapters. Draft.
- [`RELAY.md`](RELAY.md), forwarding messages to operators through adapters for channels such as email or chat. Draft, version 1.
