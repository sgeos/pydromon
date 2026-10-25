# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, the epagoge line's notes before dogfooding

**Done.** On the operator's direction to review the notes the epagoge session left in
`tmp/epagoge-dogfooding-notes.md` and act on them. The notes are a peer's report, so
each claim was checked before it was acted on.

- **Item 1, which revision.** The modified files the notes saw were the external agent
  work in progress, merged since as `c711727`. The handoff now says to dogfood from a
  clean, merged `main`, because `.venv` is an editable install that runs the working
  tree.
- **Item 2, the gate from a line, measured.** Epagoge's full gate passed from the
  sandboxed `reviewer` line on Claude Haiku 5.5, with 678 tests, a clean disclosure scan
  over 957 files and exit status 0.
- **Item 3, the commit block.** The `ask` rules match the command as written. The
  Claude profile now also denies writes to `.git` in its sandbox and `Edit` there.
  **Fact.** From the same line a Python write into `.git` failed with `Operation not
  permitted`, and `touch` there was refused before it ran. Epagoge's tree stayed clean.
- **Items 4 and 5, conduct.** A local `.dromon/LINES.md`, added to the lines'
  instructions, tells them to report rather than commit or retry, and never to put the
  contents of `secret/` into messages. The read of that directory stays allowed.
- **Item 6.** The project file states that `luna` reviews and cannot run the gate.
- **Item 7, memory.** `dromon teacher up` now refuses to load when available memory is
  below the model's size plus `memory_headroom_gb`, 4 by default, unless forced.
  **Fact.** The notes read free pages, about 1.6 GB, while the kernel reported 82
  percent, about 28 GB, available, so Dromon uses the kernel's figure.

**Decisions made by this line, open to the operator's review.**

- The default headroom is 4 GB. With 28 GB available and an 18.6 GB model, a load is
  allowed with about 5 GB to spare.
- Unknown available memory, on a system that reports neither source, does not refuse.

**Verification.** 875 tests passing outside the sandbox with warnings treated as errors.
Ruff and pyright strict clean. The two live probes above ran in epagoge on Haiku 5.5.
