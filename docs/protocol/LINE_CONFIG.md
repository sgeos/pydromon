# Line Configuration

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, configuration version 1, written on 2026-10-25. Normative. The Python
module `dromon.lineconfig` implements it.

## Purpose

**Decided by the operator on 2026-10-25.** It must be possible to expose arbitrary MCP
servers to a session line, under either an allow-all or a whitelist model, with the
settings held per line. A line's configuration names its built-in Claude Code tools, the
MCP servers it connects to, and which tool calls are allowed.

## Document

```json
{
  "builtin_tools": ["Read", "Edit"],
  "mcp_servers": {
    "github": {"type": "stdio", "command": "gh-mcp", "args": ["--stdio"], "env": {"TOKEN": "..."}},
    "docs": {"type": "http", "url": "https://docs.example/mcp", "headers": {"Authorization": "..."}}
  },
  "tool_policy": "allowlist",
  "allowed_tools": ["mcp__github__*", "mcp__docs__search"],
  "inherit_account_mcp": false
}
```

Every key is optional, and an omitted key takes the default shown in the table. Unknown
keys, duplicate keys and numbers are refused.

| Key | Value | Default |
|---|---|---|
| `builtin_tools` | Claude Code tool names, each a capital letter followed by letters, digits or `_` | none |
| `mcp_servers` | Server name to server, or a Claude Code file of the shape `{"mcpServers": {...}}` | none |
| `tool_policy` | `allowlist` or `allow_all` | `allowlist` |
| `allowed_tools` | Exact tool names, or `mcp__server__*` for every tool of one server | none |
| `inherit_account_mcp` | Also load the MCP servers of the operator's user settings and claude.ai account | `false` |
| `sandbox` | The line's own sandbox document, as [`SANDBOX.md`](SANDBOX.md) specifies, before the project's is merged in. Omitted from the stored JSON when unset | none |
| `settings` | The line's own engine settings, as [`SETTINGS.md`](SETTINGS.md) specifies, before the project's are merged in. Omitted when unset | none |
| `effort` | `low`, `medium`, `high`, `xhigh` or `max`, replacing the driver's effort. Omitted when unset | the driver's |
| `claude_code_prompt` | Run a Claude Code line on Claude Code's own system prompt rather than an empty one. Omitted when false | `false` |
| `instructions` | At most 16 distinct files, such as `CLAUDE.md`, whose text is appended to the system prompt at each turn. A relative path is resolved against the project root and `~` against the home directory. Omitted when empty | none |

A server name is an identifier and may not be `dromon`, which Dromon's own tools use. A
`stdio` server has `command` and optionally `args` and `env`. An `http` or `sse` server
has `url` and optionally `headers`. A URL must use `https`, or `http` on `127.0.0.1` or
`localhost`. At most 256 entries are accepted in any list or mapping, and each text is
at most 4,096 characters.

## Variable references

Any text of an MCP server, its command, arguments, `env` values, url and header values,
may hold `${NAME}`, or `${NAME:-default}` for a default used when `NAME` is unset, as
Claude Code's `.mcp.json` does. The configuration stores the reference, and the engine
replaces it from the daemon's environment at the start of each turn, so a token can stay
out of the event log.

- A `${` that does not begin a well-formed reference is refused when the configuration
  is accepted. So is a reference to a variable `SETTINGS.md` reserves for the engine's
  credential, because an MCP server could otherwise carry that credential elsewhere.
- A url is checked against the transport rule when it is accepted, unless it starts with
  a reference, and again after expansion at every turn.
- A turn whose server refers to an unset variable without a default, or whose expanded
  url breaks the transport rule, is refused before the engine starts.

## Policy

- `allowlist` allows the listed built-in tools, Dromon's own tools such as
  `send_message`, and every tool matching `allowed_tools`, and denies everything else.
- `allow_all` allows every tool the engine offers.
- Dromon approves no tool in advance, and every call the engine would ask about
  reaches the line's permission gate. A line's own `allow` rules and hooks may approve
  calls before the gate, as [`SETTINGS.md`](SETTINGS.md) specifies.
- Unless `inherit_account_mcp` is set, the engine runs with strict MCP configuration,
  and a turn is refused when the engine offers a built-in tool not in `builtin_tools`
  or a tool of a server not in `mcp_servers`.

## Instructions

Dromon starts Claude Code with no settings sources, so Claude Code reads no
`CLAUDE.md` by itself, and without `claude_code_prompt` the SDK passes an empty system
prompt. **Fact, read on 2026-10-25** in the SDK's command construction. The files named
in `instructions` are read at the start of each turn, each under a heading naming it,
and a file that is missing, larger than 256 KiB or not UTF-8 refuses the turn. Only the
paths are stored. On Codex the text becomes the thread's developer instructions, and
`claude_code_prompt` is refused, since Codex uses its own.

## Engines

A line on the Codex or Grok Build engine accepts only the configurations
[`CODEX.md`](CODEX.md) or [`GROK.md`](GROK.md) lists, and any other is refused when the
line is created or configured.

## Storage

The daemon validates a configuration, then appends `line_configured` with its canonical
JSON, which adds `"v": 1` and sorts every key. A new configuration replaces the old one
whole and applies from the line's next turn, which resumes the same engine session.

## Trust

A configuration is operator authority and arrives only through the client. Server
`env` and `headers` may hold credentials. Literal values are stored in the event log,
whose file is readable by its owner only, and references are stored as written. `allow_all` and `inherit_account_mcp` hand a line every
tool they bring, including a claude.ai account's mail and file connectors, and suit
only lines the operator trusts with them.

## Not yet specified

- References to secrets held in files or a keychain rather than the environment.
