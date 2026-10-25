# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, Codex and Grok Build review, and GPT-6-Luna drivers

**Done.** On the operator's direction to record whether the Claude Code driver
conclusions hold for Codex and Grok Build, and to write drivers for GPT-6-Luna.

- `DESIGN.md` records the review, with vendor sources. The main difference is that
  OpenAI documents ChatGPT plan usage for open-source and locally hosted apps, so
  Dromon's own loop could run OpenAI models on a plan, at the cost of holding tokens.
  xAI's documents invite automation, but its consumer terms could not be read.
- Driver interface 3 adds the engine a driver is written for. Drivers for GPT-6-Luna
  on Codex are packaged in the subscription, API and flexible strategies. Their effort
  and context window come from Codex's local model catalogue, fetched on 2026-10-25.
- A line can be created only on a model whose engine has an adapter, and the Claude
  Code adapter refuses a driver for another engine, so no line can run the Luna
  drivers on the wrong engine.

**Decisions made by this line, open to the operator's review.**

- Luna's subscription driver uses the catalogue's default effort, medium, and its
  metered drivers use low. Turn and tool budgets match Haiku 5.5's, the other
  inexpensive model.
- Whether Dromon's own loop should use OpenAI's ChatGPT plan program, and so hold
  tokens, is left to the operator.

**Not done.** There is no Codex adapter, so the Luna drivers have not run against
Codex. The reserved variable lists do not yet name OpenAI's or xAI's credentials.

**Verification.** All tests pass outside the sandbox with warnings treated as errors.
Ruff and pyright strict clean. The wheel includes the three new driver files. No live
test was run, since nothing in this change reaches an engine.
