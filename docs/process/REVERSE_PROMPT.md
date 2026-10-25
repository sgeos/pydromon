# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, write access for Codex lines

**Done.** On the operator's direction to investigate Codex permission profiles for write
access and build it, as `docs/protocol/CODEX.md` specifies.

- **Facts, from Codex 0.164.0's schema and documentation.** A `[permissions.NAME]`
  profile can extend `:workspace`, scope entries to `:workspace_roots` or to absolute
  paths, and give each `read`, `write` or `deny`. A narrower deny wins over a wider
  write. A thread selects a profile with `permissions`, which replaces `sandbox`.
- Every Codex thread now runs under `dromon-line`, defined on the app-server's command
  line. The workspace is writable for a line given `Edit` or `Write`, `.git` is always
  read-only, and the state and runtime directories are denied to reads and writes. The
  thread must report the profile as active, and a `fileChange` counts as tool use.
- **Facts, verified on 2026-10-25.** Under the profile, `codex sandbox -P` refused
  reading, writing, renaming, replacing, linking and changing the mode of `.dromon`,
  and writing `.git`, while the workspace was writable. A GPT-6-Luna line wrote a file,
  had its edits to `.dromon` and `.git` rejected, and ran epagoge's full gate, which
  passed. Epagoge's tree stayed clean.
- **Fact, a fix found on the way.** Codex's read-only sandbox, used until now, let a
  Codex line read `.dromon`, including the event log. Every Codex line now has it
  denied.

**Decisions made by this line, open to the operator's review.**

- `.git` is read-only for every Codex line, so Codex lines never commit, matching
  epagoge's rule and Dromon's caution.
- The profile is fixed. A line cannot widen it, and the sandbox and settings
  documents of Claude Code lines stay refused on Codex lines.
- Epagoge's local `luna` line now has `Bash` and `Edit`.

**Not done, fact.** Codex emits no item for a call it refuses before running, so the
driver's tool budget cannot count refused calls.

**Verification.** 879 tests passing outside the sandbox with warnings treated as errors,
and all 10 live tests passing. Ruff and pyright strict clean.
