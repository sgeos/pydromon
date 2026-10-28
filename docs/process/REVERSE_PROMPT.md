# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-28, roles, provider seats and the first federation steps

**Done, merged in pull requests 57 to 63.**

- **Roles.** The team is restructured into the roles `core`, `engines` and `review`,
  each in its own worktree, with seats on several providers. Write ownership is held,
  relinquished and taken through the harness, and fails over automatically when an
  owner's provider is exhausted, per `docs/architecture/ROLES.md`.
- **Checked live.** A planned handoff of `engines` from an Opus seat to a Codex seat,
  hibernation, priority mail interrupting a turn after a tool result, `dromon
  restart` draining two turns, and Codex's real out-of-credit error.
- **Keleusma.** Both kaizen reviews are answered. In a Dromon line's own sandbox, an
  HTTPS fetch, `gh`, and a real push and deletion of a throwaway branch worked, and the
  credential reads and a second worktree's write were denied.
- **Grok.** The first live probe found that Grok loads the operator's own MCP servers
  and reads Claude Code's permission mode. Dromon now gives Grok its own home and HOME.
- **Federation.** `federation_tls` is merged. Phase 1 on `feat/federation` delivers
  notes between two daemons on one machine, tested outside the sandbox.

**Not verified.** Automatic failover has not fired live. Grok has not run a turn.
Claude Code's limit shapes are typed, not observed.

**For the operator.**
1. Sign in to Dromon's Grok home with the command in `GROK.md`.
2. The Keleusma decisions listed in `KELEUSMA_DOGFOOD.md`.
3. State provider budgets with `dromon budget set`, since no engine reports one.
