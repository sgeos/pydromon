# Protocol

> **Navigation**: [Documentation Root](../README.md)

The specification shared by every implementation of Dromon. It is drafted here in the
Python prototype and its mature parts are later ported to the lasting `dromon`
repository, as recorded in [`../decisions/NAMING_AND_PORTS.md`](../decisions/NAMING_AND_PORTS.md).
Documents here describe behaviour and formats, never Python details.

- [`EVENT_LOG.md`](EVENT_LOG.md), the data model as events, their JSON encoding and their SQLite storage. Draft, version 1.
- [`CLIENT.md`](CLIENT.md), the requests between `dromon` and `dromond` over the project's Unix domain socket, with the command exit statuses. Draft, version 1.
- [`DRIVER.md`](DRIVER.md), the interface between a per-model Keleusma driver and its host. Draft, interface 1.
- [`LINE_CONFIG.md`](LINE_CONFIG.md), a session line's built-in tools, MCP servers and allow-all or allowlist tool policy. Draft, version 1.
- [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md), the `.dromon/dromon.toml` file that declares a project's lines, limits and wake policy. Draft, version 1.
- [`CODEX.md`](CODEX.md), how a line runs on Codex through its app-server, with its isolation, credentials and the configurations a Codex line may have. Draft.
- [`SETTINGS.md`](SETTINGS.md), a line's engine settings, permission rules, hooks, environment and attribution, with the review of the settings in use that motivated them. Draft, part of version 1.
- [`SANDBOX.md`](SANDBOX.md), the Claude Code sandbox document of a line and a project, its layers, unattended defaults and protected directories. Draft, part of version 1.
- [`RELAY.md`](RELAY.md), forwarding messages to operators through adapters for channels such as email or chat. Draft, version 1.
