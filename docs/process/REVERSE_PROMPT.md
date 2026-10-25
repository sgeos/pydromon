# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, ticks, per-line tools and the client direction

**Done, on the operator's three directions.**

- **Ticks.** The scheduler advances in ticks. At the start of a tick it takes a snapshot
  of every undelivered message for an idle, unblocked line under its cap, and each such
  line takes one turn. A message sent during a tick waits for the next tick, even when
  its recipient is idle throughout, and messages never interrupt a turn. The live
  ping-pong of task 5 now takes three ticks. `status` reports the tick.
- **Per-line tools.** `docs/protocol/LINE_CONFIG.md` and `dromon.lineconfig` give each
  line built-in tools, arbitrary MCP servers over stdio, HTTP or SSE, and an
  `allowlist` or `allow_all` policy, plus an opt-in to inherit the account's MCP
  servers. Configurations are stored as `line_configured`, set by `dromon create` or
  `dromon configure`, and apply from the line's next turn on the same engine session.
  A live Haiku line configured with only `Read` read a file and answered correctly.
- **Client.** The daemon keeps the `ask_operator` queue, and the client is to grow a
  graphical interface, or a launcher for one, attachable per line in many terminals at
  once. This is recorded in the design and in roadmap task 8, not built yet.

**Decisions made by this line, open to the operator's review.**

- Listed built-in tools are allowed under the allowlist without being repeated in
  `allowed_tools`, because listing them is already a grant.
- An HTTP or SSE server must use `https`, or `http` on the local host.
- A turn is refused when the engine offers a server the configuration does not name,
  unless the account's servers are inherited.

**Verification.** `uv sync`, 433 tests passing with warnings treated as errors outside
the sandbox. Ruff and pyright strict clean. A live turn exercised a configured built-in
tool.

**Not verified.** A real external MCP server, and `allow_all` or inheritance against the
real engine. MCP credentials are stored in the event log.

**Next.** Task 6, operator questions.
