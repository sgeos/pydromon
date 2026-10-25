# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, clients near parity on a shared core

**Done.** On the operator's direction to take the terminal and graphical clients near
parity with Claude Code, Codex and Grok Build, on a shared core shaped like the
operator's Rust projects.

- **Core.** `dromon.ui` holds a pure, Elm-style state machine with no input or output,
  which a test checks by its imports. It covers the input editor and history, slash
  commands with Tab completion, polling, scrollback, the activity and mail panes, the
  line list, the help panel, interrupts and detaching.
- **Runtime and platforms.** One runtime performs the core's effects through a
  `Daemon` port. The curses and Tk platforms only translate keys and paint frames.
- **Daemon.** A bounded, in-memory activity tail per line, fed by each engine adapter,
  and the `line_activity` and `interrupt` operations, specified in `CLIENT.md`.

**Verification.** The full gate passed outside the sandbox with 1062 tests. The curses
client was driven in a pseudo-terminal against a daemon with wakes held, and the Tk
window was drawn while withdrawn. The live test in which a GPT-6-Luna line messages a
Haiku line now also checks both lines' activity. It passed twice. One earlier run
failed at the existing check that the Haiku line completed a turn, before the new
checks, and its cause was not captured. It may be a flake and is worth watching.

**Not at parity, recorded in the design.** Polling stands in for pushed updates, the
engines report no token or context figures, Dromon has no permission prompts to show,
Markdown is rendered only as code fences and headings, and wide characters are taken
to fill one column. Codex and Claude Code text arrives a message at a time.

**For the operator.** External agents over MCP cannot yet interrupt a line, and an
interrupted Claude Code turn resumes from whatever its engine recorded.
