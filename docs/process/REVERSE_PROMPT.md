# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-11-01, round 14, address all known issues

**Done, merged in pull requests 123 to 134.**

- **Team.** A new `runtime` role on Claude Sonnet 5.5 for processes and the sandbox,
  and the `full-gate` run declared, which writing lines started 36 times.
- **Engines.** Codex writer conflicts recover to a new thread. Grok sessions renew
  when rules or tools change, its model pin sends a bare value, and a PreToolUse hook
  and documented switches withhold Grok's own tools. A disallowed Codex call is
  refused alone.
- **Confinement.** The ViewImage rule now runs as a hook on every Claude Code `Read`,
  after a live check found in-directory reads bypassed it. Line temporary directories
  are private, and `worktree_status` reads git from outside the sandbox. A roster
  oracle on the disclosure list is closed. Credentials are sealed out of the log.
- **Mail and roles.** Replies close what they answer, `@operator` resolves, a turn
  ends when its role ownership changes, and leases can be listed.
- **Operator.** Commit runs for reading lines, `retire`, `renew`, `run status`,
  `sync-worktrees` without running a line's interpreter, and a restart that reports
  both commits.
- **Hotfixes.** Two regressions stopped every Grok line and then every Codex line.
  Both were found by a live turn and fixed in #128 and #130.

**Verified live.** ViewImage on Claude Code and Codex before and after the fix,
`read_only_paths` on both, a line's temporary directory, the Grok tool list before and
after the switches, five supervised runs through a restart, and the event log's start
time. Full gates outside the sandbox and continuous integration on every merge.

**Not verified.** The items under Unverified live in `HANDOFF.md`.

**Also this round.** The operator merged #128 with one failing check. That was a flake,
and every later merge tested the counts first. Six of eight lines are satisfied. The
operator is not, and names a no-cost live check of each engine's launch arguments as
the change that would most move it.
