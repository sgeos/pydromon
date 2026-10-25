# Drivers

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, driver interface 2, written on 2026-10-25 for roadmap task 4 and
revised the same day to separate credential strategies. Normative. `dromon.driver`
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
| `subscription` | A Claude subscription only | Tuned for the plan's limits |
| `api` | API usage billed per token only | Shorter turns and smaller budgets, since every token is charged |
| `flexible` | Either | The API-safe settings, because the line may be charged per token |

| Driver file | Model | Strategy | Effort | Max turns | Tool budget |
|---|---|---|---|---|---|
| `claude_opus_5_5_subscription.kel` | Claude Opus 5.5 | subscription | high | 8 | 40 |
| `claude_opus_5_5_api.kel` | Claude Opus 5.5 | api | medium | 4 | 20 |
| `claude_opus_5_5_flexible.kel` | Claude Opus 5.5 | flexible | medium | 4 | 20 |
| `claude_haiku_5_5_subscription.kel` | Claude Haiku 5.5 | subscription | low | 4 | 20 |
| `claude_haiku_5_5_api.kel` | Claude Haiku 5.5 | api | low | 2 | 10 |
| `claude_haiku_5_5_flexible.kel` | Claude Haiku 5.5 | flexible | low | 2 | 10 |

Claude Haiku 5.5 is the least expensive model and is the default for probes and tests.
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

**Fact, relayed by the `pykeleusma` line on 2026-10-25.** Keleusma 0.2.2 initializes
each `private data` field to unit at every reset, and `pykeleusma` 0.1.0 does not
support shared `data` blocks. Keeping the state in the host works around both. It
also lets the event log record driver state, which a data segment inside the VM would
hide. A later interface may move the state into a data segment if that proves better.

## Events

| Tag | Text | Number | When |
|---|---|---|---|
| `settings` | | Setting index | Before the first turn, for each index from 0 until the driver answers `end` |
| `credential` | `subscription` or `api` | | When the engine reports its session, before `init`. The driver answers `accept` with the credential code, or `refuse` naming its strategy |
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
| `interface` | Number `2` | Yes |
| `strategy` | Text, `subscription`, `api` or `flexible` | Yes |
| `model` | Text, the model identifier | Yes |
| `effort` | Text, one of `low`, `medium`, `high`, `xhigh` and `max` | Yes |
| `max_turns` | Number, at least 1 | Yes |
| `tool_budget` | Number, tool calls per turn before the driver interrupts | Yes |
| `context_window` | Number, in tokens | Yes |

## Credentials in the host

The host maps Claude Code's reported `apiKeySource` to a credential. `none` is a
subscription, and `ANTHROPIC_API_KEY`, `apiKeyHelper` and `/login managed key` are API
usage. Any other value is refused without consulting the driver. Before a turn the host
refuses every cloud provider, whose source would also read `none`, and for a
subscription driver also refuses every environment variable that would make Claude
Code prefer an API credential, because the driver cannot see the environment.

## Not yet specified

- Events for messages from other lines and for operator answers, which arrive with
  roadmap task 5.
- Drivers for Dromon's own loop, where the driver would also decide each model call.
