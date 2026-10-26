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
| `project_settings` | Also load Claude Code's project setting source in the line's working directory, as [`SETTINGS.md`](SETTINGS.md) specifies. Refused on Codex and Grok lines. Omitted when false | `false` |
| `instructions` | At most 16 distinct files, such as `CLAUDE.md`, whose text is appended to the system prompt at each turn. A relative path is resolved against the line's working directory and `~` against the home directory. Omitted when empty | none |
| `workdir` | The directory the line's engine runs in, as the working directory section below specifies. Omitted when unset | the project root |

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

## Dromon's own tools

Every line has Dromon's own tools, whatever its configuration, and each fixes its
sender to the line. External agents have a subset, as [`AGENTS.md`](AGENTS.md)
specifies. Their arguments are untrusted input and are checked strictly, and an
argument outside a tool's schema is refused.

| Tool | Effect |
|---|---|
| `send_message` | Sends a note to a line or an operator, delivered in a later tick |
| `ask_operator` | Asks an operator a question, which never pauses the line |
| `wake_after` | Schedules one wake for the line, as below |
| `list_mail`, `read_mail`, `acknowledge_mail` | Page through, open and acknowledge the line's mailbox |
| `list_sent` | Lists what became of the line's sent mail |
| `acquire_lease`, `release_lease` | Take and give up a lease the project declares, as below |

