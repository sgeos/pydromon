# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, task 2, the data model and event log

**Done.** Session lines, messages, wakes and operator questions are typed records built
by ten event kinds, stored in an append-only SQLite log, and a replay rebuilds the same
state. The specification is `docs/protocol/EVENT_LOG.md`.

**Decisions made by this line, open to the operator's review.**

- **State is never stored.** It is rebuilt from events, so the event list is the data
  model, as the design proposed.
- **Identifiers are never reused.** This makes a resent event harmless, which the design
  requires of messages.
- **A blocking question pauses its own line only.** It refuses that line's next turn and
  wakes and affects nothing else.
- **Answers are free text** even when a question offers choices.
- **The log does not authenticate.** The daemon attributes each event before appending.
  File permissions are the security boundary, and new logs are owner-only.
- **The encoding is canonical JSON**, pinned byte for byte in tests, so the Rust and
  Keleusma ports can be checked against the same bytes.

**Verification.** `uv sync`, 145 tests passing with warnings treated as errors, ruff
check and format clean, and pyright strict with 0 errors and 0 warnings. The tests cover
every event kind through one scenario, every rejection, every malformed-payload class,
replay equality, append-only triggers, a second writer and three forms of corruption.

**Not verified.** Only Python 3.12.12 with SQLite 3.50.4 on macOS was tested. Concurrent
access was tested with two connections in one process, not with two processes.

**Next.** Task 3, the daemon skeleton.
