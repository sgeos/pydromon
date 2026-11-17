# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-11-17, round 17, address all known issues and drivers for new models

**Done, merged in pull requests 143, 149 and 154 to 162.**

- **Drivers.** Every coding model Anthropic, OpenAI and xAI released between
  2026-08-18 and 2026-11-16 already had a driver. Google's `gemini-4-argon`,
  `gemini-4-carbon` and `gemini-4-hydrogen` now have Cursor Agent drivers at medium
  effort for every strategy, and Argon a Pi driver on its API key. No Antigravity
  id is documented, so none was added.
- **Confinement.** Codex lines deny credentials and the project's `secret/` since round
  9, and Claude Code lines now deny `secret/` too. A live check on 2026-11-17 from six
  Codex lines found `secret/`, `~/.ssh` and Codex's sign-in file all denied.
- **Gates and state.** The disclosure scan runs from worktrees, `check.sh` runs under
  bash 3.2, and state versions and rollback floors are per-branch files.
- **Runs.** Declared runs queue on the machine status and a lease, with an age limit.
- **Mail and leases.** Only mail that asks counts as unanswered, leases record expiry
  and are capped in place, and the overview reads an index.
- **Processes.** A stalled start records a sample of the stuck child. A Grok bridge
  that fails is named as the cause, and its stderr is kept, scrubbed.
- **CI.** The scheduler's idle wait busy-polled, which made two ownership tests run
  past five minutes on Linux and turned main red at `aa45ba3`. Fixed in #158.

**Team.** From 23:04Z on 2026-11-16, by the operator's direction, every role passed to
an OpenAI seat on Codex, with Anthropic seats as fallback.

**Verified.** Full gates outside the sandbox or through the daemon on each branch,
continuous integration on every merge, reviews with mutation checks on every branch,
the no-cost engine check on every line, and the live confinement check above.

**Found broken and fixed.** On `c4cfe6f` the daemon's sweep killed every Claude Code
and Grok engine early in a turn, because a live launcher read as exited. Runtime
reproduced it without a model, and #162 fixed it. A Claude Code turn completed on the
restarted daemon. A Grok turn was refused for an exhausted usage balance, so Grok is
not yet confirmed live.

**Experience reports.** Seven Codex seats reported, five SATISFIED and two NOT
SATISFIED, in each worktree's `tmp/reports`. The operator's report is in the main
checkout's `tmp/reports`. The Anthropic seats and the Grok line wrote none, since
their turns failed until #162, and Grok's balance then ran out.
