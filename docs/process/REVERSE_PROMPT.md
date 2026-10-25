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
