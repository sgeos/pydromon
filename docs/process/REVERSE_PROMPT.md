# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, OpenAI models on Dromon through Codex

**Done.** On the operator's direction to get OpenAI models running on Dromon and to test
with GPT-6-Luna.

- `dromon.codex` runs a line through `codex app-server` over standard input and output,
  as `docs/protocol/CODEX.md` specifies. Lines can be created on `gpt-6-luna`, the
  scheduler picks the adapter by the driver's engine, and `dromon probe --model
  gpt-6-luna` runs on Codex.
- **Facts, observed on 2026-10-25.** Codex reported a ChatGPT account on the Pro plan.
  Without Dromon's overrides, a GPT-6-Luna thread connected to the operator's mail,
  calendar, document, chat and task MCP servers. With them every server reported
  disabled. The operator's environment sets `OPENAI_API_KEY`, which a subscription
  line's Codex is now started without.
- Dromon's tools reach the model as Codex dynamic tools. A resumed thread keeps them.

**Decisions made by this line, open to the operator's review.**

- The app-server rather than `codex exec`, which OpenAI recommends for automation,
  because only the app-server lets Dromon serve its tools in process and see each item
  as it starts. Both the app-server and its dynamic tools are experimental.
- Codex lines run in Codex's read-only sandbox, never ask for approval, and take only
  `Bash`. File edits wait for a way to protect `.dromon` from Codex's writable sandbox.
- A subscription line's Codex is started without OpenAI API variables rather than
  refused, because Dromon starts the process, and the reported account is still checked.

**Not done.** MCP servers on Codex lines, cost reporting, the driver's turn limit and a
Grok Build adapter.

**Verification.** 779 tests passing outside the sandbox with warnings treated as errors,
and all 8 live tests passing. The Codex ones showed that a GPT-6-Luna line resumes its
thread with its tools, recalling a word from its first turn in its second, and that a
GPT-6-Luna line messages a Claude Haiku 5.5 line in one scheduler. Ruff and pyright
strict clean. Linux was not exercised live.
