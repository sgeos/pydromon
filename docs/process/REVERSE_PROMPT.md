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

**Next.** Task 8, the interactive client.
