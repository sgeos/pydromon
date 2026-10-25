# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, task 9, per-project configuration

**Done.** On the operator's direction that configuration may grow as large as a
multi-agent project needs, after a review of the settings of Claude Code, Codex and
Grok Build.

- **Survey.** Claude Code layers JSON settings and merges lists across layers. Codex and
  Grok Build use TOML in a user file and a project file, with requirements files for
  organizations, and Codex adds profiles. The sources are cited in
  `docs/protocol/PROJECT_CONFIG.md`.
- `.dromon/dromon.toml`, file version 1, declares the default operator, the tick
  cadence, the conversation limits, the wake policy, reusable profiles, and each line's
  model, strategy, tools, limits and wake policy.
- Unknown keys are errors. Every error names the file and, where it can be found, the
  line, and a syntax error also names its column. `dromond` refuses to start on an
  invalid file, and `dromon config` validates the file without a daemon.
- At start the daemon creates missing lines and reconfigures lines whose tools changed.
  It refuses a file that changes an existing line's model or strategy.
- `dromond` flags now override the file rather than always applying their defaults.

**Decisions made by this line, open to the operator's review.**

- A line's own key replaces its profile's key whole, unlike Claude Code's merged lists,
  so that a line's grant reads in one place.
- The wake policy is a tick divisor. A line may start a turn only on ticks divisible by
  its `every_ticks`, and mail waits until then.
- Lines created through the client and absent from the file are left alone rather than
  removed.

**Not done.** User and managed layers, and references to secrets held outside the file.

**Verification.** 574 tests passing outside the sandbox with warnings treated as errors,
and the three live tests passing on Haiku 5.5. Ruff and pyright strict clean. A daemon
started on a real project file created its declared line.

**Next.** Task 10, continuous integration.

## 2026-10-25, task 10, continuous integration

**Blocked.** The workflow is written and committed on `ci/github-actions`, but GitHub
refused the push. Fact, from the push error, the GitHub command-line token has the
scopes `repo`, `gist`, `read:org` and `admin:public_key` and lacks `workflow`, which
GitHub requires for any change under `.github/workflows`. Granting it needs the
operator, through `gh auth refresh -h github.com -s workflow`. This line stopped there.

- **Review.** Codex pins actions to commit hashes and checks out without persisting
  credentials, and runs formatting and lint on Linux, macOS and Windows. goose adds
  OpenSSF Scorecard and Dependabot. Aider tests five Python versions on Ubuntu with
  tag-pinned actions. pykeleusma, this family's model, pins hashes, reads only and
  tests Python 3.12 and 3.15 on Linux and macOS.
- **Workflow.** Ruff, Pyright and pytest with warnings as errors, socket tests included,
  on Ubuntu and macOS for Python 3.12 and 3.15. A read-only token, no secrets, pinned
  actions, cancelled superseded runs and a 30-minute timeout. Live tests do not run,
  because they need the operator's subscription.
- **Dependabot** proposes action updates weekly, after a seven-day cooldown. Python
  dependencies stay pinned by hand.
- **Decision open to review.** The pins are the releases pykeleusma already runs rather
  than releases three days old, by the cooldown's reasoning.

**Verification.** actionlint reports the workflow clean. The workflow has not run, so
whether the jobs pass on GitHub's runners is unverified. The local gate passes.
