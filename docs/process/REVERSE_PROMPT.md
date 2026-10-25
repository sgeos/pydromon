# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, concrete drivers and the testing model

**Done.**

- **Testing model.** Claude Haiku 5.5, the least expensive model, runs on the
  subscription through Claude Code, confirmed live. `dromon probe` and the live test now
  use it by default.
- **Concrete drivers.** On the operator's decision, subscription use, API use and
  flexible use are separate drivers that bake their strategy in. Six driver files cover
  Opus 5.5 and Haiku 5.5. A subscription driver refuses API usage, an API driver refuses
  a subscription, and a flexible driver accepts either with API-safe settings. Driver
  interface 2 adds a `credential` event, a `strategy` setting and a credential code in
  the state.
- **Engine.** It maps `apiKeySource` to a credential and lets the driver decide. It
  refuses cloud providers for every driver, and API variables for subscription drivers,
  before starting. A live `dromon probe --strategy api` was refused as intended.
- **pykeleusma.** Its reply was read and acknowledged. Request
  `pydromon-20261025-data-segment-request` reports the data segment gap as a deficiency
  and asks for shared data support.

**Decisions made by this line, open to the operator's review.**

- The flexible drivers use the API settings, because a flexible line may be billed per
  token. The settings in each driver are a first judgement, listed in `DRIVER.md`.
- The six files were generated once from a throwaway script that is not committed, and
  each is now maintained as its own concrete driver.

**Verification.** `uv sync`, 342 tests passing with warnings treated as errors outside
the sandbox, and the live test passing on Haiku 5.5 with `DROMON_LIVE_TESTS=1`. Ruff and
pyright strict clean. The wheel contains all six drivers.

**Not verified.** Any API credential, since the operator has none. The API and flexible
paths are tested against scripted sessions only.

**Next.** Task 5, layer 1 messaging and wakes.
