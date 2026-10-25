# Drivers

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, driver interface 1, written on 2026-10-25 for roadmap task 4.
Normative. `dromon.driver` hosts drivers through `pykeleusma`, and
`src/dromon/drivers/claude_opus_5_5.kel` is the first driver.

## Purpose

A driver is a Keleusma stream processor holding the behaviour specific to one model:
the settings it is run with, the model it accepts, and its reaction to each event the
engine emits. Model-specific behaviour is written into the driver rather than
configured, so each driver states exactly which model it serves. The host performs
every action a driver yields, so the driver's verified worst-case time covers each
whole decision.

## Shape

```
loop main(input: (Event, State)) -> (Action, State)
```

| Part | Keleusma type | Meaning |
|---|---|---|
| Event | `(Text, Text, Word)` | A tag, a text and a number from the host |
| Action | `(Text, Text, Word)` | A tag, a text and a number for the host |
| State | `(Word, Word)` | The driver's state, held by the host between cycles |

Each event is one cycle. The host resumes the driver with the event and the current
state, receives the action and the next state, and resumes once more with an ignored
value so that the cycle ends in a reset. The host stores the state and passes it back
with the next event. A restarted line passes its stored state, so it continues where
it stopped.

**Inference.** Keeping the state in the host is a workaround for `pykeleusma` at
`19d4558`, where a `private data` field reads as unit rather than zero and a shared
`data` block cannot be driven. It also lets the event log record driver state, which a
data segment inside the VM would hide. A later interface may move the state into a data
segment if that proves better.

## Events

| Tag | Text | Number | When |
|---|---|---|---|
| `settings` | | Setting index | Before the first turn, for each index from 0 until the driver answers `end` |
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
| `accept` | The engine's reported model is the driver's |
| `refuse` | The engine's reported model is not the driver's. The text names the expected model, and the host refuses the turn |

Any other tag, a malformed value or a run-time failure is a driver defect, and the host
stops the line rather than guessing.

## Settings

Answered to `settings` events by index, as `(name, text, number)`, ending with `end`.
At most 32 settings are read.

| Name | Value | Required |
|---|---|---|
| `interface` | Number `1` | Yes |
| `model` | Text, the model identifier | Yes |
| `effort` | Text, one of `low`, `medium`, `high`, `xhigh` and `max` | Yes |
| `max_turns` | Number, at least 1 | Yes |
| `tool_budget` | Number, tool calls per turn before the driver interrupts | Yes |
| `context_window` | Number, in tokens | Yes |

## Not yet specified

- Events for messages from other lines and for operator answers, which arrive with
  roadmap task 5.
- Drivers for Dromon's own loop, where the driver would also decide each model call.
