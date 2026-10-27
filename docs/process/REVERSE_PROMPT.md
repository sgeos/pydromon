# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-26, stop policies and a process launcher

**Done.** On the operator's decisions of 2026-10-26, from the review of T3 Code.

- **Settings.** `stop_policy` chooses `kill_all`, the default, or `leave_background`.
  `keep_session_at_max` chooses `close`, `ask` or `keep` at the keep-session ceiling,
  always under `keep_session_hard_seconds`.
- **Launcher.** Every harness runs under `dromon-launch`, which ends it with the daemon.
  A daemon sweeps the group of any harness whose launcher died, at start-up and at
  each rest of at most 60 seconds.
- **Outcomes.** An interrupt holds the line and closes a session still running work.
  A resume handoff is not a turn. Tool search is pinned. Line tokens live as long as
  the engine process. Recorded commands are redacted.
- **Work.** The Opus `worker` line wrote it over five rounds, the Haiku `reviewer`
  line reviewed it twice, and the operator's session checked it live four times with
  Claude Haiku 5.5, which found three faults the tests had not.

**Verified live on macOS.** A turn through the launcher, the daemon killed with
`SIGKILL`, an ordinary stop under each policy, an interrupt, a resumed session after a
restart, and the launcher alone killed, swept within 57 seconds.

**Not verified.** The Linux subreaper and parent-death signal, a background command in a
session of its own, and a `~/.claude.json` that sets tool search. The verify-first list
is in `LINE_CONFIG.md`.

**For the operator.** Under `leave_background` Claude Code is killed without an orderly
exit, since a closed input makes it end its own background work.
