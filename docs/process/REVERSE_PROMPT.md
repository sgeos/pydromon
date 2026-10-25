# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, task 3, the daemon skeleton

**Done.** Tasks 1 and 2 were already complete. Task 3 is complete.

- `dromond` serves one project on a Unix domain socket in a runtime directory that must
  be owned by the user with mode 0700. It holds the event log as its single writer and
  admits one instance through an exclusive lock.
- `dromon start` spawns a detached daemon in its own session and waits for it to answer.
  `dromon status` reports the daemon and event log counts, and `dromon stop` stops it and
  waits for the socket to go. Exit statuses are distinct and listed in `CLIENT.md`.
- The protocol is versioned, line-delimited canonical JSON with a 64 KiB line limit,
  strict decoding, a per-connection idle timeout and error codes, specified in
  `docs/protocol/CLIENT.md`.
- The operator's decision that replies arrive through inbound adapters plugged into a
  daemon registry and configured per channel is recorded in `RELAY.md`, with the design
  deferred.
- Pull request #6, the response path requirement, was merged first.

**Decisions made by this line, open to the operator's review.**

- `dromon start` succeeds without action when a daemon already answers, while `status`
  and `stop` exit with status 4 when none does.
- The runtime directory defaults to `.dromon/run` in the project and moves with
  `DROMON_RUNTIME_DIR`, because socket paths are limited to 103 bytes.
- Access control rests on the directory's mode. A peer credential check is deferred.

**Verification.** `uv sync`, 245 tests passing with warnings treated as errors outside
the sandbox, including 10 socket tests that start real daemons, and 235 passing inside
it with `-m "not socket"`. Ruff check and format clean, and pyright strict with 0 errors
and 0 warnings. A manual run of start, a second start, status, stop and status behaved as
specified, and no daemon process was left after the suite.

**Not verified.** Only macOS with Python 3.12.12 was tested. Linux behaviour of the lock,
`posix_spawn` with `setsid` and the socket limit is inferred from documentation.

**Next.** Task 4, which needs the operator's choice of loop for Claude Opus 5.5.
