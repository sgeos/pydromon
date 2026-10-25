# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, task 7, conversation control

**Done.** Tasks 1 to 6 were already complete. Task 7 is complete, on the operator's
direction to pick sensible limits to be tuned later.

- `dromon.limits` counts each line's turns in a row without operator mail, at most 12
  by default, and its estimated cost since release, at most $2. An optional cooldown,
  off by default, spaces a line's turns.
- A line past a limit is held with `line_held`, takes no turns, and keeps its mail. The
  daemon asks, on the line's behalf, the operator who last messaged it, or the default
  operator named by `dromond --operator`, whether to resume it.
- Any reply to that question, or `dromon release`, releases the line and resets its
  counts. Holds survive a restart, while the counts do not.
- The temporary per-line turn cap is gone.

**Decisions made by this line, open to the operator's review.**

- The defaults, 12 autonomous turns and $2 of estimated cost, are a first guess.
- Counting turns without operator mail, rather than per pair of lines, catches a
  runaway among any number of lines.
- The cost is the SDK's estimate, which on a subscription is not a charge.
- A hold question offers the choice resume, but any reply releases the line.

**Verification.** `uv sync`, 512 tests passing with warnings treated as errors outside
the sandbox, and all three live tests passing on Haiku 5.5. Ruff and pyright strict
clean. A scripted runaway between two lines was held within the limit with a question
from each.

## 2026-10-25, task 8, the interactive client

**Done.** On the operator's direction to look at what Claude Code, Codex and Grok Build
use, then pick something sensible.

- **Survey.** Claude Code uses a customized Ink on Node.js, Codex a Ratatui and
  Crossterm interface in Rust, and Grok Build its own Rust pager crate. All three are
  full-screen terminal programs. The sources were secondary and are cited in the design.
- **Choice.** Curses, from the standard library, which maps onto Ratatui and Crossterm
  in the Rust port, and Tk for the launchable window, also from the standard library.
- `dromon attach LINE` opens curses, and `--gui` opens Tk. Both are thin shells over
  `dromon.attach`, which polls the new `line_view` operation and turns typed input into
  messages and the `/answer`, `/ack`, `/release`, `/help` and `/quit` commands.
- Every client is an independent connection, so any number may attach at once, as a
  socket test with two clients shows.

**Verification.** 547 tests passing outside the sandbox, with nothing skipped. The real
curses client ran under a pseudo-terminal against a held daemon, drew the view, sent a
typed message and quit with status 0. The Tk window was not opened, to avoid a window
on the operator's screen overnight, and is covered only by an import test and the
shared logic it calls.

**Next.** Task 9, per-project configuration.
