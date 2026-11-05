# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-11-05, round 15, address all known issues

**Done, merged in pull requests 136 to 142.**

- **Engines.** Every configured line's engine is started through its adapter's own
  path and closed before a prompt, in `dromon check-engines` and the restart
  preflight, with the two round 14 regressions as tests. Grok's remaining tools are
  blocked by hook, a stale Grok model list is retried on a fresh session, read-only
  Grok lines are allowed after a live probe, and `dromon grok-home` regenerates
  Grok's home files with backups.
- **Lines.** Whole cost survives releases and restarts, with estimates for Codex.
  Unanswered mail counts only real asks. Stale or unknown briefs are flagged at the
  head of a turn. A start may restore a verified snapshot. Failed turns record a
  fixed-word cause. Roles may name whom they review.
- **Operator.** Disclosure checks are budgeted by passes, failures and bytes. Runs
  record their end promptly, `wake_after` takes a run, declared runs take checked
  parameters, `dromon ci-log` copies a CI log to a line, and `review-gate` is declared.
- **Processes.** Child starts are bounded, credential-shaped text is scrubbed from the
  log, a pid ledger lets a line stop its own children, and the gate type-checks every
  CI interpreter.

**Verified live.** The engine check against every active line, a Grok reader's write
denied, Grok's home regenerated, `dromon ci-log` against real CI, and a review
seat's `review-gate` run, which found the wrong interpreter that #143 fixes.

**Not verified.** The items under Unverified live in `HANDOFF.md`.

**Also this round.** Main has failed intermittently since #138 on a process-start
stall, which I did not catch before merging #139 and #140 because I re-ran failures as
flakes. Bisect runs by hand put the rise at #138 and the stall itself earlier. Five of
eight lines are satisfied. The operator is not, and names a stall-free child start as
the change that would most move it.
