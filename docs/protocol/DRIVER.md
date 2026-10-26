# Drivers

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, driver interface 3, written on 2026-10-25 for roadmap task 4 and
revised the same day to separate credential strategies and, in interface 3, to name
each driver's engine. Normative. `dromon.driver`
hosts drivers through `pykeleusma`, and the packaged drivers are in
`src/dromon/drivers/`.

## Purpose

A driver is a Keleusma stream processor holding the behaviour specific to one model:
the settings it is run with, the model it accepts, and its reaction to each event the
engine emits. Model-specific behaviour is written into the driver rather than
configured, so each driver states exactly which model it serves.

## Concrete drivers

**Decided by the operator on 2026-10-25.** A driver is concrete. Each one fixes one
model and one credential strategy in its code, rather than choosing behaviour from
parameters. The distinction between a subscription and API usage matters, because a
subscription draws on a plan's limits while API usage is charged for every token.

| Strategy | Accepts | Settings |
|---|---|---|
| `subscription` | A subscription only, a Claude plan or a ChatGPT plan | Tuned for the plan's limits |
| `api` | API usage billed per token only | Shorter turns and smaller budgets, since every token is charged |
| `flexible` | Either | The API-safe settings, because the line may be charged per token |

| Driver file | Model | Strategy | Effort | Max turns | Tool budget |
|---|---|---|---|---|---|
| `claude_opus_5_5_subscription.kel` | Claude Opus 5.5 | subscription | high | 200 | 400 |
| `claude_opus_5_5_api.kel` | Claude Opus 5.5 | api | medium | 4 | 20 |
| `claude_opus_5_5_flexible.kel` | Claude Opus 5.5 | flexible | medium | 4 | 20 |
| `claude_haiku_5_5_subscription.kel` | Claude Haiku 5.5 | subscription | low | 100 | 200 |
| `claude_haiku_5_5_api.kel` | Claude Haiku 5.5 | api | low | 2 | 10 |
| `claude_haiku_5_5_flexible.kel` | Claude Haiku 5.5 | flexible | low | 2 | 10 |
| `claude_fable_5_1_subscription.kel` | Claude Fable 5.1 | subscription | high | 200 | 400 |
| `claude_fable_5_1_api.kel` | Claude Fable 5.1 | api | medium | 4 | 20 |
| `claude_fable_5_1_flexible.kel` | Claude Fable 5.1 | flexible | medium | 4 | 20 |
| `gpt_6_luna_subscription.kel` | GPT-6-Luna on Codex | subscription | medium | 200 | 400 |
| `gpt_6_luna_api.kel` | GPT-6-Luna on Codex | api | low | 2 | 10 |
| `gpt_6_luna_flexible.kel` | GPT-6-Luna on Codex | flexible | low | 2 | 10 |
| `grok_4_7_subscription.kel` | Grok 4.7 on Grok Build | subscription | high | 200 | 400 |
| `grok_4_7_api.kel` | Grok 4.7 on Grok Build | api | medium | 4 | 20 |
| `grok_4_7_flexible.kel` | Grok 4.7 on Grok Build | flexible | medium | 4 | 20 |
| `pi__openai__gpt_6_luna_subscription.kel` | GPT-6-Luna on Pi | subscription | medium | 200 | 400 |
| `pi__openai__gpt_6_luna_api.kel` | GPT-6-Luna on Pi | api | medium | 4 | 20 |
| `pi__openai__gpt_6_luna_flexible.kel` | GPT-6-Luna on Pi | flexible | medium | 4 | 20 |
| `cursor_agent__gpt_5_6_luna_medium_subscription.kel` | GPT-5.6 Luna on Cursor Agent | subscription | medium | 200 | 400 |
| `cursor_agent__gpt_5_6_luna_medium_api.kel` | GPT-5.6 Luna on Cursor Agent | api | medium | 4 | 20 |
| `cursor_agent__gpt_5_6_luna_medium_flexible.kel` | GPT-5.6 Luna on Cursor Agent | flexible | medium | 4 | 20 |
| `antigravity__gemini_3_1_pro_subscription.kel` | Gemini 3.1 Pro on Antigravity | subscription | medium | 200 | 400 |
| `antigravity__gemini_3_1_pro_api.kel` | Gemini 3.1 Pro on Antigravity | api | medium | 4 | 20 |
| `antigravity__gemini_3_1_pro_flexible.kel` | Gemini 3.1 Pro on Antigravity | flexible | medium | 4 | 20 |

