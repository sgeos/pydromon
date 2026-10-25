# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, task 4, the first engine adapter

**Done.** On the operator's decisions, lines run on the subscription through Claude Code
and the Agent SDK, and drivers are written in Keleusma.

- `dromon.engine.ClaudeCodeLine` runs one turn per call through `ClaudeSDKClient`, with
  no user or project settings, no tools unless configured, and the engine session
  resumed on the next turn. It refuses an environment selecting an API key, API profile
  or cloud provider before starting, and interrupts and refuses a turn whose engine
  reports any `apiKeySource` other than `"none"`.
- `src/dromon/drivers/claude_opus_5_5.kel` is the Opus 5.5 driver. It declares its
  settings, accepts only its model, counts tool uses and interrupts past a budget of
  40, and has a verified worst case of 84 nominal cost units per decision.
- `dromon probe` ran live on the subscription and reported Claude Code 2.1.310, Agent
  SDK 0.2.174, model `claude-opus-5-5`, credential `none`, and a successful turn.
- `docs/protocol/DRIVER.md` specifies driver interface 1.

**Finding.** `pykeleusma` at `19d4558` cannot use Keleusma data segments. Drivers carry
their state through the host instead, which also lets the event log record it. The
`pykeleusma` line has the report as `pydromon-20261025-data-segment-gap`.

**Decisions made by this line, open to the operator's review.**

- Each turn starts a fresh Claude Code process and resumes the engine session by its
  identifier, so a line survives a daemon restart.
- The probe uses no tools, so it cannot change any file.
- The Agent SDK is pinned exactly, because it bundles the engine itself.
- The live test needs both `-m live` and `DROMON_LIVE_TESTS=1`, because a later `-m`
  replaces the configured deselection.

**Verification.** `uv sync`, 282 tests passing with warnings treated as errors outside
the sandbox, 272 passing inside it with `-m "not socket and not live"`, and the live
test passing once with `DROMON_LIVE_TESTS=1`. Ruff and pyright strict clean. The wheel
contains the driver source.

**Not verified.** Mid-turn message injection, a turn with tools, and the interrupt path
against the real engine. The interrupt path is tested only against a scripted session.

**Next.** Task 5, layer 1 messaging and wakes.
