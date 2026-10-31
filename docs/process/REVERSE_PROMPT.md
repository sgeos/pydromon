# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-31, rounds 9 to 11

**Done, merged in pull requests 97 to 110.**

- **Confinement.** Codex and Grok lines are denied a shared list of credential paths
  and the project's secret directories by real path. A live Codex line turn was refused
  `secret/`, `~/.ssh` and `~/.codex/auth.json`. `read_only_paths` makes paths in every
  line's workdir unwritable on each engine, and this project now protects
  `docs/process` and `docs/roadmap` with it.
- **Grok.** Tool calls are judged by Grok's own `_meta` name and kind, Dromon's tools
  are reached through `use_tool`, and the driver's model is requested per session. A
  Grok line in another project ran turns of 58 and more tool calls on the result.
- **Team.** Roles can be created, updated and deleted at run time, under one writer
  per worktree. The `capabilities` role was created that way on a new Claude Sonnet 5.5
  driver. Read lines get their own scratch.
- **Operator.** Mail is pushed through a file relay that the operator follows, lines
  raise issues, and `dromon wait` waits for a reply without polling.
- **Durability.** Leases survive a restart and pass with a role, and declared commands
  run under a supervisor that a successor and a restart reconcile.
- **Gaps from another project's planned team.** `ViewImage` on Claude Code, Codex and
  Grok, budget windows for Claude Code, and no Grok quota classification without a
  documented shape.

**Verified.** Full gates outside the sandbox and continuous integration on every pull
request, with reviews and re-reviews by the review line. Probe turns on Sol, Terra and
Sonnet 5.5, and a live Codex confinement check.

**Not verified.** The items under Unverified live in `HANDOFF.md`.

**Also this session.** A restart onto the one-writer rule left no daemon for about
eight minutes, since two log-only legacy lines could write the project root. They are
declared read-only in the project file now. Every line and the operator wrote an
experience report, and two of four lines are satisfied.