**Decided 2026-10-26, for dogfooding.** The subscription drivers' turn limits and tool
budgets are sized for a line that develops software, which reads, edits and runs a
gate many times in one turn. The earlier values, 4 to 8 engine turns and 20 to 40 tool
calls, ended such turns early. A subscription draws on a plan's limits rather than
charging per token, and the per-line conversation limits, which a client can now set,
remain the operator's bound on a line's work. Drivers that may bill per token keep
their small budgets.

Claude Haiku 5.5 is the least expensive model and is the default for probes and tests.

**Claude Fable 5.1, verified on 2026-10-26.** Added at the operator's direction for
Keleusma's proofs line. One `dromon probe --model claude-fable-5-1` turn, run with the
operator's approval, reported the subscription credential and the model
`claude-fable-5-1` from Claude Code 2.1.310, and replied `ready`. Its context window of
1,000,000 tokens is copied from the Opus drivers and is not verified.

GPT-6-Luna's settings follow Codex's model catalogue as fetched on 2026-10-25, which
gives it a default effort of medium and a context window of 272,000 tokens, and for a
ChatGPT plan the subscription strategy means Codex's own sign-in.

## Engines

Each driver names the engine it is written for, `claude-code`, `codex`, `grok-build`,
`pi`, `cursor-agent` or `antigravity`. A file is named for its model with `-` and `.` written as `_`, and with the `:`
and `/` of an engine-qualified name written as `__`. A model has
packaged drivers once they are written, but a line can be created on it only when
Dromon has an adapter for its engine, and an adapter refuses a driver written for
another engine. All three engines have adapters, as of 2026-10-25 for `codex`, which
[`CODEX.md`](CODEX.md) specifies, and for `grok-build`, which
[`GROK.md`](GROK.md) specifies and which is not yet verified against a live turn. The
`pi` engine has an adapter as of 2026-10-26, which [`PI.md`](PI.md) specifies and which
is not yet verified against a live turn. The `cursor-agent` engine has an adapter as of
2026-10-26, which [`CURSOR.md`](CURSOR.md) specifies. A daemon refuses Cursor lines, and only `dromon
probe --accept-partial-isolation` runs a Cursor turn. The `antigravity` engine has an adapter as of
2026-10-26, written from documentation alone, which [`ANTIGRAVITY.md`](ANTIGRAVITY.md)
specifies. A daemon refuses its lines, and the probe's flag reaches it, but its
credential is unverified, which no packaged driver accepts, so a turn is refused
before `agy` starts unless the probe is also given `--accept-unverified-credential`,
the operator's confirmation by hand, which puts `subscription` to the driver in its
place. Its model identifier is a guess.

**Model names. Decided on 2026-10-26.** Claude Code, Codex and Grok Build each run their
own vendor's models, which Dromon names by the vendor's identifier. Pi, Cursor Agent and
Antigravity run other vendors' models, so their identifiers would read as, or come to
collide with, the models above, which run on another engine with other credentials and
costs. Their models are named `ENGINE:ID`, the engine and the harness's own identifier,
such as `pi:openai/gpt-6-luna`. The engine can be read from the name, and a name never
stands for two engines. No identifier seen in the three harnesses' catalogues on
2026-10-26 contains a colon, so the first colon divides the two parts. An adapter hands
the driver's `init` event the name its engine reports, the harness's own identifier or,
for Cursor Agent, which reports a display name such as `GPT-5.6 Luna 1M`, that name.
A file is named for its model and strategy, and the host refuses a file whose declared
model or strategy differs from its name.

The host performs every action a driver yields, so the driver's verified worst-case
time covers each whole decision.

## Shape

```
loop main(input: (Event, State)) -> (Action, State)
```

