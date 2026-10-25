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
mail is delivered when it lists it. `manage` can grant a line shell access and
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

## Trust

The agent socket, like the operator socket, is protected by the runtime directory and
the operating system's user account, and any process of the operator's user can reach
it. Tokens separate one user's agents from each other and give each its own level. They
do not defend against a process that can read the agent's MCP configuration, and the
daemon still has no peer credential check.

**Fact, verified on 2026-10-25 with Claude Code 2.1.310 and Claude Haiku 5.5.** A Claude
Code session given `dromon mcp` with a registered token at `message` access was offered
exactly the seven tools of that level, and sent a message to a line, which read it. The
line acknowledged it rather than doing what it asked, which a line may, since an agent
has no operator authority. Directed by an operator, the line sent a reply that reached
the agent's mailbox.

## Not yet specified

- Pushed notifications to an agent, which polls its mailbox.
- Agents reaching the daemon from another machine.
- A per-agent allowlist of lines it may message.
