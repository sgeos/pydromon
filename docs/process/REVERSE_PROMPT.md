# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, external agents over MCP

**Done.** On the operator's direction to let agents outside the daemon, such as another
Claude Code session, communicate with the daemon and its lines through MCP, configurable
from off to full trust with graded permissions between, as `docs/protocol/AGENTS.md`
specifies.

- An external agent is the party `&name`, with its own mailbox. Its messages are
  untrusted data, and a line's wake prompt now says that a sender beginning with `&`
  has no operator authority.
- The levels are `none`, `observe`, `message`, `manage` and `operator`, each including
  the ones before it. The `[agents]` table sets `listen`, off by default, `anonymous`
  and `max_access`.
- `dromon agent add`, `list` and `revoke` register agents through the operator socket.
  A token is printed once and stored only as its SHA-256 digest.
- `dromon mcp --agent NAME` is a stdio MCP server that forwards to a separate agent
  socket, which exists only when `listen` is set and serves only agent operations.
- **Fact, observed live on 2026-10-25.** A Claude Code session given `dromon mcp` sent a
  line a request to reply. The line read it and acknowledged it without replying. When
  the operator directed it, the line replied to the agent.

**Decisions made by this line, open to the operator's review.**

- An `operator` answer is recorded as the reply of the operator the question was asked
  of, with the agent named only in the daemon's output, not in the event log.
- An anonymous agent allowed to message is recorded as a party the first time, under
  the name it gives, which a later registration with a token can claim.
- The MCP server is written by hand rather than with an MCP library, so the Rust port
  can reproduce it, and it speaks three MCP protocol versions.

**Not done.** Pushed notifications to agents, agents from other machines, and a
per-agent list of lines it may message. The peer credential check is still missing.

**Verification.** 865 tests passing outside the sandbox with warnings treated as errors,
and all 9 live tests passing. Ruff and pyright strict clean.