| Part | Keleusma type | Meaning |
|---|---|---|
| Event | `(Text, Text, Word)` | A tag, a text and a number from the host |
| Action | `(Text, Text, Word)` | A tag, a text and a number for the host |
| State | `(Word, Word, Word)` | Tool uses in the current turn, turns completed, and the credential code, held by the host between cycles |

Each event is one cycle. The host resumes the driver with the event and the current
state, receives the action and the next state, and resumes once more with an ignored
value so that the cycle ends in a reset. The host stores the state and passes it back
with the next event. A restarted line passes its stored state, so it continues where
it stopped.

The credential code is 0 before the engine reports a credential, 1 for a subscription
and 2 for API usage.

The state lives in the host so that the event log can record it and a restarted line
can resume it, which a data segment inside the VM would hide.

**Fact, executed on 2026-10-25.** A `private data` field starts as unit and, once
assigned, persists across cycles. An earlier statement here that it resets at every
cycle was wrong, and came from a test that read a field before assigning it. The
`pykeleusma` line has accepted shared data support as its task 11, which lends a host
buffer to the module's `data` block. A later interface may use either.

## Events

| Tag | Text | Number | When |
|---|---|---|---|
| `settings` | | Setting index | Before the first turn, for each index from 0 until the driver answers `end` |
| `credential` | `subscription`, `api` or `unverified` | | When the engine reports its session, before `init`. The driver answers `accept` with the credential code, or `refuse` naming its strategy. `unverified` is sent for an engine that names no credential source, Antigravity alone, and no packaged driver accepts it. `dromon probe --accept-unverified-credential` sends `subscription` in its place, on the operator's confirmation by hand |
| `init` | The model the engine reports | | When the engine reports its session |
| `turn_start` | | | Before each turn |
| `text` | | Length of the text | For each text block |
| `thinking` | | | For each thinking block |
| `tool_use` | The tool's name | | For each tool call |
| `result` | The result subtype | The engine's turn count | When the turn ends |

A driver answers an event it does not know with `continue`, so a host may add events
without breaking older drivers.

## Actions

| Tag | Meaning |
|---|---|
| `continue` | Nothing to do |
| `interrupt` | Interrupt the current turn. The text names the reason |
| `end_turn` | The turn is over |
| `accept` | The engine's reported credential or model is acceptable |
| `refuse` | The engine's reported credential or model is not acceptable. The text names the driver's strategy or model, and the host interrupts and refuses the turn |

Any other tag, a malformed value or a run-time failure is a driver defect, and the host
stops the line rather than guessing.

## Settings

Answered to `settings` events by index, as `(name, text, number)`, ending with `end`.
At most 32 settings are read.

| Name | Value | Required |
|---|---|---|
| `interface` | Number `3` | Yes |
| `engine` | Text, `claude-code`, `codex`, `grok-build`, `pi`, `cursor-agent` or `antigravity` | Yes |
| `strategy` | Text, `subscription`, `api` or `flexible` | Yes |
| `model` | Text, the model identifier | Yes |
| `effort` | Text, one of `low`, `medium`, `high`, `xhigh` and `max` | Yes |
| `max_turns` | Number, at least 1 | Yes |
| `tool_budget` | Number, tool calls per turn before the driver interrupts | Yes |
| `context_window` | Number, in tokens | Yes |

## Credentials in the host

For Codex, the host maps the account Codex reports, `chatgpt` to a subscription and
`apiKey` to API usage, as [`CODEX.md`](CODEX.md) specifies. For Claude Code, the host
maps Claude Code's reported `apiKeySource` to a credential. `none` is a
subscription, and `ANTHROPIC_API_KEY`, `apiKeyHelper` and `/login managed key` are API
usage. Any other value is refused without consulting the driver. Before a turn the host
refuses every cloud provider, whose source would also read `none`, and for a
subscription driver also refuses every environment variable that would make Claude
Code prefer an API credential, because the driver cannot see the environment.

## Not yet specified

- Events for messages from other lines and for operator answers, which arrive with
  roadmap task 5.
- Drivers for Dromon's own loop, where the driver would also decide each model call.
