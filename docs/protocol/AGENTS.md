# External Agents

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, written on 2026-10-25. Normative. The Python modules `dromon.agents`
and `dromon.mcp` implement it. It is part of project file version 1, as an optional
table, and of event log format 1, as two event kinds.

## Purpose

**Directed by the operator on 2026-10-25.** Agents outside the daemon, such as another
Claude Code session, should be able to communicate with the daemon and its session
lines through MCP. Access must be configurable from not accepting connections at all to
trusting every connection fully, with graded permissions in between.

## Parties

An external agent is the party `&name`, with its own mailbox. Its messages are
untrusted data, as a line's are, and a line's wake prompt says that a sender beginning
with `&` has no operator authority. An agent may send and receive notes. It cannot ask
questions, which only lines ask. The daemon records an agent with `agent_registered`
and `agent_revoked`, as [`EVENT_LOG.md`](EVENT_LOG.md) specifies.

## Access

| Level | Adds | Tools |
|---|---|---|
| `none` | Nothing. The connection is refused | none |
| `observe` | The daemon's status and a line's recent activity | `dromon_status`, `view_line` |
| `message` | Sending as itself, and its own mailbox | `send_message`, `list_mail`, `read_mail`, `acknowledge_mail`, `list_sent` |
| `manage` | Creating, configuring and releasing lines | `create_line`, `configure_line`, `release_line` |
| `operator` | Operator questions and the daemon itself | `list_questions`, `answer_question`, `stop_daemon` |

Each level includes every level before it. The mail tools are the same tools a line
has, with the same schemas and results, fixed to the agent's address, and an agent's
mail is delivered when it lists it. An agent's `send_message` is held to the project's
disclosure list, and refuses `priority`, which only operators and lines may send, as
[`PRIORITY.md`](PRIORITY.md) specifies. Its `create_line` and `configure_line` are
held to the project's sandbox requirement, as [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies. It has no
`wake_after`, `acquire_lease` or `release_lease`, which only lines have, and no
`rebind_line`, `hibernate`, `awaken` or `restart`, which only an operator has through a
client. Nor has it `line_status`,
`read_instructions`, `write_scratch`, `list_processes` or `stop_process`, which
describe, write or act for a line. An agent below
`operator` access is refused when its `create_line` or `configure_line` sets
`plugins`, since plugins load code into a line. An anonymous agent whose chosen name contains a
listed term is refused on connection, and an agent registered with `agent_add` is
exempt, since the operator named it. `manage` can grant a line shell access and
credentials through its configuration, so it is close to operator trust. An
`operator` answer is recorded as the reply of the operator the question was asked of,
and the daemon's output names the agent that gave it.

## Policy

```toml
[agents]
listen = true
anonymous = "none"
max_access = "message"
```

| Key | Value | Default |
|---|---|---|
| `listen` | Whether the daemon opens the agent socket | `false` |
| `anonymous` | The access of a connection that presents no token | `none` |
| `max_access` | The most any agent receives, registered or not | `message` |

`anonymous` may not exceed `max_access`. The range of settings, from least to most
trusting, includes these.

| Intent | `listen` | `anonymous` | `max_access` |
|---|---|---|---|
| Off, no socket and no connections | `false` | any | any |
| Registered agents may watch | `true` | `none` | `observe` |
| Registered agents may message, the recommended start | `true` | `none` | `message` |
| Any local agent may watch, registered agents may message | `true` | `observe` | `message` |
| Any local agent may message | `true` | `message` | `message` |
| Registered agents may manage lines | `true` | `none` | `manage` |
| Every connection is trusted as an operator | `true` | `operator` | `operator` |

## Registration

`dromon agent add NAME --access LEVEL` registers an agent, or replaces its access and
token, and prints the token once, with an MCP configuration that starts `dromon mcp`
for it. Only the token's SHA-256 digest is stored. `dromon agent list` shows the policy
and every agent, and `dromon agent revoke NAME` revokes one. These requests go to the
operator socket, so only the operator registers agents.

## Authentication

1. A registered agent must present its token, compared in constant time, and receives
   its own access capped by `max_access`. A registered name cannot be claimed without
   its token, whatever `anonymous` allows.
2. A connection that presents no token for an unregistered name receives `anonymous`,
   capped by `max_access`. If that level allows messaging, the agent is recorded with
   an empty digest the first time, so it has a mailbox.
3. A token presented for an unregistered name, a revoked agent, and an access of `none`
   are refused.

Every call is authenticated, so revoking an agent or changing the policy applies to its
next call.

