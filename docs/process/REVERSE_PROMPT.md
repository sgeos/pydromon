# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, task 5, layer 1 messaging and wakes

**Done.** Tasks 1 to 4 were already complete. Task 5 is complete.

- `dromon.scheduler` runs in the daemon. A message to an idle, unblocked line wakes it,
  records the wake, marks it running, delivers every pending message in one turn, and
  returns it to idle. A message to a running line waits for the next turn. Lines run
  concurrently, and a per-line turn cap stops runaway exchanges.
- Lines send through an in-process `send_message` tool whose sender the daemon fixes.
  Operators use `dromon create` and `dromon send`, through the new `create_line` and
  `send` operations, with a `rejected` error code for requests that do not apply.
- **Live evidence.** In a real daemon, an operator message woke `alpha`, which sent
  `ping` to `beta`. The idle `beta` was woken, replied `pong` with `reply_to`, and woke
  `alpha` again. Both ended idle. A live test repeats this.

**Security finding.** With a claude.ai login, a line saw the account's connectors, among
them mail sending, file sharing and calendar deletion, despite `setting_sources=[]` and
no built-in tools. Task 4's lines had the same exposure, though nothing was approved for
them. Lines now run with `strict_mcp_config=True`, a permission gate that allows only the
line's own tools, and a refusal of any turn whose engine reports another tool.

**Decisions made by this line, open to the operator's review.**

- The turn cap defaults to 10 per line per daemon lifetime.
- `line_created` gained a `strategy` field, a change to the draft format.
- `--hold-wakes` lets the socket tests run a real daemon without touching the engine.

**Verification.** `uv sync`, 388 tests passing with warnings treated as errors outside
the sandbox, and both live tests passing on Haiku 5.5. Ruff and pyright strict clean.

**Not verified.** Restart recovery against a real engine mid-turn, and two lines on
different models. Engine sessions are not persisted across daemon restarts.

**Next.** Task 6, operator questions.
