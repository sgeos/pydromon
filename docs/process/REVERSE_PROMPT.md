# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-26, the session-line experience loop, rounds 1 to 7

**Done.** On the operator's goal that pydromon's dogfooded lines improve the
session-line experience in an iterative loop of reports, research, changes and new
reports, until every line is satisfied or what remains is unreasonable.

- **Rounds.** Pull requests 42 to 49 carried rounds 1 to 7. Each round began from the
  lines' reports, was built by the Opus `worker` line, reviewed by the Haiku `reviewer`
  line, fixed by the worker, probed live by the operator's session in a scratch
  project, and merged on green checks. A last change after round 7 makes a compaction's
  `trigger` only what the engine reported.
- **Outcome.** In round 7, `worker`, `reviewer` and `luna` each wrote
  `VERDICT: SATISFIED`. The reports are in `tmp/reports/`.
- **Verified live.** Kept sessions, cost differencing, task-end notes, context
  estimates, `compact_session` with its own wake and that wake's withdrawal, automatic
  compaction with its hold on Claude Code and on Codex, and Codex usage, which was
  found to report a compaction as a `contextCompaction` item rather than the notice
  its binary names.

**Declined, with reasons given to the lines.** A daemon-run job runner, which would
bypass the sandbox. Listing untracked processes, which Darwin 27 does not allow the
daemon. A size after a Codex compaction, which Codex does not report, so Dromon gives
an upper bound from the next call instead.

**Inferences, not verified.** The reviewer accepted two of the operator's live results
and the round 7 fixes without observing or re-reading them. A refused turn's request
carry-over is covered by tests on both engines and was not forced live.

**For the operator.**
1. Start Keleusma's daemon for kaizen when its live session has ended, as
   `KELEUSMA_DOGFOOD.md` shows.
2. The lines' remaining wishes are wording and status refinements, listed in their
   round 7 reports.
