# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, nominal Grok support

**Done, not verified live.** On the operator's direction to add nominal Grok support,
with Grok usage exhausted so that it cannot be tested yet, as `docs/protocol/GROK.md`
specifies.

- Drivers for Grok 4.7 on a new `grok-build` engine, in the three strategies, with the
  context and efforts from Grok's model cache.
- `dromon.grok` drives `grok agent --no-leader stdio` over the Agent Client Protocol.
  Its design rests on Grok Build 1.0.70's bundled guide and on one `initialize`
  handshake, which runs no model. No session was created and no prompt was sent.
- **Fact.** Grok's custom sandbox profiles live only in `sandbox.toml` files, which the
  injectable overlay cannot set. Dromon therefore requires the operator to define
  `dromon-reader` and `dromon-writer`, checks before each turn that they deny Dromon's
  directories and that a writer keeps `.git` read-only, and selects one with
  `GROK_SANDBOX`. A missing or unsafe profile refuses the turn with the table to add.
- **Fact.** The operator's Grok configuration sets always-approve and declares mail and
  document MCP servers. Dromon asks for Grok's ask mode, refuses a session that does
  not report one, answers every permission request, and cancels a turn that uses a tool
  the line was not given.
- Dromon's tools reach a Grok line through `dromon mcp --line`, on a new `lines.sock`,
  with a per-line token the daemon holds in memory.

**Decisions made by this line, open to the operator's review.**

- Dromon does not write either `sandbox.toml`. The operator adds the profiles.
- The API strategy is refused, because Grok prefers a cached sign-in over a key and
  Dromon cannot yet confirm which it used.
- Every unconfirmed behaviour refuses the turn. The first live run may therefore fail
  closed on a field shape and need an adjustment, and `GROK.md` lists what to check.

**Not done.** No live verification of any Grok turn. On macOS Grok cannot block a
command's network access.

**Verification.** 931 tests passing outside the sandbox with warnings treated as errors,
including 33 for the Grok adapter against a scripted server. Ruff and pyright strict
clean. The live tests were not rerun, since nothing they cover changed.