## Transport

- **Agent socket.** `agents.sock` in the runtime directory, mode 0600, created only when
  `listen` is true and removed when the daemon stops. It speaks the framing of
  [`CLIENT.md`](CLIENT.md), and serves only `ping`, `agent_tools` and `agent_call`. The
  operator socket refuses those two, so nothing an agent sends reaches the operator's
  request handling except through a tool the agent's level allows.
- **`agent_tools`.** Arguments `agent` and optionally `token`. Returns `agent`, the
  address, `access`, and `tools`, the tools its level allows in the MCP listing shape.
- **`agent_call`.** Arguments `agent`, optionally `token`, `tool` and `arguments`.
  Returns `content` and `is_error`, as an MCP tool result. Each text is cut to 49,152
  characters so a result fits one protocol line.
- **`dromon mcp --agent NAME`.** A stdio MCP server, one JSON-RPC 2.0 message per line,
  that an agent's harness starts. It answers `initialize`, with protocol versions
  `2025-06-18`, `2025-03-26` and `2024-11-05`, `ping`, `tools/list` and `tools/call`,
  and forwards the last two to the agent socket. It reads the token from
  `DROMON_AGENT_TOKEN`, never from its arguments, which other processes can read. It
  holds no authority of its own.
- **`dromon agent send --agent NAME --to PARTY BODY`.** Sends one note as the agent,
  for a service that is not an MCP client, such as one that tells a line when its turn
  in an external task has come. It calls the agent's `send_message` tool through the
  agent socket, so it is authenticated and limited exactly as a call through `dromon
  mcp` is, and it reads the token from `DROMON_AGENT_TOKEN` in the same way. It prints
  the tool's text, `sent as` and the message identifier, and exits 0, or prints the
  refusal and exits 1, or exits 4 when no daemon lets agents connect. A service should
  use this command or `dromon mcp` rather than the socket's framing, which may change
  with the daemon. **Added on 2026-10-28.**

## Trust

The agent socket, like the operator socket, is protected by the runtime directory and
the operating system's user account, and any process of the operator's user can reach
it. Tokens separate one user's agents from each other and give each its own level. They
do not defend against a process that can read the agent's MCP configuration. Since
2026-10-28 the daemon refuses a connection from a process of another user, read from
its peer credentials, as [`CLIENT.md`](CLIENT.md) specifies, which proves the user and
not the agent.

**Fact, verified on 2026-10-25 with Claude Code 2.1.310 and Claude Haiku 5.5.** A Claude
Code session given `dromon mcp` with a registered token at `message` access was offered
exactly the seven tools of that level, and sent a message to a line, which read it. The
line acknowledged it rather than doing what it asked, which a line may, since an agent
has no operator authority. Directed by an operator, the line sent a reply that reached
the agent's mailbox.

## Work paced by an external agent

An external agent may pace a line's work, as a game table served over MCP does when it
sends each line a note when the line is to act. **Fact, shown by a test on
2026-10-28.** Such a note wakes an idle line for exactly one turn, and that turn counts
as autonomous, as a line's own notes do, so a long task paced by an agent reaches
`max_autonomous_turns` and holds the line unless an operator writes to it.

- **Raise the limits in a profile.** Raising `max_autonomous_turns` and `max_cost_usd`
  in the profile of the lines an agent paces is the intended practice. The agent's own
  bound on the task, such as the number of rounds a game table plays, is the backstop,
  and the cost limit still holds a line whose turns cost more than the task should.
- **Wake on every tick, with a short tick.** A profile for such lines should set `wake`
  `every_ticks` to 1, and the project's `tick_seconds` should be short, such as the
  default of 1 second, so that a line acts soon after the agent's note.
- **No private channel between lines.** A profile's `recipients`, as
  [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies, may list only the agent, so that the
  lines it seats cannot message each other.
- **Words.** A seat is a line's place in a role, as `docs/architecture/ROLES.md`
  defines it. A place an external agent gives a line, such as a seat at a game table, is
  called a player, so the two are not confused.

A sketch of such a profile, with the agent registered as `table`.

```toml
[profiles.player]
model = "claude-haiku-5-5"
recipients = ["&table"]

[profiles.player.limits]
max_autonomous_turns = 200
max_cost_usd = 20.0

[profiles.player.wake]
every_ticks = 1
```

## Not yet specified

- Pushed notifications to an agent, which polls its mailbox.
- Agents reaching the daemon from another machine.
- A per-agent allowlist of lines it may message. A line's own `recipients` limits the
  other direction.