**Leases.** **Decided by the operator on 2026-10-26.** A project declares named leases
in its `[leases]` table, as [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies, so its
lines can take turns at a shared resource, such as a machine that runs one full gate at
a time.

- **`acquire_lease`.** Argument `name`. It succeeds when no line holds the lease, and
  answers with the lease's end, the wall clock in Coordinated Universal Time plus the
  project's time to live, rounded up to the whole second. It is refused while any line
  holds it, naming the holder and when that lease ends, including the line itself, so
  one acquisition lasts at most one time to live. The holder may acquire it again once
  it has expired. Expiry follows the daemon's wall clock, so a change of that clock
  moves it. A name the project does not declare is refused, listing the declared
  names.
- **`release_lease`.** Argument `name`. It ends the line's lease, and is refused for a
  lease the line does not hold.
- **Ends.** A lease ends when released, when its time to live passes, when its holder's
  engine session is cleared, whether by `clear_session` or by a change of its
  `workdir`, when its holder is rebound, and when a reload stops declaring its name.
- **Memory only.** Leases live in the daemon's memory and are not events, so a restart
  drops every lease. A resource a stopped daemon's lines were using is then no longer
  guarded, which suits a resource such as a gate whose processes the operator restarts
  too. A line that needs the lease after a restart acquires it again.
- **Visibility.** `status` and `line_view` show the leases each line holds.
- **Agents.** External agents have no lease tools.

**`wake_after`.** **Decided by the operator on 2026-10-26.** A line that starts a long
command in the background, which survives the end of its turn, needs a way to be woken
to read the result.

- **Arguments.** `seconds`, a whole number from 60 to 86,400, and `note`, text of at
  most 2,000 bytes. A number with a fraction, a boolean or text is refused.
- **Effect.** The daemon appends `wake_scheduled` with the due time, the wall clock in
  Coordinated Universal Time plus `seconds`, rounded up to the whole second so a wake
  never fires early. A line has at most one pending scheduled wake, and a new call
  replaces it, which the tool's answer reports with the replaced due time.
- **Firing.** At the first tick at or after the due time, the note is recorded as a
  message from the line to itself, as [`EVENT_LOG.md`](EVENT_LOG.md) specifies under
  scheduled wakes. It wakes the line through the ordinary path in a later tick, counts
  toward the line's autonomous-turn limit like any mail that is not from an operator,
  and waits like other mail while the line is held. A wake that fell due while the
  daemon was down fires at its first tick. A note that the disclosure list in force at
  firing covers is replaced by a fixed notice, as [`EVENT_LOG.md`](EVENT_LOG.md)
  specifies, so a list changed by a reload or a restart still applies.
- **Rest.** While nothing else is in flight, the daemon's loop rests until the next
  wake falls due, for at most 60 seconds at a time, so a change of the wall clock
  delays a wake by at most that much.
- **Visibility.** `status` and `line_view` show the pending wake's due time and the
  identifier its note will carry, as [`CLIENT.md`](CLIENT.md) specifies.

**Disclosure guard.** When the project names a disclosure list, `send_message`,
`ask_operator` and `wake_after` refuse a body, question, choice or note that contains a
listed term, as [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies, and record nothing.
A wake note is checked again when it fires.

## Instructions

Dromon starts Claude Code with no settings sources, so Claude Code reads no
`CLAUDE.md` by itself, and without `claude_code_prompt` the SDK passes an empty system
prompt. **Fact, read on 2026-10-25** in the SDK's command construction. The files named
in `instructions` are read at the start of each turn, each under a heading naming it,
and a file that is missing, larger than 256 KiB or not UTF-8 refuses the turn. Only the
paths are stored. On Codex the text becomes the thread's developer instructions, and
`claude_code_prompt` is refused, since Codex uses its own.

## Working directory

**Decided by the operator on 2026-10-25.** Each line's engine runs in its own working
directory, so that a line can work in a git worktree of its own while the daemon's
state stays in the project.

- **Resolution.** `workdir` is text of 1 to 4,096 characters without NUL. A relative
  path is resolved against the project root, and `~` or `~/` against the home
  directory. Another user's home, such as `~ana/`, is refused. Only the path is stored.
- **Checks.** After symbolic links are resolved, the directory must exist. It may not
  be the project's state directory or runtime directory or lie inside either. It may
  not contain either, unless it is the project root itself, which contains the state
  directory by default and is protected as every line's root is. A configuration that
  fails a check is refused when it is created, configured or read from the project
  file, and a turn whose directory has since vanished or moved under a protected
  directory ends before its engine starts.
- **Default.** Without `workdir` a line runs in the project root, as before, and its
  configuration encodes exactly as it did before the key existed.
- **What follows the directory.** Claude Code's working directory, the Codex workspace
  and its `:workspace_roots`, and the Grok agent's working directory. A line's relative
  `instructions` paths, and the relative paths of its sandbox document, resolve against
  it. Permission rules in its settings are interpreted by Claude Code against the same
  working directory. The project's state and runtime directories stay protected for
  every line wherever it runs.
- **Sessions.** Claude Code keeps a session under the directory it ran in. When a
  line's `workdir` changes, by `configure_line` or by the project file at start or
  reload, the daemon appends `line_session_cleared`, so the line's next turn starts a
  new session. A turn in flight at the change does not record its session. The
  comparison is of the stored text, so writing the same directory another way also
  starts a new session, including a first explicit `workdir` that names the project
  root. Other configuration changes keep the session.
- **Limit, a re-pointed link.** Because only the text is compared, a `workdir` that
  names a symbolic link keeps its text when the link is re-pointed to another
  directory. No `line_session_cleared` is appended, and the line's next turn resumes a
  session from the old directory, which Claude Code is then expected not to find. The
  directory itself is checked again when the engine is built, so the new target must
  still be allowed. An operator who re-points such a link can send `clear_session`.
- **Clients.** A client cannot change `workdir` through `set_setting`. The settings
  view shows it as an `info` entry, and `configure_line`, which replaces the whole
  configuration, is the way to change it. A `configure_line` without `workdir` returns
  the line to the project root.
- **Git safety.** Every enabled Claude Code sandbox denies writes to the hooks and
  configuration of the git checkout the line works in, as [`SANDBOX.md`](SANDBOX.md)
  specifies.

**Fact, observed on 2026-10-26 with Claude Code 2.1.310.** Claude Code stores a
session under a directory of `~/.claude/projects/` named for the working directory. A
Haiku line with its workdir in a linked worktree had its session stored under that
worktree's name, so a session resumed from another directory would not be found.

## Engines

A line on the Codex or Grok Build engine accepts only the configurations
[`CODEX.md`](CODEX.md) or [`GROK.md`](GROK.md) lists, and any other is refused when the
line is created or configured.

## Storage

The daemon validates a configuration, then appends `line_configured` with its canonical
JSON, which adds `"v": 1` and sorts every key. A new configuration replaces the old one
whole and applies from the line's next turn, which resumes the same engine session
unless the line's `workdir` changed.

## Trust

A configuration is operator authority and arrives only through the client. Server
`env` and `headers` may hold credentials. Literal values are stored in the event log,
whose file is readable by its owner only, and references are stored as written. `allow_all` and `inherit_account_mcp` hand a line every
tool they bring, including a claude.ai account's mail and file connectors, and suit
only lines the operator trusts with them.

## Not yet specified

- References to secrets held in files or a keychain rather than the environment.
