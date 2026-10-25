# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, sandboxing parity with Claude Code

**Done.** On the operator's direction to attempt sandboxing parity with Claude Code,
configurable per line and per project, after milestone 0.1.0.

- **Fact.** Dromon starts Claude Code with `setting_sources=[]`, so before this change a
  line's Bash commands ran with no sandbox but any managed policy.
- A sandbox document is Claude Code's `sandbox` object with its key names. It may be set
  for the project in `[sandbox]`, for a profile or line in the project file, or for one
  line with `--sandbox-config FILE` on `dromon create` and `dromon configure`. Layers
  merge as Claude Code's scopes do, scalars by specificity and arrays united.
- Keys Claude Code accepts only from user or managed settings, such as
  `filesystem.disabled`, need the client's operator authority. Managed-only keys,
  `ripgrep` and credential masking are refused.

**Decisions made by this line, open to the operator's review.**

- Unattended defaults. An enabled sandbox fails closed, refuses unsandboxed retries
  and denies hosts outside the allowlist unless the document says otherwise, because a
  line has nobody to answer Claude Code's prompts. The gate also refuses a
  `dangerouslyDisableSandbox` call.
- Protected directories. An enabled sandbox always denies reading and writing the state
  and runtime directories, and a document that reopens them is refused, so a line
  cannot grant itself tools or reach the socket with operator authority.
- Relative paths resolve against the project root, since Dromon sets no settings file.

**Verification.** 639 tests passing outside the sandbox with warnings treated as errors,
and all 4 live tests passing on Haiku 5.5. In the new live test, a sandboxed line ran
both Bash commands, and only the write inside the project succeeded. Ruff and pyright
strict clean. Linux was not exercised live.
