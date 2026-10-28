# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-28, federation, the environment allowlist and self-started turns

**Done, merged in pull requests 65 to 68.**

- **Environment.** Every engine starts from an allowlist instead of the daemon's whole
  environment, and a `${NAME}` reference resolves only for a line the project file
  grants it to. A turn that reports it is not signed in now fails.
- **Federation.** Phases 1 and 2 deliver notes between daemons over Unix sockets and
  over TLS 1.3 with pinned Ed25519 keys. External agents send through `dromon agent
  send`, and a line's `recipients` bound whom it may mail.
- **Report findings.** `check_disclosure`, task records, and notes when a turn ends with
  tasks running or when a worktree's `HEAD` moved.
- **Self-started turns.** A turn the engine begins at a task end is admitted as an
  ordinary autonomous turn or interrupted, with refusal tests for each blocker.

**Checked live.** A variable set in the daemon was absent from a line's shell. Two
scratch daemons exchanged a ping and its reply. Automatic failover moved a role from a
Claude seat whose budget was set to zero to its Codex seat, which recorded recovery.

**Found by the reports.** The team's project file named no disclosure list, so no
line's mail or report had been checked. It now does, and a check passed on a report.

**Not verified.** Grok has not run a turn. Claude Code's limit shapes are typed, not
observed. The TLS transport has run only between daemons on one machine.

**For the operator.**
1. Sign in to Dromon's Grok home with the command in `GROK.md`.
2. Decide whether a review line may cite live probes the operator runs.
3. The Keleusma decisions listed in `KELEUSMA_DOGFOOD.md`.
4. State provider budgets with `dromon budget set`, since no engine reports one.
